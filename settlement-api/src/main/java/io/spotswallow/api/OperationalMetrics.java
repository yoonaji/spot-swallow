package io.spotswallow.api;

import java.util.Map;
import java.util.concurrent.atomic.AtomicLong;

import io.micrometer.core.instrument.Gauge;
import io.micrometer.core.instrument.MeterRegistry;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

/**
 * 알림에 쓰는 운영 지표.
 * - 가장 오래 기다린 미완료 작업의 나이: 완료시간 목표 위반을 미리 감지한다.
 * - 미발행 아웃박스 수: 발행기 장애를 감지한다.
 * - 대사 결과(유실·중복·금액 불일치, DLQ 격리 수): 데이터가 틀린 순간을 감지한다.
 *   대사 쿼리는 전체 거래를 다시 계산하므로 15초마다만 돌린다.
 */
@Component
public class OperationalMetrics {
    private final JdbcTemplate jdbc;
    private final AtomicLong oldestPendingJobAgeSeconds = new AtomicLong();
    private final AtomicLong pendingJobs = new AtomicLong();
    private final AtomicLong outboxPending = new AtomicLong();
    private final ReconciliationController reconciliation;
    private final AtomicLong statusResultMismatch = new AtomicLong();
    private final AtomicLong amountMismatch = new AtomicLong();
    private final AtomicLong doubleCompleted = new AtomicLong();
    private final AtomicLong deadLettered = new AtomicLong();
    private final AtomicLong stalePendingJobs = new AtomicLong();
    private final AtomicLong auditViolations = new AtomicLong();

    public OperationalMetrics(JdbcTemplate jdbc, MeterRegistry registry, ReconciliationController reconciliation) {
        this.jdbc = jdbc;
        this.reconciliation = reconciliation;
        Gauge.builder("swallow.recon.status_result_mismatch", statusResultMismatch, AtomicLong::get).register(registry);
        Gauge.builder("swallow.recon.amount_mismatch", amountMismatch, AtomicLong::get).register(registry);
        Gauge.builder("swallow.recon.double_completed", doubleCompleted, AtomicLong::get).register(registry);
        Gauge.builder("swallow.recon.dead_lettered_jobs", deadLettered, AtomicLong::get).register(registry);
        // 감사 체인이 깨졌거나(brokenLinks) 체인 밖에서 결과가 바뀐(untracked) 건수
        Gauge.builder("swallow.recon.audit_violations", auditViolations, AtomicLong::get).register(registry);
        Gauge.builder("swallow.recon.stale_pending_jobs", stalePendingJobs, AtomicLong::get).register(registry);
        Gauge.builder("swallow.jobs.oldest_pending_age_seconds", oldestPendingJobAgeSeconds, AtomicLong::get).register(registry);
        Gauge.builder("swallow.jobs.pending", pendingJobs, AtomicLong::get).register(registry);
        Gauge.builder("swallow.outbox.pending", outboxPending, AtomicLong::get).register(registry);
    }

    @Scheduled(fixedDelay = 5000)
    public void refresh() {
        oldestPendingJobAgeSeconds.set(value("""
                SELECT coalesce(extract(epoch FROM now() - min(accepted_at)), 0)::bigint
                  FROM settlement_jobs WHERE status <> 'COMPLETED'
                """));
        pendingJobs.set(value("SELECT count(*) FROM settlement_jobs WHERE status <> 'COMPLETED'"));
        outboxPending.set(value("SELECT count(*) FROM outbox WHERE status = 'PENDING'"));
    }

    @Scheduled(fixedDelay = 15000, initialDelay = 5000)
    public void refreshReconciliation() {
        Map<String, Object> r = reconciliation.reconcile();
        statusResultMismatch.set(((Number) r.get("statusResultMismatch")).longValue());
        amountMismatch.set(((Number) r.get("amountMismatch")).longValue());
        doubleCompleted.set(((Number) r.get("doubleCompleted")).longValue());
        deadLettered.set(((Number) r.get("deadLettered")).longValue());
        stalePendingJobs.set(((Number) r.get("stalePendingJobs")).longValue());
        Map<?, ?> audit = (Map<?, ?>) r.get("auditChain");
        auditViolations.set(((Number) audit.get("brokenLinks")).longValue() + ((Number) audit.get("untracked")).longValue()
                + ((Number) audit.get("anchorMismatch")).longValue());
    }

    private long value(String sql) {
        Long v = jdbc.queryForObject(sql, Long.class);
        return v == null ? 0 : v;
    }
}
