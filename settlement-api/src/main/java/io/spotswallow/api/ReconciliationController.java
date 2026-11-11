package io.spotswallow.api;

import java.util.LinkedHashMap;
import java.util.Map;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * 대사(reconciliation).
 * 처리 경로(워커)와 독립적으로, 입력 거래에서 정산 금액을 SQL 로 다시 계산해 확정 결과와 비교한다.
 * 유실, 중복, 금액 불일치가 하나라도 있으면 consistent=false.
 */
@RestController
public class ReconciliationController {
    private final JdbcTemplate jdbc;

    public ReconciliationController(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    @GetMapping("/reconciliation")
    public Map<String, Object> reconcile() {
        Map<String, Object> report = new LinkedHashMap<>();
        long acceptedJobs = count("SELECT count(*) FROM settlement_jobs");
        long completedJobs = count("SELECT count(*) FROM settlement_jobs WHERE status = 'COMPLETED'");
        long results = count("SELECT count(*) FROM settlement_results");
        long pendingJobs = acceptedJobs - completedJobs;
        long stalePendingJobs = count("""
                SELECT count(*) FROM settlement_jobs
                 WHERE status <> 'COMPLETED' AND accepted_at < now() - interval '10 minutes'
                """);
        // 상태와 결과가 어긋난 작업 (COMPLETED 인데 결과 없음, 또는 결과가 있는데 COMPLETED 아님)
        long statusResultMismatch = count("""
                SELECT count(*) FROM settlement_jobs j
                  LEFT JOIN settlement_results r ON r.job_id = j.job_id
                 WHERE (j.status = 'COMPLETED') <> (r.job_id IS NOT NULL)
                """);
        // 입력 거래로 다시 계산한 금액과 확정 결과가 다른 작업
        long amountMismatch = count("""
                SELECT count(*)
                  FROM settlement_results r
                  LEFT JOIN (SELECT merchant_id, settlement_date,
                                    count(*)                       AS tx_count,
                                    sum(amount)                    AS gross,
                                    sum(round(amount * fee_rate, 2)) AS fee
                               FROM transactions
                              GROUP BY merchant_id, settlement_date) t
                    ON t.merchant_id = r.merchant_id AND t.settlement_date = r.settlement_date
                 WHERE r.tx_count     <> coalesce(t.tx_count, 0)
                    OR r.gross_amount <> coalesce(t.gross, 0)
                    OR r.fee_amount   <> coalesce(t.fee, 0)
                """);
        // 결과 확정(COMPLETED 이벤트)이 두 번 이상 일어난 작업. 멱등성이 깨졌다는 뜻이다.
        long doubleCompleted = count("""
                SELECT count(*) FROM (
                    SELECT job_id FROM job_events WHERE event_type = 'COMPLETED'
                     GROUP BY job_id HAVING count(*) > 1) d
                """);
        long duplicateDeliveriesAbsorbed = count("SELECT count(*) FROM job_events WHERE event_type = 'DUPLICATE_SKIPPED'");
        long deadLettered = count("SELECT count(DISTINCT job_id) FROM job_events WHERE event_type = 'DEAD_LETTERED'");
        long outboxPending = count("SELECT count(*) FROM outbox WHERE status = 'PENDING'");

        report.put("acceptedJobs", acceptedJobs);
        report.put("completedJobs", completedJobs);
        report.put("results", results);
        report.put("pendingJobs", pendingJobs);
        report.put("stalePendingJobs", stalePendingJobs);
        report.put("statusResultMismatch", statusResultMismatch);
        report.put("amountMismatch", amountMismatch);
        report.put("doubleCompleted", doubleCompleted);
        report.put("duplicateDeliveriesAbsorbed", duplicateDeliveriesAbsorbed);
        report.put("deadLettered", deadLettered);
        report.put("outboxPending", outboxPending);
        report.put("consistent", statusResultMismatch == 0 && amountMismatch == 0 && doubleCompleted == 0);
        report.put("allSettled", pendingJobs == 0 && results == acceptedJobs);
        return report;
    }

    private long count(String sql) {
        Long value = jdbc.queryForObject(sql, Long.class);
        return value == null ? 0 : value;
    }
}
