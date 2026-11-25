package io.spotswallow.worker;

import java.math.BigDecimal;
import java.time.LocalDate;
import java.util.List;

import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.MeterRegistry;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;

/**
 * 정산 계산. 트랜잭션 밖에서 메모리로 계산한다 (장시간 DB 트랜잭션을 만들지 않기 위해).
 * 수수료는 거래 건별로 원 단위 이하 둘째 자리에서 반올림(HALF_UP)한 뒤 합산한다.
 *
 * 중간 저장 (swallow.worker.checkpoint-every-tx > 0):
 *   거래 N 건마다 (마지막 tx_id, 건수, 합계)를 job_checkpoints 에 저장한다. 회수로 다시 받은 작업은
 *   저장된 지점 다음 거래부터 계산한다. 저장은 짧은 단독 쓰기이고, 실패해도 계산은 계속한다(속도를 위한 장치).
 *   최종 결과 확정은 여전히 settlement_results PK 가 한 번만 허용하므로 정합성 조건은 바뀌지 않는다.
 */
@Component
public class SettlementCalculator {
    private static final Logger log = LoggerFactory.getLogger(SettlementCalculator.class);

    private final JdbcTemplate jdbc;
    private final long workMicrosPerTx;
    private final int checkpointEveryTx;
    private final String workerId;
    private final Counter checkpointsSaved;
    private final Counter resumed;
    private final Counter txSkipped;

    public SettlementCalculator(JdbcTemplate jdbc, MeterRegistry registry,
                                @Value("${swallow.worker.work-micros-per-tx:0}") long workMicrosPerTx,
                                @Value("${swallow.worker.checkpoint-every-tx:0}") int checkpointEveryTx,
                                @Value("${swallow.worker.id:worker}") String workerId) {
        this.jdbc = jdbc;
        this.workMicrosPerTx = workMicrosPerTx;
        this.checkpointEveryTx = checkpointEveryTx;
        this.workerId = workerId;
        this.checkpointsSaved = Counter.builder("swallow.worker.checkpoints_saved").register(registry);
        this.resumed = Counter.builder("swallow.worker.resumed_jobs").register(registry);
        this.txSkipped = Counter.builder("swallow.worker.resumed_tx_skipped").register(registry);
    }

    public record Settlement(int txCount, BigDecimal gross, BigDecimal fee, BigDecimal net) {}

    record Tx(long id, BigDecimal amount, BigDecimal feeRate) {}

    /** 중간 저장 없이 한 번에 계산한다. */
    public Settlement calculate(String merchantId, LocalDate settlementDate) {
        SettlementAccumulator acc = new SettlementAccumulator();
        for (Tx t : load(merchantId, settlementDate, 0)) {
            acc.add(t.id(), t.amount(), t.feeRate());
        }
        simulateHeavyWork(acc.count());
        return acc.toSettlement();
    }

    /** checkpoint-every-tx 가 0 이면 calculate(merchant, date) 와 같다. */
    public Settlement calculate(String jobId, String merchantId, LocalDate settlementDate) {
        if (checkpointEveryTx <= 0) {
            return calculate(merchantId, settlementDate);
        }
        SettlementAccumulator acc = loadCheckpoint(jobId);
        if (acc.count() > 0) {
            resumed.increment();
            txSkipped.increment(acc.count());
            log.info("RESUMED {} from checkpoint: {} tx already done", jobId, acc.count());
        }
        int sinceSave = 0;
        for (Tx t : load(merchantId, settlementDate, acc.lastTxId())) {
            acc.add(t.id(), t.amount(), t.feeRate());
            simulateHeavyWork(1);
            if (++sinceSave >= checkpointEveryTx) {
                saveCheckpoint(jobId, acc);
                sinceSave = 0;
            }
        }
        return acc.toSettlement();
    }

    private List<Tx> load(String merchantId, LocalDate settlementDate, long afterTxId) {
        // 거래를 먼저 다 읽어 커서를 닫는다. 긴 계산 동안 DB 연결을 잡고 있지 않기 위해서다.
        return jdbc.query("""
                SELECT tx_id, amount, fee_rate FROM transactions
                 WHERE merchant_id = ? AND settlement_date = ? AND tx_id > ?
                 ORDER BY tx_id
                """, (rs, i) -> new Tx(rs.getLong(1), rs.getBigDecimal(2), rs.getBigDecimal(3)),
                merchantId, settlementDate, afterTxId);
    }

    private SettlementAccumulator loadCheckpoint(String jobId) {
        List<SettlementAccumulator> rows = jdbc.query(
                "SELECT last_tx_id, tx_count, gross, fee FROM job_checkpoints WHERE job_id = ?",
                (rs, i) -> new SettlementAccumulator(rs.getLong(1), rs.getInt(2), rs.getBigDecimal(3), rs.getBigDecimal(4)),
                jobId);
        return rows.isEmpty() ? new SettlementAccumulator() : rows.get(0);
    }

    private void saveCheckpoint(String jobId, SettlementAccumulator acc) {
        try {
            // 앞으로만 간다: 더 많이 계산한 저장이 이미 있으면 덮어쓰지 않는다.
            jdbc.update("""
                    INSERT INTO job_checkpoints (job_id, last_tx_id, tx_count, gross, fee, worker_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (job_id) DO UPDATE
                       SET last_tx_id = EXCLUDED.last_tx_id, tx_count = EXCLUDED.tx_count,
                           gross = EXCLUDED.gross, fee = EXCLUDED.fee,
                           worker_id = EXCLUDED.worker_id, updated_at = now()
                     WHERE job_checkpoints.tx_count < EXCLUDED.tx_count
                    """, jobId, acc.lastTxId(), acc.count(), acc.gross(), acc.fee(), workerId);
            checkpointsSaved.increment();
        } catch (Exception e) {
            log.warn("checkpoint save failed for {} (continuing without it): {}", jobId, e.toString());
        }
    }

    /** 실제 정산의 무거운 계산(검증, 환율, 규칙 적용 등)을 흉내 내는 처리 시간. 실험에서 처리율 μ 를 조절한다. */
    private void simulateHeavyWork(int txCount) {
        long micros = workMicrosPerTx * txCount;
        if (micros <= 0) {
            return;
        }
        try {
            Thread.sleep(micros / 1000, (int) (micros % 1000) * 1000);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
}
