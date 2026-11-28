#!/usr/bin/env python3
"""
2주차 정책 비교 실험.

  python3 -m sim.run_experiments                 # 전부 (4코어 약 5분)
  python3 -m sim.run_experiments --only main --seeds 3

결과: data/results/<실험>.csv (실행 1회 = 한 줄), 그래프는 python3 -m sim.plots
"""
import argparse
import csv
import os
import time
from dataclasses import replace
from multiprocessing import Pool

from capacity.policies import (AwsCapacityRebalance, AwsRebalanceOnDemandBase, AwsRebalanceOnDemandSpare,
                               AwsRebalanceWithSpare, Config, FixedSpare, OnDemandOnly, Proposed, ProposedWithNotice,
                               QueueReactive, ablation_policies, standard_policies)
from sim.engine import Scenario, run

CPU_POOL = "preemption-1-node-aws-04-19-2023-pool3"          # 평상시: CPU 인스턴스, 3개 영역 풀
MULTI_NODE = "preemption-4-node-aws-08-03-2023-us-west-2c_v100_1"  # 회수 집중: 한 영역 4노드

# 2026-11-19 EC2 실측 (docs/tready-ec2.md): c6i.large 서울, 20회.
# 미리 구운 이미지 기준 Tready = 요청~사용자 스크립트 시작(p50 9.9초) + 실제 워커 기동(p50 4.3초, 로컬 2코어)
MEASURED_TREADY_MEDIAN = 14.6
MEASURED_TREADY_SIGMA = 0.06   # q95/중앙값 ≈ 1.1

SCENARIOS = {
    "normal": Scenario("normal", CPU_POOL),
    "concentrated": Scenario("concentrated", MULTI_NODE),
    "surge": Scenario("surge", CPU_POOL, surges=[(6 * 3600, 7 * 3600, 2.0)]),
    "delay-x2": Scenario("delay-x2", MULTI_NODE, tready_mult=2.0),
    "delay-x4": Scenario("delay-x4", MULTI_NODE, tready_mult=4.0),
    "measured": Scenario("measured", MULTI_NODE, tready_median=MEASURED_TREADY_MEDIAN,
                         tready_sigma=MEASURED_TREADY_SIGMA),
}
SLO = 300.0


def _job(args):
    sc, policy, cfg, seed, extra = args
    t = time.time()
    m = run(sc, policy, cfg, seed)
    m.update(seed=seed, slo=cfg.slo, cv=sc.cv, notice_s=sc.notice_s, wall_s=round(time.time() - t, 1), **extra)
    return m


def experiments(seeds):
    S = range(seeds)
    base = Config(slo=SLO)
    out = {}
    out["main"] = [(sc, pol, base, s, {}) for sc in SCENARIOS.values() for pol in standard_policies() for s in S]
    out["p_sweep"] = [(sc, Proposed(name=f"proposed p={p}"), replace(base, p=p), s, {})
                      for sc in SCENARIOS.values() for p in (0.8, 0.9, 0.95, 0.99) for s in S]
    out["ablation"] = [(sc, pol, base, s, {}) for sc in SCENARIOS.values() for pol in ablation_policies() for s in S]
    # 회수 경고가 얼마나 일찍 오느냐 (2분 중단 알림 vs 10분 전 재조정 권고)
    out["notice"] = [(replace(SCENARIOS[n], notice_s=lead), pol, base, s, {})
                     for n in ("concentrated", "delay-x4") for lead in (0, 120, 600)
                     for pol in (QueueReactive(), AwsCapacityRebalance(), Proposed(), ProposedWithNotice())
                     for s in S]
    # 완료시간 목표를 바꿔 가며
    out["slo"] = [(sc, pol, replace(base, slo=slo), s, {})
                  for slo in (180, 300, 600) for sc in (SCENARIOS["concentrated"], SCENARIOS["delay-x4"])
                  for pol in (FixedSpare(1), FixedSpare(2), AwsCapacityRebalance(), Proposed()) for s in S]
    # 식 (7)은 평균 처리율만 본다. 처리 시간 편차가 커지면 얼마나 틀리는가
    out["cv"] = [(replace(SCENARIOS["concentrated"], cv=cv), pol, base, s, {})
                 for cv in (0.57, 1.0, 2.0) for pol in (Proposed(), FixedSpare(2)) for s in S]
    # 핵심 질문: 복구 시간이 얼마를 넘으면 회수 경고 후 교체(AWS)만으로는 부족한가
    out["tready_sweep"] = [(replace(SCENARIOS["concentrated"], name=f"tready-x{m:g}", tready_mult=m), pol, base, s,
                            {"tready_mult": m})
                           for m in (0.1, 0.25, 0.5, 1, 1.5, 2, 3, 4, 6)
                           for pol in (AwsCapacityRebalance(), FixedSpare(2), Proposed(), ProposedWithNotice(),
                                       OnDemandOnly())
                           for s in S]
    # 비용-위반율 경계(frontier): 정책마다 조절 손잡이를 끝까지 돌려 곡선을 만든다.
    # 고정 여유 k, AWS 재조정 + 고정 여유 k, 제안 기법의 p. 같은 비용에서 누가 더 낮은 위반율을 내는지 본다.
    knobs = ([FixedSpare(k) for k in range(0, 7)] + [AwsRebalanceWithSpare(k) for k in range(0, 5)]
             + [AwsRebalanceOnDemandSpare(k) for k in range(1, 5)] + [AwsRebalanceOnDemandBase(k) for k in range(1, 5)]
             + [OnDemandOnly()])
    out["frontier"] = [(replace(SCENARIOS["concentrated"], name=f"tready-x{m:g}", tready_mult=m), pol, base, s,
                        {"tready_mult": m, "knob": getattr(pol, "k", "")})
                       for m in (2, 3, 4, 6) for pol in knobs for s in S]
    out["frontier"] += [(replace(SCENARIOS["concentrated"], name=f"tready-x{m:g}", tready_mult=m),
                         cls(name=f"{nm} p={p}"), replace(base, p=p), s, {"tready_mult": m, "knob": p})
                        for m in (2, 3, 4, 6) for p in (0.8, 0.9, 0.95, 0.99)
                        for cls, nm in ((Proposed, "proposed"), (ProposedWithNotice, "proposed+rebalance"))
                        for s in S]
    # 견고성: 정해 둔 설정 하나로 평상시부터 복구 15분까지 모든 환경을 돌린다.
    # 고정 온디맨드 k 대는 환경에 맞게 k 를 골라야 하고, 제안 기법은 p 하나로 스스로 맞춘다는 주장을 확인한다.
    envs = dict(SCENARIOS)
    envs["delay-x3"] = replace(SCENARIOS["concentrated"], name="delay-x3", tready_mult=3)
    envs["delay-x6"] = replace(SCENARIOS["concentrated"], name="delay-x6", tready_mult=6)
    robust = ([AwsCapacityRebalance(), OnDemandOnly()] + [AwsRebalanceWithSpare(k) for k in (1, 2, 3)]
              + [AwsRebalanceOnDemandBase(k) for k in (1, 2, 3)] + [AwsRebalanceOnDemandSpare(k) for k in (1, 2)])
    out["robust"] = [(sc, pol, base, s, {}) for sc in envs.values() for pol in robust for s in S]
    out["robust"] += [(sc, ProposedWithNotice(name=f"proposed+rebalance p={p}"), replace(base, p=p), s, {})
                      for sc in envs.values() for p in (0.95, 0.99) for s in S]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--out", default="data/results")
    ap.add_argument("--procs", type=int, default=os.cpu_count())
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    with Pool(a.procs) as pool:
        for name, jobs in experiments(a.seeds).items():
            if a.only and name not in a.only:
                continue
            t = time.time()
            rows = pool.map(_job, jobs, chunksize=1)
            path = os.path.join(a.out, f"{name}.csv")
            with open(path, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)
            print(f"{name}: {len(rows)} runs in {time.time() - t:.0f}s -> {path}", flush=True)


if __name__ == "__main__":
    main()
