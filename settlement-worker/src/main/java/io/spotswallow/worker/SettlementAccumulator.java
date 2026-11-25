package io.spotswallow.worker;

import java.math.BigDecimal;
import java.math.RoundingMode;

/**
 * 정산 합계를 거래 순서(tx_id 오름차순)대로 쌓는다. 수수료는 거래 건별로 소수 둘째 자리에서 반올림(HALF_UP)한 뒤 더한다.
 *
 * 중간 저장(job_checkpoints)과 이어 계산의 정확성은 이 클래스에 달려 있다:
 * "앞부분 합계 + 뒷부분을 이어서 더한 값" 이 "처음부터 한 번에 더한 값" 과 같아야 한다.
 * 금액이 소수 둘째 자리까지인 BigDecimal 덧셈이므로 어디서 끊어도 결과가 같다 (SettlementAccumulatorTest).
 */
final class SettlementAccumulator {
    private long lastTxId;
    private int count;
    private BigDecimal gross;
    private BigDecimal fee;

    SettlementAccumulator() {
        this(0, 0, BigDecimal.ZERO, BigDecimal.ZERO);
    }

    /** 저장된 중간 합계에서 이어 간다. */
    SettlementAccumulator(long lastTxId, int count, BigDecimal gross, BigDecimal fee) {
        this.lastTxId = lastTxId;
        this.count = count;
        this.gross = gross;
        this.fee = fee;
    }

    void add(long txId, BigDecimal amount, BigDecimal feeRate) {
        if (txId <= lastTxId) {
            throw new IllegalStateException("transactions must be added in tx_id order: " + txId + " <= " + lastTxId);
        }
        gross = gross.add(amount);
        fee = fee.add(amount.multiply(feeRate).setScale(2, RoundingMode.HALF_UP));
        count++;
        lastTxId = txId;
    }

    long lastTxId() {
        return lastTxId;
    }

    int count() {
        return count;
    }

    BigDecimal gross() {
        return gross.setScale(2, RoundingMode.UNNECESSARY);
    }

    BigDecimal fee() {
        return fee.setScale(2, RoundingMode.UNNECESSARY);
    }

    SettlementCalculator.Settlement toSettlement() {
        return new SettlementCalculator.Settlement(count, gross(), fee(), gross().subtract(fee()));
    }
}
