package io.spotswallow.worker;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.time.LocalDate;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import com.rabbitmq.client.Channel;
import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.Timer;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.amqp.core.Message;
import org.springframework.amqp.rabbit.annotation.RabbitListener;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.dao.DataAccessResourceFailureException;
import org.springframework.dao.TransientDataAccessException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * 스팟 워커.
 *
 * 처리 순서: 메시지 수신 → 계산(트랜잭션 밖) → 짧은 트랜잭션으로 결과 확정 → 커밋 후 Ack.
 *
 * - 계산 중 회수: Ack 가 없으므로 RabbitMQ 가 다른 워커에게 다시 전달한다.
 * - 커밋 후 Ack 전 회수: 다시 전달되지만 settlement_results 의 PK 충돌로 결과는 한 번만 확정되고, Ack 만 보낸다.
 * 즉 전달은 최소 한 번(at-least-once), 반영은 정확히 한 번(effectively-once).
 */
@Component
public class SettlementWorker {
    private static final Logger log = LoggerFactory.getLogger(SettlementWorker.class);
    private static final Pattern JOB_ID = Pattern.compile("\"jobId\"\\s*:\\s*\"([^\"]+)\"");

    private final JdbcTemplate jdbc;
    private final TransactionTemplate tx;
    private final SettlementCalculator calculator;
    private final String workerId;
    private final ChaosPolicy chaos;

    private final Counter completed;
    private final Counter duplicateSkipped;
    private final Counter redelivered;
    private final Counter requeued;
    private final Counter deadLettered;
    private final Timer processing;

    public SettlementWorker(JdbcTemplate jdbc, TransactionTemplate tx, SettlementCalculator calculator,
                            MeterRegistry registry, ChaosPolicy chaos,
                            @Value("${swallow.worker.id}") String workerId) {
        this.jdbc = jdbc;
        this.tx = tx;
        this.calculator = calculator;
        this.chaos = chaos;
        this.workerId = workerId;
        this.completed = Counter.builder("swallow.worker.completed").register(registry);
        this.duplicateSkipped = Counter.builder("swallow.worker.duplicate_skipped").register(registry);
        this.redelivered = Counter.builder("swallow.worker.redelivered").register(registry);
        this.requeued = Counter.builder("swallow.worker.requeued").register(registry);
        this.deadLettered = Counter.builder("swallow.worker.dead_lettered").register(registry);
        this.processing = Timer.builder("swallow.worker.processing").publishPercentiles(0.5, 0.95, 0.99).register(registry);
    }

    record Job(String jobId, String merchantId, LocalDate settlementDate) {}

    @RabbitListener(queues = RabbitTopology.JOBS_QUEUE, ackMode = "MANUAL")
    public void onMessage(Message message, Channel channel) throws IOException {
        long tag = message.getMessageProperties().getDeliveryTag();
        if (Boolean.TRUE.equals(message.getMessageProperties().isRedelivered())) {
            redelivered.increment();
        }
        String body = new String(message.getBody(), StandardCharsets.UTF_8);
        String jobId = parseJobId(body);

        try {
            processing.record(() -> process(jobId));
            channel.basicAck(tag, false);
        } catch (TransientDataAccessException | DataAccessResourceFailureException e) {
            // DB 일시 장애: 잠시 뒤 큐로 되돌려 다시 처리한다. 커밋되지 않았으므로 안전하다.
            log.warn("transient DB failure on {}, requeue: {}", jobId, e.toString());
            requeued.increment();
            sleepQuietly(1000);
            channel.basicNack(tag, false, true);
        } catch (Exception e) {
            // 다시 해도 실패할 메시지(독성 메시지): DLQ 로 격리해 큐 전체가 막히지 않게 한다.
            log.error("unprocessable message {}, dead-lettering", body, e);
            deadLettered.increment();
            recordEventQuietly(jobId == null ? "UNKNOWN" : jobId, "DEAD_LETTERED", e.toString());
            channel.basicNack(tag, false, false);
        }
    }

    private void process(String jobId) {
        Job job = loadJob(jobId);

        // 1) 계산: 트랜잭션 밖. 여기서 회수되면 Ack 가 없으므로 메시지는 재전달된다.
        SettlementCalculator.Settlement s = calculator.calculate(job.merchantId(), job.settlementDate());
        chaos.maybeCrash(ChaosPolicy.Point.BEFORE_COMMIT, jobId);

        // 2) 확정: 짧은 트랜잭션. PK 충돌이면 이미 다른 워커가 확정한 작업이다.
        boolean firstCommit = Boolean.TRUE.equals(tx.execute(status -> {
            int inserted = jdbc.update("""
                    INSERT INTO settlement_results
                        (job_id, merchant_id, settlement_date, tx_count, gross_amount, fee_amount, net_amount, worker_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (job_id) DO NOTHING
                    """, job.jobId(), job.merchantId(), job.settlementDate(),
                    s.txCount(), s.gross(), s.fee(), s.net(), workerId);
            if (inserted == 1) {
                jdbc.update("UPDATE settlement_jobs SET status = 'COMPLETED', completed_at = now() WHERE job_id = ?", job.jobId());
                insertEvent(job.jobId(), "COMPLETED", "net=" + s.net());
                return true;
            }
            insertEvent(job.jobId(), "DUPLICATE_SKIPPED", "already settled");
            return false;
        }));

        if (firstCommit) {
            completed.increment();
        } else {
            duplicateSkipped.increment();
        }

        // 3) 커밋 직후, Ack 전에 회수되는 상황.
        chaos.maybeCrash(ChaosPolicy.Point.AFTER_COMMIT_BEFORE_ACK, jobId);
    }

    private Job loadJob(String jobId) {
        if (jobId == null) {
            throw new IllegalArgumentException("message has no jobId");
        }
        List<Job> jobs = jdbc.query(
                "SELECT job_id, merchant_id, settlement_date FROM settlement_jobs WHERE job_id = ?",
                (rs, i) -> new Job(rs.getString(1), rs.getString(2), rs.getObject(3, LocalDate.class)), jobId);
        if (jobs.isEmpty()) {
            throw new IllegalStateException("unknown job " + jobId);
        }
        return jobs.get(0);
    }

    private void insertEvent(String jobId, String type, String detail) {
        jdbc.update("INSERT INTO job_events (job_id, event_type, actor, detail) VALUES (?, ?, ?, ?)",
                jobId, type, workerId, detail);
    }

    private void recordEventQuietly(String jobId, String type, String detail) {
        try {
            insertEvent(jobId, type, detail);
        } catch (Exception ignored) {
            // 감사 기록 실패가 DLQ 격리를 막지 않게 한다.
        }
    }

    static String parseJobId(String body) {
        Matcher m = JOB_ID.matcher(body);
        return m.find() ? m.group(1) : null;
    }

    private static void sleepQuietly(long millis) {
        try {
            Thread.sleep(millis);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
}
