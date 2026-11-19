package io.spotswallow.api;

import java.time.LocalDate;
import java.util.Map;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * 실험용 합성 거래 데이터 적재. 같은 seed 면 같은 데이터가 만들어진다.
 * 가맹점마다 거래 수가 다르다 (평균 txPerMerchantDay, 범위 ±spread). 큰 가맹점과 작은 가맹점이 섞여
 * 작업별 처리 시간에 편차가 생긴다. spread=0 이면 모든 가맹점이 같은 거래 수를 가진다.
 */
@RestController
@RequestMapping("/admin")
public class AdminController {
    private final JdbcTemplate jdbc;

    public AdminController(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    @PostMapping("/seed")
    @Transactional
    public Map<String, Object> seed(@RequestParam(defaultValue = "50") int merchants,
                                    @RequestParam(defaultValue = "2") int days,
                                    @RequestParam(defaultValue = "200") int txPerMerchantDay,
                                    @RequestParam(defaultValue = "2026-11-01") LocalDate startDate,
                                    @RequestParam(defaultValue = "0.8") double spread,
                                    @RequestParam(defaultValue = "0.42") double seed) {
        if (spread < 0 || spread >= 1) {
            throw new IllegalArgumentException("spread must be in [0, 1)");
        }
        jdbc.queryForList("SELECT setseed(?)", seed);
        int inserted = jdbc.update("""
                INSERT INTO transactions (merchant_id, settlement_date, amount, fee_rate, occurred_at)
                SELECT 'M' || lpad(m::text, 4, '0'),
                       (?::date + d),
                       round((1000 + random() * 199000)::numeric, 2),
                       0.0150 + (m % 5) * 0.0025,
                       (?::date + d) + random() * interval '1 day'
                  FROM (SELECT m, d,
                               greatest(1, round(? * (1 - ? + 2 * ? * random())))::int AS n
                          FROM generate_series(1, ?) m,
                               generate_series(0, ? - 1) d) sizes,
                       LATERAL generate_series(1, sizes.n) t
                """, startDate, startDate, txPerMerchantDay, spread, spread, merchants, days);
        return Map.of("insertedTransactions", inserted, "merchants", merchants, "days", days);
    }
}
