"""
여유 용량 제어 정책. 모든 정책은 Δ 초마다 (스팟 목표 대수, 온디맨드 목표 대수)를 낸다.

제안 기법 (중간보고서 4.2절, 알고리즘 1):
  (3) λ, μ  ← 지수 감쇠 평균
  (4) Nbase = ⌈λ/μ + Q/(μ·Tdrain)⌉
  (5) Trisk = q_p(Tready) + Δ
  (6) K(n)  = ⌈f_p · n⌉,  f_p = 길이 Trisk 구간별 동시 회수 비율 f_w 의 p 분위수
  (7) Qend  = max(0, Q + K + λ·Trisk − (n − K + d)·μ·Trisk)
            위험 구간 동안 K 대가 회수되어 남은 워커로 처리하고, 회수된 워커가 처리하던 K 건이 큐로 돌아온다.
  (8) Qend ≤ ((n + d)·μ − λ) · (SLO − Trisk)
            대체 워커가 합류한 뒤 남은 시간 안에 적체를 해소할 수 있어야 한다.
  (8') 2주차 수정: 위험 구간 중 도착한 작업의 실제 대기 시간(FIFO)이 SLO 이하. max_wait() 참고.
  (9) 축소 지연: 늘릴 때는 바로, 줄일 때는 최근 Tdown 동안의 최댓값을 유지한다.

1주차 trace 분석에서 다중 노드 풀의 f_w q99 가 1.0 이었으므로 (모두 함께 회수), 스팟만으로 (8)을
만족할 수 없는 경우가 있다. 그래서 온디맨드 d 대를 함께 탐색해 비용이 가장 작은 (n, d)를 고른다.
d = 0 이면 원래 알고리즘 1 과 같다.
"""
import math
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class Prices:
    spot: float = 0.037   # c6i.large 서울 리전 스팟 가격 가정 ($/h), EC2 측정 때 실제 값으로 바꾼다
    ondemand: float = 0.107


@dataclass
class Context:
    """제어 주기마다 정책에 넘기는 관측값."""
    t: float
    lam: float
    mu: float
    queue: int                     # 대기 중인 작업 수 Q
    tready_q: float                # q_p(Tready)
    tready_mean: float
    f_p: float                     # q_p(f_w), 구간 길이 = Trisk
    f_mean: float                  # 평균 f_w (회수율 기반 정책용)
    spot_alive: int                # 살아 있는 스팟 워커(부팅 중 포함)
    spot_available: bool           # 시장에 스팟 용량이 있는지 (최근 생성 실패 여부)
    future_f: Optional[float] = None  # 오라클 전용: 실제로 다음 Trisk 동안 회수될 비율


@dataclass
class Config:
    delta: float = 30.0
    slo: float = 600.0
    tdrain: float = 300.0
    n_max: int = 40
    od_max: int = 40
    tdown: float = 600.0
    p: float = 0.95
    prices: Prices = field(default_factory=Prices)


def n_base(lam, mu, q, tdrain):
    """식 (4)."""
    if mu <= 0:
        return 1
    return max(1, math.ceil(lam / mu + q / (mu * tdrain) - 1e-9))


def backlog_after_risk(n, d, k, lam, mu, q, t_risk, reprocess=True):
    """식 (7)."""
    m = n - k + d
    return max(0.0, q + (k if reprocess else 0) + lam * t_risk - m * mu * t_risk)


def satisfies_slo(n, d, q_end, lam, mu, t_risk, slo):
    """식 (8)."""
    total = n + d
    if q_end <= 0 and total * mu >= lam:
        return True
    if total * mu <= lam or slo <= t_risk:
        return False
    return q_end <= (total * mu - lam) * (slo - t_risk)


def max_wait(n, d, k, lam, mu, q, t_risk, reprocess=True, steps=20):
    """
    식 (8') — 2주차 시뮬레이션에서 고친 조건. 위험 구간 중 시각 τ 에 도착한 작업의 대기 시간을 FIFO 로 계산해
    가장 오래 기다리는 작업의 완료시간(대기 + 처리 1건)을 낸다.
      앞선 작업 A(τ) = max(0, Q + K + (λ − mμ)τ),  위험 구간 동안 처리율 mμ, 끝난 뒤 (n + d)μ.
    식 (8)은 "위험 구간 내내 기다린 작업"을 가정해 Trisk ≥ SLO 이면 적체 0 을 요구하므로 지나치게 보수적이다.
    """
    m = n - k + d
    r1, r2 = m * mu, (n + d) * mu
    if r2 <= lam or mu <= 0:
        return math.inf
    worst = 0.0
    a0 = q + (k if reprocess else 0)
    for i in range(steps + 1):
        tau = t_risk * i / steps
        ahead = max(0.0, a0 + (lam - r1) * tau)
        rest = t_risk - tau
        if ahead <= r1 * rest:
            w = ahead / r1 if r1 > 0 else 0.0
        else:
            w = rest + (ahead - r1 * rest) / r2
        worst = max(worst, w)
    return worst + 1.0 / mu


def min_cost_target(lam, mu, q, t_risk, f_p, cfg: Config, reprocess=True, allow_ondemand=True,
                    spot_cap=None, slo_check="wait"):
    """
    알고리즘 1 의 탐색. (스팟 n, 온디맨드 d) 중 식 (8)을 만족하는 가장 싼 조합.
    spot_cap: 스팟 용량이 없을 때 지금 살아 있는 대수 이상은 늘릴 수 없다.
    만족하는 조합이 없으면 가능한 최대 용량을 낸다.
    """
    nb = n_base(lam, mu, q, cfg.tdrain)
    n_hi = cfg.n_max if spot_cap is None else min(cfg.n_max, spot_cap)
    best = None
    for d in range(0, (cfg.od_max if allow_ondemand else 0) + 1):
        for n in range(max(0, nb - d), n_hi + 1):
            k = math.ceil(f_p * n - 1e-9)
            if slo_check == "wait":
                ok = max_wait(n, d, k, lam, mu, q, t_risk, reprocess) <= cfg.slo
            else:
                q_end = backlog_after_risk(n, d, k, lam, mu, q, t_risk, reprocess)
                ok = satisfies_slo(n, d, q_end, lam, mu, t_risk, cfg.slo)
            if ok:
                cost = n * cfg.prices.spot + d * cfg.prices.ondemand
                if best is None or cost < best[0] - 1e-12:
                    best = (cost, n, d)
                break  # 같은 d 에서는 처음 만족하는 n 이 가장 싸다
        if best is not None and d * cfg.prices.ondemand >= best[0]:
            break      # 온디맨드만으로도 이미 더 비싸면 더 볼 필요가 없다
    if best is None:
        return (n_hi, cfg.od_max if allow_ondemand else 0), nb, False
    return (best[1], best[2]), nb, True


# ---------------------------------------------------------------- 정책들

class Policy:
    name = "policy"
    fallback = True        # 스팟 용량이 없을 때 모자란 만큼 온디맨드로 대체 (모든 비교 정책 공통)
    scale_down_delay = True

    def raw_target(self, ctx: Context, cfg: Config):
        raise NotImplementedError


class QueueReactive(Policy):
    """큐 길이 반응형 (KEDA 식): 여유 용량 없이 Nbase 만 맞춘다."""
    name = "queue-reactive"

    def raw_target(self, ctx, cfg):
        return n_base(ctx.lam, ctx.mu, ctx.queue, cfg.tdrain), 0


class FixedSpare(Policy):
    """고정 여유 용량: Nbase + k."""

    def __init__(self, k):
        self.k = k
        self.name = f"fixed-{k}"

    def raw_target(self, ctx, cfg):
        return n_base(ctx.lam, ctx.mu, ctx.queue, cfg.tdrain) + self.k, 0


class RateBased(Policy):
    """
    회수율 기반 (Snape 계열): 평균 회수율 e 로 n = Nbase + ⌈Nbase · e / (1 − e)⌉.
    복구 시간의 꼬리와 동시 회수를 보지 않는다.
    """
    name = "rate-based"

    def raw_target(self, ctx, cfg):
        nb = n_base(ctx.lam, ctx.mu, ctx.queue, cfg.tdrain)
        e = min(0.9, ctx.f_mean)
        return nb + math.ceil(nb * e / (1 - e) - 1e-9), 0


class OnDemandOnly(Policy):
    """온디맨드 전용: 회수가 없으므로 Nbase 만 온디맨드로. 비용 상한 기준."""
    name = "on-demand-only"
    fallback = False

    def raw_target(self, ctx, cfg):
        return 0, n_base(ctx.lam, ctx.mu, ctx.queue, cfg.tdrain)


class Proposed(Policy):
    """
    제안 기법. ablation 스위치:
      use_tready_tail=False  → Trisk 에 q_p(Tready) 대신 평균 Tready 사용
      use_concurrency=False  → f_p 대신 평균 회수 비율 사용 (동시 회수 무시)
      reprocess=False        → 식 (7)에서 회수된 작업이 큐로 돌아오는 것(+K)을 무시
      ondemand=False         → 선제적 온디맨드 대체 없이 스팟만 탐색 (원래 알고리즘 1)
      delay=False            → 축소 지연(식 9) 끔
      slo_check="drain"      → 중간보고서의 식 (8) 원형 (기본값은 대기 시간 기반 식 (8'))
    """

    def __init__(self, name="proposed", use_tready_tail=True, use_concurrency=True, reprocess=True,
                 ondemand=True, delay=True, oracle=False, slo_check="wait"):
        self.slo_check = slo_check
        self.name = name
        self.use_tready_tail = use_tready_tail
        self.use_concurrency = use_concurrency
        self.reprocess = reprocess
        self.ondemand = ondemand
        self.scale_down_delay = delay
        self.oracle = oracle

    def t_risk(self, ctx, cfg):
        return (ctx.tready_q if self.use_tready_tail else ctx.tready_mean) + cfg.delta

    def raw_target(self, ctx, cfg):
        t_risk = self.t_risk(ctx, cfg)
        if self.oracle and ctx.future_f is not None:
            f = ctx.future_f
        else:
            f = ctx.f_p if self.use_concurrency else ctx.f_mean
        cap = None if ctx.spot_available else ctx.spot_alive
        (n, d), _, _ = min_cost_target(ctx.lam, ctx.mu, ctx.queue, t_risk, f, cfg,
                                       reprocess=self.reprocess, allow_ondemand=self.ondemand,
                                       spot_cap=cap, slo_check=self.slo_check)
        return n, d


class AwsCapacityRebalance(QueueReactive):
    """
    AWS 기본 기능 기준선: Auto Scaling 그룹의 Capacity Rebalancing.
    여유 용량은 따로 두지 않고(대상 추적 스케일링 = Nbase), 회수 경고(재조정 권고 또는 2분 전 중단 알림)를
    받는 즉시 대체 인스턴스를 요청한다. 경고가 오는 시점은 시나리오의 notice_s 로 정한다.
    """
    name = "aws-rebalance"
    uses_notice = True


class ProposedWithNotice(Proposed):
    """제안 기법 + 회수 경고 시 즉시 대체 (둘은 함께 쓸 수 있다)."""
    uses_notice = True

    def __init__(self, **kw):
        super().__init__(name="proposed+rebalance", **kw)


class Controller:
    """정책 + 공통 장치(스팟 용량 부족 시 대체, 축소 지연). 시뮬레이터와 실제 제어기가 함께 쓴다."""

    def __init__(self, policy: Policy, cfg: Config):
        self.policy = policy
        self.cfg = cfg
        self.history = deque()  # (t, spot, od)

    def decide(self, ctx: Context):
        spot, od = self.policy.raw_target(ctx, self.cfg)
        if self.policy.fallback and not ctx.spot_available and spot > ctx.spot_alive:
            od += spot - ctx.spot_alive   # 만들 수 없는 스팟만큼 온디맨드로 채운다
        if self.policy.scale_down_delay:
            self.history.append((ctx.t, spot, od))
            while self.history and self.history[0][0] < ctx.t - self.cfg.tdown:
                self.history.popleft()
            spot = max(s for _, s, _ in self.history)
            od = max(o for _, _, o in self.history)
        return spot, od


def standard_policies():
    """비교 실험에 쓰는 정책 목록."""
    return [
        QueueReactive(), FixedSpare(1), FixedSpare(2), FixedSpare(4), RateBased(), OnDemandOnly(),
        AwsCapacityRebalance(), Proposed(), ProposedWithNotice(), Proposed(name="oracle", oracle=True),
    ]


def ablation_policies():
    return [
        Proposed(),
        Proposed(name="-wait-based-slo", slo_check="drain"),
        Proposed(name="-tready-tail", use_tready_tail=False),
        Proposed(name="-concurrency", use_concurrency=False),
        Proposed(name="-reprocess", reprocess=False),
        Proposed(name="-ondemand", ondemand=False),
        Proposed(name="-scale-down-delay", delay=False),
    ]
