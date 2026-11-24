package io.spotswallow.api;

import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Properties;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import com.rabbitmq.client.GetResponse;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.amqp.core.AmqpAdmin;
import org.springframework.amqp.rabbit.core.RabbitAdmin;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.support.TransactionTemplate;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * DLQ 재처리. 원인을 고친 뒤 격리된 메시지를 다시 처리 경로에 올린다 (런북 docs/runbook.md#dlq).
 *
 * 메시지를 큐로 직접 다시 보내지 않고 아웃박스에 새로 적는다. 순서는
 *   1. DLQ 에서 Ack 없이 하나 꺼낸다 (basicGet, autoAck=false)
 *   2. 작업이 DB 에 있고 아직 완료 전이면 같은 트랜잭션에서 outbox 행과 REPLAYED 이벤트를 쓴다.
 *      DB 에 없는 작업(존재하지 않는 ID, 깨진 메시지)은 DLQ_DISCARDED 이벤트만 남기고 버린다.
 *   3. 커밋한 뒤에 DLQ 메시지를 Ack 한다.
 * 2와 3 사이에 죽으면 메시지가 DLQ 에 남아 다음 재처리 때 한 번 더 아웃박스에 적힌다. 그러면 같은 작업이
 * 두 번 발행되지만 결과 테이블의 job_id PK 가 두 번째 반영을 막는다. 반대로 Ack 를 먼저 하면 유실될 수 있다.
 */
@RestController
@RequestMapping("/admin/dlq")
public class DlqController {
    private static final Logger log = LoggerFactory.getLogger(DlqController.class);
    private static final Pattern JOB_ID = Pattern.compile("\"jobId\"\\s*:\\s*\"([^\"]+)\"");

    private final RabbitTemplate rabbit;
    private final AmqpAdmin admin;
    private final JdbcTemplate jdbc;
    private final TransactionTemplate tx;

    public DlqController(RabbitTemplate rabbit, AmqpAdmin admin, JdbcTemplate jdbc, TransactionTemplate tx) {
        this.rabbit = rabbit;
        this.admin = admin;
        this.jdbc = jdbc;
        this.tx = tx;
    }

    @GetMapping
    public Map<String, Object> status() {
        Properties p = admin.getQueueProperties(RabbitTopology.DLQ);
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("queue", RabbitTopology.DLQ);
        out.put("messages", p == null ? 0 : p.get(RabbitAdmin.QUEUE_MESSAGE_COUNT));
        out.put("deadLetteredJobs", jdbc.queryForObject(
                "SELECT count(DISTINCT job_id) FROM job_events WHERE event_type = 'DEAD_LETTERED'", Long.class));
        return out;
    }

    @PostMapping("/replay")
    public Map<String, Object> replay(@RequestParam(defaultValue = "100") int max) {
        List<String> replayed = new ArrayList<>();
        List<String> alreadyDone = new ArrayList<>();
        List<String> discarded = new ArrayList<>();
        for (int i = 0; i < max; i++) {
            Boolean got = rabbit.execute(channel -> {
                GetResponse r = channel.basicGet(RabbitTopology.DLQ, false);
                if (r == null) {
                    return false;
                }
                String body = new String(r.getBody(), StandardCharsets.UTF_8);
                Matcher m = JOB_ID.matcher(body);
                String jobId = m.find() ? m.group(1) : null;
                String outcome = tx.execute(status -> decide(jobId, body));
                switch (outcome) {
                    case "REPLAYED" -> replayed.add(jobId);
                    case "ALREADY_COMPLETED" -> alreadyDone.add(jobId);
                    default -> discarded.add(jobId == null ? body : jobId);
                }
                channel.basicAck(r.getEnvelope().getDeliveryTag(), false);   // DB 커밋 뒤에만 Ack
                return true;
            });
            if (!Boolean.TRUE.equals(got)) {
                break;
            }
        }
        log.info("DLQ replay: replayed={} alreadyCompleted={} discarded={}", replayed.size(), alreadyDone.size(),
                discarded.size());
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("replayed", replayed);
        out.put("alreadyCompleted", alreadyDone);
        out.put("discarded", discarded);
        return out;
    }

    private String decide(String jobId, String body) {
        String status = jobId == null ? null : jdbc.query(
                "SELECT status FROM settlement_jobs WHERE job_id = ? FOR UPDATE",
                rs -> rs.next() ? rs.getString(1) : null, jobId);
        if (status == null) {
            jdbc.update("INSERT INTO job_events (job_id, event_type, actor, detail) VALUES (?, 'DLQ_DISCARDED', 'dlq-replay', ?)",
                    jobId == null ? "UNKNOWN" : jobId, body.length() > 500 ? body.substring(0, 500) : body);
            return "DISCARDED";
        }
        if ("COMPLETED".equals(status)) {
            // 이미 끝난 작업: 다시 보낼 필요 없다 (보내도 멱등성이 막지만 불필요한 재계산을 피한다)
            jdbc.update("INSERT INTO job_events (job_id, event_type, actor) VALUES (?, 'DLQ_ALREADY_COMPLETED', 'dlq-replay')", jobId);
            return "ALREADY_COMPLETED";
        }
        jdbc.update("INSERT INTO outbox (job_id, payload) VALUES (?, ?)", jobId, "{\"jobId\":\"" + jobId + "\"}");
        jdbc.update("INSERT INTO job_events (job_id, event_type, actor) VALUES (?, 'REPLAYED', 'dlq-replay')", jobId);
        return "REPLAYED";
    }
}
