"""
이산 사건 시뮬레이터: 스팟 워커가 회수되는 비동기 작업 큐.

실제 시스템(settlement-api + RabbitMQ + settlement-worker)의 동작을 다음처럼 옮겼다.
  - 작업 도착: 포아송 과정, 도착률 λ(t) (부하 급증 구간은 배수).
  - 처리: 워커 한 대가 한 번에 한 건 (prefetch=1). 처리 시간은 로그정규 분포, 평균 1/μ, 변동계수 CV.
  - 회수: 공개 trace 의 tick 별 회수 비율을 지금 살아 있는 스팟 워커 수에 곱해(확률적 반올림) 그만큼을
    무작위로 골라 tick 안의 균등한 시각에 종료한다. 처리 중이던 작업은 Ack 가 없으므로 큐 앞으로 돌아간다(수동 Ack).
  - 스팟 용량: trace 의 가용 수가 0 인 동안은 스팟을 새로 만들 수 없다.
  - 복구: 생성 요청 후 Tready 가 지나야 처리에 참여한다. Tready 는 로그정규 (중앙값, σ) × 배수.
    온디맨드도 같은 Tready 를 가지지만 회수되지 않는다.
  - 제어: Δ 초마다 제어기가 (스팟, 온디맨드) 목표 대수를 내고, 모자라면 생성, 남으면
    부팅 중 → 놀고 있는 → 처리 중(작업을 마친 뒤 종료) 순서로 줄인다.
  - 비용: 생성 요청부터 종료(회수)까지 초 단위 과금.
"""
import heapq
import math
import random
from collections import deque
from dataclasses import dataclass, field

from capacity.estimators import DecayingMean, DecayingRate, EvictionHistory, QuantileHistory
from capacity.policies import Config, Context, Controller, Policy
from sim import traces

ARRIVAL, DONE, READY, EVICT_TICK, KILL, CONTROL, NOTICE = range(7)


@dataclass
class Scenario:
    name: str
    trace: str
    duration_s: float = 12 * 3600
    lam: float = 1.8
    mu: float = 0.5
    cv: float = 0.57                     # 1주차 측정값
    surges: list = field(default_factory=list)   # [(시작, 끝, 배수)]
    tready_median: float = 150.0         # 중간보고서 예시 2분 30초. EC2 측정 후 교체
    tready_sigma: float = 0.42           # q95 ≈ 중앙값의 2배 (지연 시 5분)
    tready_mult: float = 1.0             # 복구 지연 시나리오
    warmup_s: float = 1800.0             # 지표에서 제외하는 시작 구간
    test_fraction: float = 0.4           # trace 의 뒤 40% 에서 시작 시점을 고른다
    notice_s: float = 120.0              # 회수 경고(2분 전 중단 알림 / 재조정 권고)가 오는 시점


@dataclass
class Worker:
    wid: int
    kind: str          # spot | od
    launched: float
    state: str = "booting"   # booting | idle | busy | draining | dead
    ready_at: float = None
    job: object = None
    ended: float = None


class Simulation:
    def __init__(self, scenario: Scenario, policy: Policy, cfg: Config, seed: int):
        self.sc = scenario
        self.cfg = cfg
        self.rng = random.Random(seed)
        self.controller = Controller(policy, cfg)
        self.policy = policy

        total = traces.duration(scenario.trace)
        lo = total * (1 - scenario.test_fraction)
        hi = max(lo, total - scenario.duration_s)
        self.trace_start = lo + (hi - lo) * random.Random(seed * 7919 + 1).random()
        history, future, gap = traces.segment(scenario.trace, self.trace_start, scenario.duration_s)
        self.gap = gap
        self.future = future
        # 제어기는 과거 기록과, 시뮬레이션이 진행되며 드러나는 기록을 함께 본다
        self.evhist = EvictionHistory(history + future, gap)

        self.events = []
        self.seq = 0
        self.now = 0.0
        self.queue = deque()           # (도착 시각, job id)
        self.workers = {}
        self.next_wid = 0
        self.idle = set()
        self.doomed = set()            # 회수 경고를 받은 워커
        self.last_target = (0, 0)
        self.uses_notice = getattr(policy, "uses_notice", False)

        sig2 = math.log(1 + scenario.cv ** 2)
        self.proc_sigma = math.sqrt(sig2)
        self.proc_mu = math.log(1 / scenario.mu) - sig2 / 2

        prior_rng = random.Random(seed + 17)
        prior = [self._tready_sample(prior_rng, mult=1.0) for _ in range(20)]
        self.tready_hist = QuantileHistory(prior, max_len=50)
        self.lam_est = DecayingRate(half_life_s=600, prior_rate=scenario.lam)
        self.proc_est = DecayingMean(half_life_s=600, prior=1 / scenario.mu)

        # 지표
        self.latencies = []
        self.arrived_in_window = 0
        self.reprocessed = 0
        self.evictions = 0
        self.launch_blocked = 0
        self.billed = {"spot": 0.0, "od": 0.0}
        self.control_ticks = 0
        self.ticks_meeting_need = 0
        self.ready_area = 0.0
        self.spare_area = 0.0
        self.targets = []

    # ------------------------------------------------------------ helpers
    def push(self, t, kind, payload=None):
        self.seq += 1
        heapq.heappush(self.events, (t, self.seq, kind, payload))

    def _tready_sample(self, rng=None, mult=None):
        rng = rng or self.rng
        mult = self.sc.tready_mult if mult is None else mult
        return self.sc.tready_median * math.exp(self.sc.tready_sigma * rng.gauss(0, 1)) * mult

    def lam_at(self, t):
        f = 1.0
        for s, e, m in self.sc.surges:
            if s <= t < e:
                f = m
        return self.sc.lam * f

    def spot_available(self, t):
        return self.evhist.available_now(t) > 0

    # ------------------------------------------------------------ setup
    def _schedule_arrivals(self):
        cuts = sorted({0.0, self.sc.duration_s} | {x for s, e, _ in self.sc.surges for x in (s, e)})
        jid = 0
        for a, b in zip(cuts, cuts[1:]):
            rate = self.lam_at(a)
            t = a
            while True:
                t += self.rng.expovariate(rate)
                if t >= b:
                    break
                self.push(t, ARRIVAL, jid)
                jid += 1

    def _schedule_evictions(self):
        prev_avail = None
        for t, avail, evicted in self.future:
            if prev_avail and evicted > 0 and t - self.gap < self.sc.duration_s:
                self.push(max(0.0, t - self.gap), EVICT_TICK, min(1.0, evicted / prev_avail))
            prev_avail = avail

    def _bootstrap(self):
        # 시작할 때는 Nbase 만큼 이미 준비된 스팟 워커가 있다고 둔다 (모든 정책 동일)
        n = math.ceil(self.sc.lam / self.sc.mu)
        for _ in range(n):
            w = self._launch("spot", ready_now=True)
        t = 0.0
        while t < self.sc.duration_s:
            self.push(t, CONTROL)
            t += self.cfg.delta

    # ------------------------------------------------------------ fleet
    def _launch(self, kind, ready_now=False):
        w = Worker(self.next_wid, kind, self.now)
        self.next_wid += 1
        self.workers[w.wid] = w
        if ready_now:
            w.state, w.ready_at = "idle", self.now
            self.idle.add(w.wid)
        else:
            tr = self._tready_sample()
            self.push(self.now + tr, READY, (w.wid, tr))
        return w

    def _terminate(self, w):
        if w.state == "dead":
            return
        if w.state == "busy":
            # 회수: 처리 중이던 작업은 Ack 없이 연결이 끊겨 큐로 돌아간다
            self.queue.appendleft(w.job)
            self.reprocessed += 1
            w.job = None
        self.idle.discard(w.wid)
        w.state = "dead"
        w.ended = self.now
        self.billed[w.kind] += self.now - w.launched

    def _alive(self, kind, include_draining=False):
        states = ("booting", "idle", "busy") + (("draining",) if include_draining else ())
        return [w for w in self.workers.values() if w.kind == kind and w.state in states
                and (include_draining or not self.uses_notice or w.wid not in self.doomed)]

    def _reconcile(self, kind, target):
        alive = self._alive(kind)
        diff = target - len(alive)
        if diff > 0:
            # 종료 대기 중인 워커를 먼저 되살린다
            for w in self._alive(kind, include_draining=True):
                if diff == 0:
                    break
                if w.state == "draining":
                    w.state = "busy"
                    diff -= 1
            for _ in range(diff):
                if kind == "spot" and not self.spot_available(self.now):
                    self.launch_blocked += 1
                    break
                self._launch(kind)
        elif diff < 0:
            order = {"booting": 0, "idle": 1, "busy": 2}
            for w in sorted(alive, key=lambda x: (order[x.state], -x.launched))[:-diff]:
                if w.state == "busy":
                    w.state = "draining"   # 처리 중인 작업은 마치고 종료 (graceful)
                else:
                    self._terminate(w)

    def _dispatch(self):
        while self.queue and self.idle:
            wid = self.idle.pop()
            w = self.workers[wid]
            job = self.queue.popleft()
            w.state, w.job = "busy", job
            dur = math.exp(self.proc_mu + self.proc_sigma * self.rng.gauss(0, 1))
            self.push(self.now + dur, DONE, (wid, job, dur))

    # ------------------------------------------------------------ control
    def _future_fraction(self, t0, window):
        """오라클: [t0, t0+window] 동안 실제로 회수될 비율 (구간들의 회수 비율을 누적)."""
        keep = 1.0
        prev = None
        for t, a, e in self.future:
            if prev and e > 0 and t0 < t and t - self.gap <= t0 + window:
                keep *= 1 - min(1.0, e / prev)
            prev = a
            if t - self.gap > t0 + window:
                break
        return 1 - keep

    def _control(self):
        p = self.cfg.p
        lam = self.lam_est.rate(self.now)
        mu = 1.0 / self.proc_est.mean()
        tq = self.tready_hist.quantile(p)
        tm = self.tready_hist.mean()
        f_p, _ = self.evhist.stats(self.now, tq + self.cfg.delta, p)
        _, f_mean = self.evhist.stats(self.now, tm + self.cfg.delta, p)
        spot_alive = len(self._alive("spot"))
        ctx = Context(t=self.now, lam=lam, mu=mu, queue=len(self.queue), tready_q=tq, tready_mean=tm,
                      f_p=f_p, f_mean=f_mean, spot_alive=spot_alive,
                      spot_available=self.spot_available(self.now))
        if getattr(self.policy, "oracle", False):
            # 오라클은 실제 Tready 의 평균과 앞으로의 회수를 안다
            true_mean = self.sc.tready_median * math.exp(self.sc.tready_sigma ** 2 / 2) * self.sc.tready_mult
            ctx.tready_q = ctx.tready_mean = true_mean
            ctx.future_f = self._future_fraction(self.now, true_mean + self.cfg.delta)
            ctx.lam, ctx.mu = self.lam_at(self.now), self.sc.mu
        spot, od = self.controller.decide(ctx)
        self.targets.append((self.now, spot, od))
        self.last_target = (spot, od)
        self._reconcile("spot", spot)
        self._reconcile("od", od)
        self._dispatch()

        if self.now >= self.sc.warmup_s:
            ready = sum(1 for w in self.workers.values() if w.state in ("idle", "busy", "draining"))
            need = self.lam_at(self.now) / self.sc.mu
            self.control_ticks += 1
            self.ticks_meeting_need += ready >= math.ceil(need)
            self.ready_area += ready
            self.spare_area += ready - need

    # ------------------------------------------------------------ main loop
    def run(self):
        self._schedule_arrivals()
        self._schedule_evictions()
        self._bootstrap()
        end = self.sc.duration_s
        while self.events:
            t, _, kind, payload = heapq.heappop(self.events)
            if t > end:
                break
            self.now = t
            if kind == ARRIVAL:
                self.queue.append((t, payload))
                self.lam_est.add(t)
                if t >= self.sc.warmup_s:
                    self.arrived_in_window += 1
                self._dispatch()
            elif kind == DONE:
                wid, job, dur = payload
                w = self.workers[wid]
                if w.state not in ("busy", "draining") or w.job != job:
                    continue   # 이미 회수된 워커의 완료 사건
                w.job = None
                self.proc_est.add(t, dur)
                if job[0] >= self.sc.warmup_s:
                    self.latencies.append(t - job[0])
                if w.state == "draining":
                    w.state = "idle"
                    self._terminate(w)
                else:
                    w.state = "idle"
                    self.idle.add(wid)
                self._dispatch()
            elif kind == READY:
                wid, tr = payload
                w = self.workers[wid]
                if w.state != "booting":
                    continue
                w.state, w.ready_at = "idle", t
                self.idle.add(wid)
                self.tready_hist.add(tr)
                self._dispatch()
            elif kind == EVICT_TICK:
                spots = self._alive("spot", include_draining=True)
                x = payload * len(spots)
                k = int(x) + (self.rng.random() < x - int(x))
                for w in self.rng.sample(spots, min(k, len(spots))):
                    t_kill = t + self.rng.random() * self.gap
                    self.push(t_kill, KILL, w.wid)
                    self.push(max(t, t_kill - self.sc.notice_s), NOTICE, w.wid)
            elif kind == NOTICE:
                w = self.workers[payload]
                if w.state != "dead" and self.uses_notice:
                    # Capacity Rebalancing: 경고를 받은 워커는 없는 셈 치고 바로 대체 워커를 요청한다
                    self.doomed.add(w.wid)
                    self._reconcile("spot", self.last_target[0])
                    self._reconcile("od", self.last_target[1])
            elif kind == KILL:
                w = self.workers[payload]
                if w.state != "dead":
                    self.evictions += 1
                    self._terminate(w)
                    self._dispatch()
            elif kind == CONTROL:
                self._control()
        self.now = end
        for w in self.workers.values():
            if w.state != "dead":
                self.billed[w.kind] += end - w.launched
        return self.metrics()

    def metrics(self):
        slo = self.cfg.slo
        end = self.sc.duration_s
        late = sum(1 for x in self.latencies if x > slo)
        # 끝날 때까지 처리되지 못한 작업 중 이미 SLO 를 넘긴 것도 위반으로 센다
        stuck = sum(1 for a, _ in self.queue if a >= self.sc.warmup_s and end - a > slo)
        busy_stuck = sum(1 for w in self.workers.values()
                         if w.job and w.job[0] >= self.sc.warmup_s and end - w.job[0] > slo)
        n = max(1, self.arrived_in_window)
        lat = sorted(self.latencies)
        pr = self.cfg.prices
        cost = self.billed["spot"] / 3600 * pr.spot + self.billed["od"] / 3600 * pr.ondemand
        done = len(self.latencies)
        return {
            "scenario": self.sc.name, "policy": self.policy.name, "p": self.cfg.p,
            "trace_start_h": round(self.trace_start / 3600, 1),
            "jobs": n, "completed": done,
            "violation_rate": (late + stuck + busy_stuck) / n,
            "p50_s": lat[len(lat) // 2] if lat else None,
            "p99_s": lat[min(len(lat) - 1, int(len(lat) * 0.99))] if lat else None,
            "spot_hours": self.billed["spot"] / 3600, "od_hours": self.billed["od"] / 3600,
            "cost_usd": cost, "cost_per_1k_jobs": cost / max(1, done) * 1000,
            "evictions": self.evictions, "reprocessed": self.reprocessed,
            "launch_blocked": self.launch_blocked,
            "need_met_rate": self.ticks_meeting_need / max(1, self.control_ticks),
            "mean_ready": self.ready_area / max(1, self.control_ticks),
            "mean_spare": self.spare_area / max(1, self.control_ticks),
        }


def run(scenario, policy, cfg, seed):
    return Simulation(scenario, policy, cfg, seed).run()
