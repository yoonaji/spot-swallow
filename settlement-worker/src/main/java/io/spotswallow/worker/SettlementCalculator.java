package io.spotswallow.worker;

import java.math.BigDecimal;
import java.math.RoundingMode;
import java.time.LocalDate;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;

/**
 * 정산 계산. 트랜잭션 밖에서 메모리로 계산한다 (장시간 DB 트랜잭션을 만들지 않기 위해).
 * 수수료는 거래 건별로 원 단위 이하 둘째 자리에서 반올림(HALF_UP)한 뒤 합산한다.
 */
@Component
public class SettlementCalculator {
    private final JdbcTemplate jdbc;
    private final long workMicrosPerTx;

    public SettlementCalculator(JdbcTemplate jdbc,
                                @Value("${swallow.worker.work-micros-per-tx:0}") long workMicrosPerTx) {
        this.jdbc = jdbc;
        this.workMicrosPerTx = workMicrosPerTx;
    }

    public record Settlement(int txCount, BigDecimal gross, BigDecimal fee, BigDecimal net) {}

    public Settlement calculate(String merchantId, LocalDate settlementDate) {
        BigDecimal[] totals = {BigDecimal.ZERO, BigDecimal.ZERO};
        int[] count = {0};
        jdbc.query("""
                SELECT amount, fee_rate FROM transactions
                 WHERE merchant_id = ? AND settlement_date = ?
                """, rs -> {
            BigDecimal amount = rs.getBigDecimal(1);
            BigDecimal fee = amount.multiply(rs.getBigDecimal(2)).setScale(2, RoundingMode.HALF_UP);
            totals[0] = totals[0].add(amount);
            totals[1] = totals[1].add(fee);
            count[0]++;
        }, merchantId, settlementDate);

        simulateHeavyWork(count[0]);
        BigDecimal gross = totals[0].setScale(2, RoundingMode.UNNECESSARY);
        BigDecimal fee = totals[1].setScale(2, RoundingMode.UNNECESSARY);
        return new Settlement(count[0], gross, fee, gross.subtract(fee));
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
