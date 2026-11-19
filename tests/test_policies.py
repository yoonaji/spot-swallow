"""제안 기법이 중간보고서 4.3절 예시 계산(표 2)을 그대로 재현하는지 확인한다."""
import math

import pytest

from capacity.estimators import DecayingRate, EvictionHistory, weighted_quantile
from capacity.policies import (Config, Context, Controller, FixedSpare, Proposed, RateBased,
                               min_cost_target, n_base)

LAM, MU = 1.8, 0.5
CFG = Config(slo=600, n_max=40, od_max=40)


@pytest.mark.parametrize("t_risk,f_p,expected", [
    (150, 0.05, 4),   # 평상시 20 시간
    (150, 0.50, 5),   # 회수 집중 3 시간
    (300, 0.50, 6),   # 회수 집중 + 복구 지연 1 시간
])
def test_paper_example_table2(t_risk, f_p, expected):
    (n, d), nb, ok = min_cost_target(LAM, MU, 0, t_risk, f_p, CFG, allow_ondemand=False, slo_check="drain")
    assert nb == 4 and ok and d == 0
    assert n == expected


def test_paper_example_daily_worker_hours():
    hours = 0
    for t_risk, f_p, h in [(150, 0.05, 20), (150, 0.5, 3), (300, 0.5, 1)]:
        (n, _), _, _ = min_cost_target(LAM, MU, 0, t_risk, f_p, CFG, allow_ondemand=False, slo_check="drain")
        hours += n * h
    assert hours == 101   # 고정 여유 2대는 144


def test_ondemand_used_when_everything_can_be_evicted():
    # 모두 함께 회수(f=1)되고 위험 구간이 SLO 에 가까우면 스팟만으로는 만족할 수 없다
    (n, d), _, ok = min_cost_target(LAM, MU, 0, 590, 1.0, CFG, slo_check="drain")
    assert ok and d >= 4
    (_, d0), _, ok0 = min_cost_target(LAM, MU, 0, 590, 1.0, CFG, allow_ondemand=False, slo_check="drain")
    assert not ok0 and d0 == 0


def test_nbase_counts_backlog():
    assert n_base(1.8, 0.5, 0, 300) == 4
    assert n_base(1.8, 0.5, 300, 300) == 6   # 3.6 + 300/(0.5*300) = 5.6


def ctx(**kw):
    base = dict(t=0, lam=LAM, mu=MU, queue=0, tready_q=120, tready_mean=90, f_p=0.05, f_mean=0.1,
                spot_alive=4, spot_available=True)
    base.update(kw)
    return Context(**base)


def test_scale_down_delay_keeps_recent_max():
    c = Controller(FixedSpare(0), Config(tdown=600))
    assert c.decide(ctx(t=0, lam=3.6)) == (8, 0)
    assert c.decide(ctx(t=300, lam=1.8)) == (8, 0)     # 600초 안이므로 유지
    assert c.decide(ctx(t=700, lam=1.8)) == (4, 0)


def test_unavailable_spot_is_replaced_by_ondemand():
    c = Controller(FixedSpare(2), Config(tdown=0))
    assert c.decide(ctx(spot_alive=3, spot_available=False)) == (6, 3)


def test_rate_based():
    assert RateBased().raw_target(ctx(f_mean=0.2), CFG) == (5, 0)   # 4 + ceil(4*0.25)


def test_proposed_uses_tail_not_mean():
    tail = Proposed().raw_target(ctx(tready_q=270, f_p=0.5), CFG)
    mean = Proposed(use_tready_tail=False).raw_target(ctx(tready_q=270, tready_mean=120, f_p=0.5), CFG)
    assert sum(tail) >= sum(mean)


def test_decaying_rate_converges():
    r = DecayingRate(half_life_s=60)
    for i in range(1, 2000):
        r.add(i * 0.5)          # 초당 2건
    assert r.rate(1000) == pytest.approx(2.0, rel=0.05)


def test_weighted_quantile():
    assert weighted_quantile([0, 0, 0, 1], [1, 1, 1, 1], 0.75) == 0
    assert weighted_quantile([0, 0, 0, 1], [1, 1, 1, 1], 0.80) == 1
    assert weighted_quantile([0, 1], [1, 9], 0.5) == 1


def test_eviction_history_fraction():
    # 4대 중 2대가 한 tick 에 회수 → f_w = 0.5
    ticks = [(i * 300, 4, 0) for i in range(30)] + [(30 * 300, 2, 2)] + [(i * 300, 4, 0) for i in range(31, 60)]
    h = EvictionHistory(ticks, 300, min_windows=10)
    f_p, f_mean = h.stats(60 * 300, 300, 0.99, weighted=False)
    assert f_p == 0.5 and math.isclose(f_mean, 0.5 / 58, rel_tol=0.1)


def test_wait_based_check_is_less_conservative_but_still_safe():
    from capacity.policies import max_wait
    # 회수 집중 예시(f=0.5, Trisk=150)에서 n=4 면 가장 늦은 작업도 약 1분 안에 끝난다
    # 가장 늦는 작업은 τ≈82초에 도착한 작업: 앞선 67건을 1.0건/초로 처리 + 자기 처리 2초
    assert max_wait(4, 0, 2, LAM, MU, 0, 150) == pytest.approx(69.8, abs=1.0)
    (n, d), _, _ = min_cost_target(LAM, MU, 0, 150, 0.5, CFG, allow_ondemand=False)
    assert n == 4
    # 위험 구간이 SLO 보다 길어도 적체가 작으면 만족할 수 있다 (식 8 원형은 적체 0 을 요구)
    (n8, _), _, _ = min_cost_target(LAM, MU, 0, 330, 0.33, Config(slo=300), allow_ondemand=False, slo_check="drain")
    (nw, _), _, _ = min_cost_target(LAM, MU, 0, 330, 0.33, Config(slo=300), allow_ondemand=False)
    assert nw < n8
    # 모두 회수되고 복구가 SLO 보다 길면 스팟만으로는 어떤 n 도 안 된다
    assert max_wait(8, 0, 8, LAM, MU, 0, 400) > 300
