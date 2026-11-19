#!/usr/bin/env python3
"""
시뮬레이터 검증: 실제 시스템(docker compose)과 시뮬레이터를 같은 조건으로 돌려 결과를 비교한다.

같은 조건이란:
  - 작업 도착 시각: 실제 실행에서 DB 에 기록된 접수 시각(accepted_at)을 그대로 쓴다.
  - 처리 시간: 실제 실행에서 워커가 남긴 processing_ms 를 무작위로 다시 뽑아 쓴다(부트스트랩).
  - 회수: kill_every 초마다 처리 중이거나 놀고 있는 워커 하나를 종료한다 (scripts/e2e.py 의 evictor 와 같다).
  - 복구: 종료 후 recovery 초 + 컨테이너 기동 시간 뒤에 같은 워커가 돌아온다 (e2e.py 의 replacer 와 같다).

비교 지표: 전체 처리 시간(makespan), 작업 완료시간 평균·p95, 재처리 건수.

  # 1) 실제 시스템 실행 (워커 3대, 작업 150건, 작업당 약 2초)
  WORK_MICROS_PER_TX=10000 scripts/reset.sh 3
  python3 scripts/e2e.py --merchants 50 --days 3 --kill-every 20
  python3 -m sim.validate --export data/validation/kill20.json --kill-every 20
  # 2) 시뮬레이터로 같은 조건 재현 후 비교
  python3 -m sim.validate --compare data/validation/*.json
"""
import argparse
import glob
import heapq
import json
import os
import random
import subprocess
from statistics import mean

PSQL = ["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "swallow", "-d", "swallow", "-At", "-F", ",", "-c"]


def q(sql):
    out = subprocess.run(PSQL + [sql], capture_output=True, text=True, check=True).stdout
    return [line.split(",") for line in out.strip().splitlines() if line]


def export(path, kill_every, recovery, workers):
    jobs = q("SELECT extract(epoch FROM accepted_at), extract(epoch FROM completed_at) FROM settlement_jobs "
             "WHERE completed_at IS NOT NULL ORDER BY accepted_at")
    proc = [int(r[0].split("processing_ms=")[1].split(";")[0])
            for r in q("SELECT detail FROM job_events WHERE event_type='COMPLETED'")]
    dup = q("SELECT count(*) FROM job_events WHERE event_type='DUPLICATE_SKIPPED'")[0][0]
    t0 = min(float(a) for a, _ in jobs)
    arrivals = [float(a) - t0 for a, _ in jobs]
    lat = sorted(float(c) - float(a) for a, c in jobs)
    makespan = max(float(c) for _, c in jobs) - t0
    # 컨테이너 기동 시간: 워커 로그의 "Started ... in X seconds"
    logs = subprocess.run(["docker", "compose", "logs", "settlement-worker"], capture_output=True, text=True).stdout
    starts = [float(x.split(" in ")[1].split(" seconds")[0]) for x in logs.splitlines()
              if "Started SettlementWorkerApplication in" in x]
    data = {"kill_every": kill_every, "recovery": recovery, "workers": workers, "arrivals": arrivals,
            "processing_s": [p / 1000 for p in proc], "startup_s": starts,
            "real": {"makespan_s": makespan, "latency_mean_s": mean(lat), "latency_p95_s": lat[int(len(lat) * .95)],
                     "jobs": len(jobs), "duplicates_absorbed": int(dup)}}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    json.dump(data, open(path, "w"))
    print(json.dumps(data["real"]), "startup", mean(starts) if starts else None)


def simulate(d, seed):
    rng = random.Random(seed)
    startup = mean(d["startup_s"]) if d["startup_s"] else 5.0
    events, seq = [], 0

    def push(t, kind, x=None):
        nonlocal seq
        seq += 1
        heapq.heappush(events, (t, seq, kind, x))

    for i, a in enumerate(d["arrivals"]):
        push(a, "arr", (a, i))
    if d["kill_every"]:
        t = d["kill_every"]
        while t < 3600:
            push(t, "kill")
            t += d["kill_every"]
    alive = {w: None for w in range(d["workers"])}   # 워커 → 처리 중 작업 (None = 놀고 있음)
    epoch = {w: 0 for w in alive}
    queue, done, reprocessed = [], {}, 0
    total = len(d["arrivals"])

    def dispatch(now):
        for w in list(alive):
            if alive[w] is None and queue:
                job = queue.pop(0)
                alive[w] = job
                push(now + rng.choice(d["processing_s"]), "done", (w, epoch[w], job))

    while events and len(done) < total:
        now, _, kind, x = heapq.heappop(events)
        if kind == "arr":
            queue.append(x)
        elif kind == "done":
            w, ep, job = x
            if w in alive and epoch[w] == ep and alive[w] == job:
                done[job[1]] = now - job[0]
                alive[w] = None
        elif kind == "kill" and alive:
            w = rng.choice(list(alive))
            if alive[w] is not None:
                queue.insert(0, alive[w])
                reprocessed += 1
            del alive[w]
            epoch[w] += 1
            # replacer 는 1초 간격으로 확인하므로 평균 0.5초 늦게 본다
            push(now + d["recovery"] + 0.5 + startup, "back", w)
        elif kind == "back":
            alive[x] = None
        dispatch(now)
    lat = sorted(done.values())
    return {"makespan_s": now, "latency_mean_s": mean(lat), "latency_p95_s": lat[int(len(lat) * .95)],
            "reprocessed": reprocessed}


def compare(paths, seeds=20):
    print("| 조건 | 지표 | 실제 시스템 | 시뮬레이터 (20회 평균) | 오차 |")
    print("|---|---|---|---|---|")
    for p in paths:
        d = json.load(open(p))
        sims = [simulate(d, s) for s in range(seeds)]
        label = f"회수 {d['kill_every']:g}초마다" if d["kill_every"] else "회수 없음"
        for k, name in [("makespan_s", "전체 처리 시간(초)"), ("latency_mean_s", "평균 완료시간(초)"),
                        ("latency_p95_s", "p95 완료시간(초)")]:
            real, sim = d["real"][k], mean(s[k] for s in sims)
            print(f"| {label} | {name} | {real:.1f} | {sim:.1f} | {(sim / real - 1) * 100:+.1f}% |")
        print(f"| {label} | 중복 흡수 / 재처리 | {d['real']['duplicates_absorbed']} | "
              f"{mean(s['reprocessed'] for s in sims):.1f} | - |")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--export")
    ap.add_argument("--compare", nargs="*")
    ap.add_argument("--kill-every", type=float, default=0)
    ap.add_argument("--recovery", type=float, default=3)
    ap.add_argument("--workers", type=int, default=3)
    a = ap.parse_args()
    if a.export:
        export(a.export, a.kill_every, a.recovery, a.workers)
    if a.compare is not None:
        compare(a.compare or sorted(glob.glob("data/validation/*.json")))


if __name__ == "__main__":
    main()
