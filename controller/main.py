#!/usr/bin/env python3
"""
실제 시스템용 여유 용량 제어기. 시뮬레이터와 같은 capacity.Controller 를 Δ 초마다 호출한다.

  python3 -m controller.main --policy proposed --duration 1800 --speedup 5

동작:
  1. 시작할 때 Nbase 만큼 스팟 워커를 띄우고 준비될 때까지 기다린다.
  2. 회수 주입기(injector.py)를 같은 프로세스의 스레드로 시작한다 (--no-injector 로 끌 수 있다).
  3. Δ 초마다: RabbitMQ·DB 에서 λ, μ, Q, Tready 를 읽고, trace 의 과거 기록으로 f_p 를 구해
     (스팟 n, 온디맨드 d) 목표를 정하고, docker 컨테이너를 띄우거나 내린다.
  4. 회수 경고를 쓰는 정책은 경고가 오는 즉시(1초 이내) 대체 워커를 요청한다.
  5. 매 주기 상태를 data/live/<이름>.csv 에 남긴다.

로컬 docker 에는 인스턴스 확보·부팅 시간이 없으므로, 스팟·온디맨드 워커를 띄울 때 로그정규 분포에서 뽑은
부팅 지연(BOOT_DELAY_S)을 넣는다. Tready = 부팅 지연 + 실제 JVM 기동 시간(+ 3주차 보안 단계)이다.
"""
import argparse
import csv
import math
import os
import random
import time

from capacity.estimators import DecayingMean, DecayingRate, EvictionHistory, QuantileHistory
from capacity.policies import (AwsCapacityRebalance, Config, Context, Controller, FixedSpare, OnDemandOnly,
                               Proposed, ProposedWithNotice, QueueReactive)
from controller.fleet import DockerFleet, plan_changes
from controller.injector import Injector
from controller.observe import DbObserver, Market, RabbitObserver
from sim import traces

POLICIES = {
    "proposed": Proposed, "proposed+rebalance": ProposedWithNotice, "aws-rebalance": AwsCapacityRebalance,
    "queue-reactive": QueueReactive, "on-demand-only": OnDemandOnly,
    "fixed-1": lambda: FixedSpare(1), "fixed-2": lambda: FixedSpare(2), "fixed-4": lambda: FixedSpare(4),
}
DEFAULT_TRACE = "preemption-4-node-aws-08-03-2023-us-west-2c_v100_1"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--policy", default="proposed", choices=sorted(POLICIES))
    ap.add_argument("--p", type=float, default=0.95)
    ap.add_argument("--slo", type=float, default=300)
    ap.add_argument("--delta", type=float, default=30)
    ap.add_argument("--tdown", type=float, default=600)
    ap.add_argument("--half-life", type=float, default=600, help="λ, μ 추정 반감기(초)")
    ap.add_argument("--trace", default=DEFAULT_TRACE)
    ap.add_argument("--trace-start-h", type=float, help="기본값: trace 의 뒤 40%% 시작점")
    ap.add_argument("--speedup", type=float, default=1.0, help="trace 재생 배속")
    ap.add_argument("--notice", type=float, default=120, help="회수 경고가 오는 시점(trace 초)")
    ap.add_argument("--boot-median", type=float, default=60, help="부팅 지연 중앙값(초)")
    ap.add_argument("--boot-sigma", type=float, default=0.42)
    ap.add_argument("--expected-lam", type=float, default=1.8, help="추정기 사전값")
    ap.add_argument("--expected-mu", type=float, default=0.5)
    ap.add_argument("--n-max", type=int, default=12)
    ap.add_argument("--od-max", type=int, default=12)
    ap.add_argument("--duration", type=float, default=1800)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--no-injector", action="store_true")
    ap.add_argument("--log", help="기본값: data/live/<정책>-<시각>.csv")
    a = ap.parse_args()

    rng = random.Random(a.seed)
    cfg = Config(delta=a.delta, slo=a.slo, tdown=a.tdown, p=a.p, n_max=a.n_max, od_max=a.od_max)
    policy = POLICIES[a.policy]()
    controller = Controller(policy, cfg)
    uses_notice = getattr(policy, "uses_notice", False)

    rows, gap = traces.load(a.trace)
    total = rows[-1][0] + gap
    trace_start = a.trace_start_h * 3600 if a.trace_start_h is not None else total * 0.6
    evhist = EvictionHistory(list(rows), gap)

    fleet = DockerFleet(env={"WORK_MICROS_PER_TX": os.environ.get("WORK_MICROS_PER_TX", "10000")})
    rabbit, db, market = RabbitObserver(), DbObserver(), Market()
    market.write({"available": 1, "notices": {}})

    def boot_delay():
        return a.boot_median * math.exp(a.boot_sigma * rng.gauss(0, 1))

    prior = [boot_delay() + 4 for _ in range(20)]   # 사전값: 부팅 지연 + JVM 기동 약 4초
    tready = QuantileHistory(prior, max_len=50)
    lam_est = DecayingRate(a.half_life, prior_rate=a.expected_lam, t0=time.time())
    proc_est = DecayingMean(a.half_life, prior=1 / a.expected_mu, t0=time.time())

    # 1. 초기 워커 (모든 정책 같은 조건): 부팅 지연 없이 Nbase 대
    n0 = math.ceil(a.expected_lam / a.expected_mu)
    print(f"policy={a.policy} p={a.p} trace={a.trace} start={trace_start / 3600:.1f}h speedup={a.speedup}")
    for _ in range(n0):
        fleet.launch("spot", 0)
    deadline = time.time() + 120
    while time.time() < deadline and len([w for w in fleet.alive() if w.state == "ready"]) < n0:
        fleet.refresh(rabbit.consumer_ips())
        time.sleep(1)
    print(f"initial workers ready: {len([w for w in fleet.alive() if w.state == 'ready'])}/{n0}")
    db.new_processing_times()  # 이전 실행의 기록은 건너뛴다
    published_prev = rabbit.queue_stats()["published_total"]

    injector = None
    if not a.no_injector:
        injector = Injector(fleet, a.trace, trace_start, a.speedup, a.notice, a.seed, market)
        injector.start()

    log_path = a.log or f"data/live/{a.policy}-{int(time.time())}.csv"
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    fields = ["t", "lam", "mu", "queue", "unacked", "tready_q", "f_p", "spot_available", "target_spot", "target_od",
              "spot_alive", "od_alive", "ready", "launched", "stopped", "evictions"]
    f = open(log_path, "w", newline="")
    w = csv.DictWriter(f, fieldnames=fields)
    w.writeheader()

    t0 = time.time()
    last_target = (n0, 0)
    next_tick = t0
    seen_notices = set()

    def reconcile(target_spot, target_od, available, doomed):
        add_spot, add_od, stops = plan_changes(fleet.alive(), target_spot, target_od, doomed)
        for cid in stops:
            fleet.stop(cid)
        if not available:
            add_spot = 0   # 시장에 스팟 용량이 없다
        for _ in range(add_spot):
            fleet.launch("spot", boot_delay())
        for _ in range(add_od):
            fleet.launch("od", boot_delay())
        return add_spot + add_od, len(stops)

    try:
        while time.time() - t0 < a.duration:
            now = time.time()
            m = market.read()
            notices = set(m.get("notices", {})) if uses_notice else set()
            if notices - seen_notices:
                # Capacity Rebalancing: 경고를 받은 워커는 없는 셈 치고 바로 대체 요청
                reconcile(*last_target, m.get("available", 1) > 0, notices)
                seen_notices |= notices
            if now < next_tick:
                time.sleep(min(1.0, next_tick - now))
                continue
            next_tick += a.delta

            qs = rabbit.queue_stats()
            lam_est.add(now, max(0, qs["published_total"] - published_prev))
            published_prev = qs["published_total"]
            for x in db.new_processing_times():
                proc_est.add(now, x)
            for wk in fleet.refresh(rabbit.consumer_ips()):
                tready.add(wk.ready_at - wk.launched)

            tq, tm = tready.quantile(a.p), tready.mean()
            trace_now = injector.trace_time(now) if injector else trace_start
            f_p, _ = evhist.stats(trace_now, (tq + a.delta) * a.speedup, a.p)
            _, f_mean = evhist.stats(trace_now, (tm + a.delta) * a.speedup, a.p)
            available = m.get("available", 1) > 0
            spot_alive = [x for x in fleet.alive("spot") if x.cid not in notices]
            ctx = Context(t=now, lam=lam_est.rate(now), mu=1 / proc_est.mean(), queue=qs["ready"],
                          tready_q=tq, tready_mean=tm, f_p=f_p, f_mean=f_mean,
                          spot_alive=len(spot_alive), spot_available=available)
            last_target = controller.decide(ctx)
            launched, stopped = reconcile(*last_target, available, notices)
            row = {"t": round(now - t0, 1), "lam": round(ctx.lam, 3), "mu": round(ctx.mu, 3), "queue": qs["ready"],
                   "unacked": qs["unacked"], "tready_q": round(tq, 1), "f_p": round(f_p, 3),
                   "spot_available": int(available), "target_spot": last_target[0], "target_od": last_target[1],
                   "spot_alive": len(fleet.alive("spot")), "od_alive": len(fleet.alive("od")),
                   "ready": len([x for x in fleet.alive() if x.state == "ready"]),
                   "launched": launched, "stopped": stopped, "evictions": injector.kills if injector else 0}
            w.writerow(row)
            f.flush()
            print(row, flush=True)
    finally:
        if injector:
            injector.stop_flag.set()
        f.close()
        print(f"log: {log_path}")


if __name__ == "__main__":
    main()
