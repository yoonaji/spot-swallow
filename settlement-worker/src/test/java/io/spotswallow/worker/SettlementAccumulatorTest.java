package io.spotswallow.worker;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

import java.math.BigDecimal;
import java.util.Random;

import org.junit.jupiter.api.Test;

class SettlementAccumulatorTest {

    /** 어느 지점에서 끊고 저장했다가 이어 계산해도, 처음부터 한 번에 계산한 결과와 한 푼도 다르지 않아야 한다. */
    @Test
    void resumingFromAnyCheckpointGivesTheSameTotals() {
        Random rnd = new Random(42);
        int n = 300;
        long[] ids = new long[n];
        BigDecimal[] amounts = new BigDecimal[n];
        BigDecimal[] rates = new BigDecimal[n];
        long id = 1000;
        for (int i = 0; i < n; i++) {
            id += 1 + rnd.nextInt(5);   // tx_id 는 띄엄띄엄일 수 있다
            ids[i] = id;
            amounts[i] = BigDecimal.valueOf(100 + rnd.nextInt(5_000_000), 2);
            rates[i] = BigDecimal.valueOf(rnd.nextInt(400), 4);   // 0 ~ 3.99%
        }
        SettlementAccumulator full = new SettlementAccumulator();
        for (int i = 0; i < n; i++) {
            full.add(ids[i], amounts[i], rates[i]);
        }
        for (int cut = 0; cut <= n; cut++) {
            SettlementAccumulator first = new SettlementAccumulator();
            for (int i = 0; i < cut; i++) {
                first.add(ids[i], amounts[i], rates[i]);
            }
            // DB 에 NUMERIC(18,2) 로 저장했다 읽은 것처럼 scale 2 로 되돌린다
            SettlementAccumulator resumed = new SettlementAccumulator(first.lastTxId(), first.count(),
                    new BigDecimal(first.gross().toPlainString()), new BigDecimal(first.fee().toPlainString()));
            for (int i = cut; i < n; i++) {
                resumed.add(ids[i], amounts[i], rates[i]);
            }
            assertEquals(full.toSettlement(), resumed.toSettlement(), "cut at " + cut);
        }
    }

    @Test
    void rejectsOutOfOrderTransactions() {
        SettlementAccumulator acc = new SettlementAccumulator(10, 1, BigDecimal.ONE, BigDecimal.ZERO);
        assertThrows(IllegalStateException.class, () -> acc.add(10, BigDecimal.ONE, BigDecimal.ZERO));
    }
}
