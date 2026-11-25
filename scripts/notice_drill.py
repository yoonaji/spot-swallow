#!/usr/bin/env python3
"""
회수 경고 실험: 경고 없이 죽을 때와, 경고를 받고 마무리(drain)한 뒤 죽을 때를 비교한다.

세 가지 방식을 같은 회수 일정으로 돌린다 (회수 시각, 횟수, 복구 시간이 같다).
  none           경고 없음. 정해진 시각에 바로 kill. 처리 중이던 작업은 다른 워커가 처음부터 다시 계산한다.
  drain          경고를 받으면 새 작업을 멈추고 처리 중인 것만 끝낸 뒤 쉰다. 교체는 kill 뒤에 요청한다.
  drain+replace  위와 같고, 교체 서버도 경고를 받는 즉시 요청한다 (AWS 재조정과 같은 동작).
  checkpoint     경고 없음(none 과 같은 kill). 대신 워커가 거래 N 건마다 중간 합계를 저장해, 다시 받은 작업은
                 저장 지점부터 계산한다 (--checkpoint-every).

경고는 kill 보다 --lead 초 먼저 온다 (실제 AWS 는 120초, 실험은 작업 길이보다 길게만 잡으면 된다).

  python3 scripts/notice_drill.py                       # 짧은 작업·긴 작업 × 세 방식 × 3회
  python3 scripts/notice_drill.py --workloads long --rounds 1

결과: 표준 출력에 표, data/notice-drill/<시각>.csv
"""
import argparse
import csv
import json
import os
import random
import statistics
import subprocess
import sys
import time
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from e2e import call, wait_for_api  # noqa: E402
from controller.fleet import DockerFleet  # noqa: E402

BASE = "http://localhost:8080"
PSQL = ["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "swallow", "-d", "swallow", "-At", "-c"]

# 이름: (WORK_MICROS_PER_TX, 가맹점 수, 일수, 거래 수) → 작업 1건 길이 약 (거래 수 × micros)
WORKLOADS = {
    "short": (20_000, 25, 8, 100),    # 작업 약 2초, 200건
    "long": (150_000, 12, 5, 100),    # 작업 약 15초, 60건
    "xlong": (600_000, 6, 4, 100),    # 작업 약 60초, 24건 (GPU 배치 추론처럼 긴 작업)
}
MODES = ("none", "drain", "drain+replace", "checkpoint")


def sh(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()


def worker_logs():
    ids = sh("docker", "ps", "-aq", "--filter", "label=swallow.kind").split()
    return "\n".join(subprocess.run(["docker", "logs", i], capture_output=True, text=True).stdout +
                     subprocess.run(["docker", "logs", i], capture_output=True, text=True).stderr for i in ids)


def run_one(workload, mode, seed, workers, lead, interval, evictions, recovery, timeout, checkpoint_every=10):
    micros, merchants, days, tx = WORKLOADS[workload]
    env = {"WORK_MICROS_PER_TX": str(micros), "SPOT_NOTICE_SOURCE": "file",
           "CHECKPOINT_EVERY_TX": str(checkpoint_every if mode == "checkpoint" else 0)}
    fleet = DockerFleet(env=env)
    fleet.cleanup()
    sh("docker", "compose", "down", "-v", "--remove-orphans")
    for _ in range(3):
        up = subprocess.run(["docker", "compose", "up", "-d", "postgres", "rabbitmq", "settlement-api"],
                            capture_output=True, text=True, timeout=300)
        if up.returncode == 0:
            break
        # 가끔 새 RabbitMQ 볼륨의 .erlang.cookie 권한 오류(eacces)로 뜨지 않는다. 볼륨을 비우고 다시 띄운다.
        sh("docker", "compose", "down", "-v", "--remove-orphans")
        sh("docker", "volume", "prune", "-af")
    if up.returncode != 0:
        sys.exit(f"docker compose up failed: {up.stderr[-500:]}")
    wait_for_api(BASE, timeout=180)
    call(BASE, "POST", f"/admin/seed?merchants={merchants}&days={days}&txPerMerchantDay={tx}&startDate=2026-03-01")
    for _ in range(workers):
        fleet.launch("spot")
    time.sleep(12)   # 워커가 소비자로 붙을 때까지

    rng = random.Random(seed)
    t0 = time.time()
    start = date(2026, 3, 1)
    for m in range(1, merchants + 1):
        for d in range(days):
            call(BASE, "POST", "/settlement-jobs", {"merchantId": f"M{m:04d}",
                                                    "settlementDate": (start + timedelta(days=d)).isoformat()})

    # 회수 일정: 경고 시각 = 10 + i*interval, kill = 경고 + lead. 피해 워커는 경고 시각에 살아 있는 것 중 무작위.
    schedule = [10 + i * interval for i in range(evictions)]
    pending_kill = []      # (kill_at, cid)
    pending_launch = []    # launch_at
    doomed = set()
    log = []
    rep = {}
    while time.time() - t0 < timeout:
        now = time.time() - t0
        while schedule and schedule[0] <= now:
            schedule.pop(0)
            alive = sorted(w.cid for w in fleet.alive() if w.cid not in doomed)
            if not alive:
                continue
            cid = rng.choice(alive)
            doomed.add(cid)
            if mode not in ("none", "checkpoint"):
                fleet.notify(cid)
            if mode == "drain+replace":
                pending_launch.append(now)
            pending_kill.append((now + lead, cid))
            log.append(f"{now:5.1f}s doomed {cid}" if mode in ("none", "checkpoint") else f"{now:5.1f}s notice {cid}")
        for item in [x for x in pending_kill if x[0] <= now]:
            pending_kill.remove(item)
            fleet.kill(item[1])
            log.append(f"{now:5.1f}s kill {item[1]}")
            if mode != "drain+replace":
                pending_launch.append(now)
        for item in list(pending_launch):
            pending_launch.remove(item)
            fleet.launch("spot", boot_delay_s=recovery)   # 복구 시간을 BOOT_DELAY 로 흉내 낸다
        _, rep = call(BASE, "GET", "/reconciliation")
        if rep.get("allSettled") and not schedule and not pending_kill:
            break
        time.sleep(1)
    elapsed = time.time() - t0
    time.sleep(2)

    logs = worker_logs()
    lat = [float(x) for x in sh(*PSQL, "SELECT extract(epoch FROM completed_at - accepted_at) FROM settlement_jobs "
                                       "WHERE completed_at IS NOT NULL").split()]
    lat.sort()
    p95 = lat[int(0.95 * (len(lat) - 1))] if lat else 0
    drained = [int(x.split("SPOT DRAINED in ")[1].split()[0]) for x in logs.splitlines() if "SPOT DRAINED in " in x]
    # 중간 저장에서 이어 계산한 작업과, 그 덕분에 다시 계산하지 않은 거래 수
    resumed = [int(x.split("from checkpoint: ")[1].split()[0]) for x in logs.splitlines() if "from checkpoint: " in x]
    row = {
        "workload": workload, "mode": mode, "seed": seed,
        "jobs": rep.get("acceptedJobs"), "completed": rep.get("completedJobs"),
        "kills": sum(1 for e in log if " kill " in e),
        "redelivered": logs.count("REDELIVERED "),
        "duplicates_absorbed": rep.get("duplicateDeliveriesAbsorbed"),
        "drained": len(drained), "drain_ms_max": max(drained) if drained else 0,
        "resumed": len(resumed), "tx_skipped": sum(resumed),
        "compute_saved_s": round(sum(resumed) * micros / 1e6, 1),
        "lost": rep.get("statusResultMismatch"), "amount_mismatch": rep.get("amountMismatch"),
        "p50_s": round(statistics.median(lat), 1) if lat else 0, "p95_s": round(p95, 1),
        "max_s": round(lat[-1], 1) if lat else 0, "elapsed_s": round(elapsed, 1),
        "consistent": bool(rep.get("consistent") and rep.get("allSettled")),
        "events": " | ".join(log[:20]),
    }
    fleet.cleanup()
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workloads", nargs="*", default=list(WORKLOADS))
    ap.add_argument("--modes", nargs="*", default=list(MODES))
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--lead", type=float, default=30, help="경고에서 kill 까지(초). 작업 길이보다 길어야 한다")
    ap.add_argument("--interval", type=float, default=20, help="회수 간격(초)")
    ap.add_argument("--evictions", type=int, default=6)
    ap.add_argument("--recovery", type=float, default=15, help="교체 서버 복구 시간(초, 실측 약 15초)")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--checkpoint-every", type=int, default=10, help="checkpoint 방식에서 거래 몇 건마다 저장할지")
    a = ap.parse_args()

    rows = []
    for r in range(a.rounds):
        for wl in a.workloads:
            for mode in a.modes:
                print(f"== {wl} / {mode} (round {r + 1})", flush=True)
                row = run_one(wl, mode, 1000 + r, a.workers, a.lead, a.interval, a.evictions, a.recovery, a.timeout,
                              a.checkpoint_every)
                row["round"] = r + 1
                print(json.dumps({k: v for k, v in row.items() if k != "events"}, ensure_ascii=False), flush=True)
                rows.append(row)

    os.makedirs("data/notice-drill", exist_ok=True)
    path = f"data/notice-drill/{int(time.time())}.csv"
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print("\n| 작업 | 방식 | 회수 | 다시 받은 작업 | 이어 계산 | 아낀 계산(초) | 마무리(drain) | p50 | p95 | 최대 | 전체 | 정합성 |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for x in rows:
        print(f"| {x['workload']} | {x['mode']} | {x['kills']} | {x['redelivered']} | {x['resumed']} | "
              f"{x['compute_saved_s']} | {x['drained']} | {x['p50_s']} | {x['p95_s']} | {x['max_s']} | "
              f"{x['elapsed_s']} | {'OK' if x['consistent'] else 'FAIL'} |")
    print("saved", path)
    sys.exit(0 if all(x["consistent"] for x in rows) else 1)


if __name__ == "__main__":
    main()
