package io.spotswallow.api;

import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.concurrent.ThreadLocalRandom;
import java.util.concurrent.TimeUnit;

import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.MeterRegistry;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.amqp.core.Message;
import org.springframework.amqp.core.MessageBuilder;
import org.springframework.amqp.core.MessageDeliveryMode;
import org.springframework.amqp.rabbit.connection.CorrelationData;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * 아웃박스 발행기.
 * PENDING 레코드를 RabbitMQ 로 보내고, 브로커의 Publisher Confirm 을 받은 뒤에만 PUBLISHED 로 바꾼다.
 * 확인 전에 죽으면 레코드가 PENDING 으로 남아 다시 발행된다 (중복 발행 가능, 유실 없음).
 * 중복은 워커의 멱등성(settlement_results PK)이 흡수한다.
 */
@Component
public class OutboxPublisher {
    private static final Logger log = LoggerFactory.getLogger(OutboxPublisher.class);

    record OutboxRow(long outboxId, String jobId, String payload) {}

    private final JdbcTemplate jdbc;
    private final TransactionTemplate tx;
    private final RabbitTemplate rabbit;
    private final int batchSize;
    private final double crashAfterSendProbability;
    private final Counter published;
    private final Counter failed;

    public OutboxPublisher(JdbcTemplate jdbc, TransactionTemplate tx, RabbitTemplate rabbit, MeterRegistry registry,
                           @Value("${swallow.outbox.batch-size:50}") int batchSize,
                           @Value("${swallow.chaos.publisher-crash-after-send-probability:0}") double crashAfterSendProbability) {
        this.jdbc = jdbc;
        this.tx = tx;
        this.rabbit = rabbit;
        this.batchSize = batchSize;
        this.crashAfterSendProbability = crashAfterSendProbability;
        this.published = Counter.builder("swallow.outbox.published").register(registry);
        this.failed = Counter.builder("swallow.outbox.publish_failed").register(registry);
    }

    @Scheduled(fixedDelayString = "${swallow.outbox.poll-ms:200}")
    public void publishPending() {
        try {
            tx.executeWithoutResult(status -> publishBatch());
        } catch (Exception e) {
            // 실패한 배치는 롤백되어 PENDING 으로 남고 다음 주기에 다시 시도된다.
            failed.increment();
            log.warn("outbox publish batch failed, will retry: {}", e.toString());
        }
    }

    private void publishBatch() {
        // SKIP LOCKED: 발행기를 여러 개 띄워도 같은 레코드를 동시에 잡지 않는다.
        List<OutboxRow> rows = jdbc.query("""
                SELECT outbox_id, job_id, payload FROM outbox
                 WHERE status = 'PENDING'
                 ORDER BY outbox_id
                 LIMIT ?
                 FOR UPDATE SKIP LOCKED
                """, (rs, i) -> new OutboxRow(rs.getLong(1), rs.getString(2), rs.getString(3)), batchSize);

        for (OutboxRow row : rows) {
            Message message = MessageBuilder.withBody(row.payload().getBytes(StandardCharsets.UTF_8))
                    .setContentType("application/json")
                    .setDeliveryMode(MessageDeliveryMode.PERSISTENT)
                    .setMessageId(row.jobId())
                    .build();
            CorrelationData correlation = new CorrelationData(Long.toString(row.outboxId()));
            rabbit.send(RabbitTopology.EXCHANGE, RabbitTopology.JOBS_ROUTING_KEY, message, correlation);

            CorrelationData.Confirm confirm;
            try {
                confirm = correlation.getFuture().get(5, TimeUnit.SECONDS);
            } catch (Exception e) {
                throw new IllegalStateException("no publisher confirm for outbox " + row.outboxId(), e);
            }
            if (!confirm.isAck()) {
                throw new IllegalStateException("broker nacked outbox " + row.outboxId() + ": " + confirm.getReason());
            }

            // 장애 주입: 브로커는 받았지만 PUBLISHED 표시 전에 프로세스가 죽는 상황.
            if (crashAfterSendProbability > 0 && ThreadLocalRandom.current().nextDouble() < crashAfterSendProbability) {
                log.error("CHAOS: publisher halting after send, before marking outbox {} published", row.outboxId());
                Runtime.getRuntime().halt(137);
            }

            jdbc.update("UPDATE outbox SET status = 'PUBLISHED', published_at = now(), attempts = attempts + 1 WHERE outbox_id = ?",
                    row.outboxId());
            published.increment();
        }
    }
}
