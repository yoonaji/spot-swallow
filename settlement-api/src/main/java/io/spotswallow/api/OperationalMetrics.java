package io.spotswallow.api;

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
 */
@Component
public class OperationalMetrics {
    private final JdbcTemplate jdbc;
    private final AtomicLong oldestPendingJobAgeSeconds = new AtomicLong();
    private final AtomicLong pendingJobs = new AtomicLong();
    private final AtomicLong outboxPending = new AtomicLong();

    public OperationalMetrics(JdbcTemplate jdbc, MeterRegistry registry) {
        this.jdbc = jdbc;
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

    private long value(String sql) {
        Long v = jdbc.queryForObject(sql, Long.class);
        return v == null ? 0 : v;
    }
}
