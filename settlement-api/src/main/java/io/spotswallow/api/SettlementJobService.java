package io.spotswallow.api;

import java.time.LocalDate;
import java.time.format.DateTimeFormatter;
import java.util.Map;
import java.util.Optional;

import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.MeterRegistry;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/**
 * 정산 작업 접수.
 * 작업 저장과 아웃박스 기록을 하나의 DB 트랜잭션으로 묶는다.
 * 그래서 "DB 에는 작업이 있는데 큐에는 메시지가 없는" 상태가 생기지 않는다.
 */
@Service
public class SettlementJobService {
    private static final DateTimeFormatter ID_DATE = DateTimeFormatter.BASIC_ISO_DATE;

    private final JdbcTemplate jdbc;
    private final Counter accepted;
    private final Counter duplicateRequests;

    public SettlementJobService(JdbcTemplate jdbc, MeterRegistry registry) {
        this.jdbc = jdbc;
        this.accepted = Counter.builder("swallow.api.jobs.accepted").register(registry);
        this.duplicateRequests = Counter.builder("swallow.api.jobs.duplicate_requests").register(registry);
    }

    public record AcceptResult(String jobId, boolean created) {}

    /** 같은 가맹점, 같은 정산일 요청은 같은 job_id 가 되므로 두 번 접수되지 않는다. */
    public static String jobIdOf(String merchantId, LocalDate settlementDate) {
        return "S-" + merchantId + "-" + settlementDate.format(ID_DATE);
    }

    @Transactional
    public AcceptResult accept(String merchantId, LocalDate settlementDate) {
        String jobId = jobIdOf(merchantId, settlementDate);
        int inserted = jdbc.update("""
                INSERT INTO settlement_jobs (job_id, merchant_id, settlement_date, status)
                VALUES (?, ?, ?, 'ACCEPTED')
                ON CONFLICT (job_id) DO NOTHING
                """, jobId, merchantId, settlementDate);
        if (inserted == 0) {
            duplicateRequests.increment();
            return new AcceptResult(jobId, false);
        }
        jdbc.update("INSERT INTO outbox (job_id, payload) VALUES (?, ?)",
                jobId, "{\"jobId\":\"" + jobId + "\"}");
        jdbc.update("INSERT INTO job_events (job_id, event_type, actor) VALUES (?, 'ACCEPTED', 'api')", jobId);
        accepted.increment();
        return new AcceptResult(jobId, true);
    }

    public Optional<Map<String, Object>> find(String jobId) {
        var rows = jdbc.queryForList("""
                SELECT j.job_id, j.merchant_id, j.settlement_date, j.status, j.accepted_at, j.completed_at,
                       r.tx_count, r.gross_amount, r.fee_amount, r.net_amount, r.worker_id
                  FROM settlement_jobs j
                  LEFT JOIN settlement_results r ON r.job_id = j.job_id
                 WHERE j.job_id = ?
                """, jobId);
        return rows.stream().findFirst();
    }
}
