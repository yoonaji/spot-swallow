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
        // 마감 등급별 마감 초과 (완료가 마감보다 늦었거나, 아직 안 끝났는데 마감이 지남)
        Map<String, Object> deadlines = new LinkedHashMap<>();
        jdbc.query("""
                SELECT priority, count(*),
                       count(*) FILTER (WHERE deadline_at IS NOT NULL
                                          AND coalesce(completed_at, now()) > deadline_at)
                  FROM settlement_jobs GROUP BY priority ORDER BY priority
                """, rs -> {
            deadlines.put(rs.getString(1), Map.of("jobs", rs.getLong(2), "missed", rs.getLong(3)));
        });
        report.put("deadlineByPriority", deadlines);
        Map<String, Object> audit = auditChain();
        report.put("auditChain", audit);
        boolean auditOk = Boolean.TRUE.equals(audit.get("ok"));
        report.put("consistent", statusResultMismatch == 0 && amountMismatch == 0 && doubleCompleted == 0 && auditOk);
        report.put("allSettled", pendingJobs == 0 && results == acceptedJobs);
        return report;
    }

    /**
     * 결과 감사 체인 검사 (V3).
     * - brokenLinks: 해시를 다시 계산했을 때 다르거나, 앞 줄 해시와 이어지지 않는 줄. 감사 기록 자체가 고쳐졌다는 뜻이다.
     * - untracked: 지금 결과 값이 그 작업의 마지막 감사 기록과 다르다. 트리거를 끄고 몰래 바꿨다는 뜻이다.
     * - changes: 확정 뒤에 결과가 바뀌거나 지워진 기록 (누가 바꿨는지 db_user 로 남는다). 정정일 수 있으므로 ok 에는 넣지 않고,
     *   금액이 틀렸다면 amountMismatch 가 따로 잡는다.
     */
    private Map<String, Object> auditChain() {
        Map<String, Object> a = new LinkedHashMap<>();
        long entries = count("SELECT count(*) FROM result_audit");
        long brokenLinks = count("""
                SELECT count(*) FROM (
                    SELECT hash, prev_hash,
                           result_audit_hash(prev_hash, seq, job_id, action, payload, db_user, recorded_at) AS recomputed,
                           coalesce(lag(hash) OVER (ORDER BY seq), repeat('0', 64)) AS expected_prev
                      FROM result_audit) c
                 WHERE c.hash <> c.recomputed OR c.prev_hash <> c.expected_prev
                """);
        long untracked = count("""
                WITH last AS (
                    SELECT DISTINCT ON (job_id) job_id, payload FROM result_audit ORDER BY job_id, seq DESC)
                SELECT count(*) FROM settlement_results r
                  FULL JOIN last l ON l.job_id = r.job_id
                 WHERE (CASE WHEN r.job_id IS NULL THEN 'DELETED' ELSE result_payload(r) END)
                       IS DISTINCT FROM coalesce(l.payload, '(none)')
                """);
        Map<String, Object> changes = new LinkedHashMap<>();
        jdbc.query("""
                SELECT action, db_user, count(*) FROM result_audit WHERE action <> 'INSERT'
                 GROUP BY action, db_user ORDER BY action, db_user
                """, rs -> {
            changes.put(rs.getString(1) + " by " + rs.getString(2), rs.getLong(3));
        });
        String head = jdbc.query("SELECT hash FROM result_audit ORDER BY seq DESC LIMIT 1",
                rs -> rs.next() ? rs.getString(1) : null);
        a.put("entries", entries);
        a.put("brokenLinks", brokenLinks);
        a.put("untracked", untracked);
        a.put("changesAfterCommit", changes);
        a.put("headHash", head);
        a.put("ok", brokenLinks == 0 && untracked == 0);
        return a;
    }

    private long count(String sql) {
        Long value = jdbc.queryForObject(sql, Long.class);
        return value == null ? 0 : value;
    }
}
