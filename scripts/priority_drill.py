#!/usr/bin/env python3
"""
마감 등급 우선순위 실험: 서버가 한꺼번에 회수되어 적체가 쌓일 때, 급한 작업의 마감을 지킬 수 있는가.

같은 부하·같은 회수 일정으로 두 방식을 비교한다.
  fifo      큐가 접수 순서대로 꺼낸다 (QUEUE_MAX_PRIORITY=0). 급한 작업도 적체 뒤에 줄을 선다.
  priority  우선순위 큐 (QUEUE_MAX_PRIORITY=10). 급한 작업(URGENT) 메시지를 먼저 꺼낸다.

부하: 초당 --rate 건(포아송), 그중 --urgent-share 가 급한 작업(마감 2분), 나머지는 보통(마감 5분). 작업당 약 2초.
회수: --kill-at 초에 워커 4대 중 3대를 경고 없이 종료하고, --recovery 초 뒤에 교체 워커가 준비된다(느린 복구).

  python3 scripts/priority_drill.py                  # 두 방식 × 2회
  python3 scripts/priority_drill.py --modes priority --rounds 1

결과: 표준 출력에 표, data/priority-drill/<시각>.csv
"""
import argparse
import csv
import json
import os
import random
import subprocess
import sys
import time
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from e2e import call, wait_for_api  # noqa: E402
from controller.fleet import DockerFleet  # noqa: E402

BASE = "http://localhost:8080"
PSQL = ["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "swallow", "-d", "swallow", "-At", "-F", ",", "-c"]
MODES = {"fifo": 0, "priority": 10}


def sh(*cmd, env=None):
    return subprocess.run(cmd, capture_output=True, text=True, env=env).stdout.strip()


def up(env):
    for _ in range(3):
        r = subprocess.run(["docker", "compose", "up", "-d", "postgres", "rabbitmq", "settlement-api"],
                           capture_output=True, text=True, timeout=300, env=env)
        if r.returncode == 0:
            return
        sh("docker", "compose", "down", "-v", "--remove-orphans")
        sh("docker", "volume", "prune", "-af")
    sys.exit(f"docker compose up failed: {r.stderr[-500:]}")


def pct(xs, q):
    return round(xs[int(q * (len(xs) - 1))], 1) if xs else 0


def run_one(mode, seed, a):
    env = {**os.environ, "QUEUE_MAX_PRIORITY": str(MODES[mode])}
    fleet = DockerFleet(env={"WORK_MICROS_PER_TX": str(a.work_micros), "QUEUE_MAX_PRIORITY": str(MODES[mode]),
                             "SPOT_NOTICE_SOURCE": "none"})
    fleet.cleanup()
    sh("docker", "compose", "down", "-v", "--remove-orphans")
    up(env)
    wait_for_api(BASE, timeout=180)
    n_jobs = int(a.rate * a.duration * 1.3) + 50
    merchants = 100
    days = n_jobs // merchants + 1
    call(BASE, "POST", f"/admin/seed?merchants={merchants}&days={days}&txPerMerchantDay=100&startDate=2026-01-01")
    for _ in range(a.workers):
        fleet.launch("spot")
    time.sleep(15)

    rng = random.Random(seed)
    start = date(2026, 1, 1)
    jobs = [(m, d) for d in range(days) for m in range(1, merchants + 1)]
    t0 = time.time()
    t_next = 0.0
    sent = urgent = 0
    killed = launched = False
    rep = {}
    while True:
        now = time.time() - t0
        if not killed and now >= a.kill_at:
            victims = sorted(w.cid for w in fleet.alive())[:a.kill]
            for cid in victims:
                fleet.kill(cid)
            killed = True
            print(f"{now:6.1f}s killed {len(victims)} workers", flush=True)
        if killed and not launched and now >= a.kill_at:
            # 교체는 바로 요청하지만, 준비되기까지 --recovery 초가 걸린다
            for _ in range(a.kill):
                fleet.launch("spot", boot_delay_s=a.recovery)
            launched = True
        if now < a.duration and sent < len(jobs):
            if now >= t_next:
                m, d = jobs[sent]
                pri = "URGENT" if rng.random() < a.urgent_share else "NORMAL"
                urgent += pri == "URGENT"
                call(BASE, "POST", "/settlement-jobs", {"merchantId": f"M{m:04d}", "priority": pri,
                                                        "settlementDate": (start + timedelta(days=d)).isoformat()})
                sent += 1
                t_next += rng.expovariate(a.rate)
                continue
            time.sleep(min(0.2, max(0.0, t_next - now)))
            continue
        _, rep = call(BASE, "GET", "/reconciliation")
        if rep.get("allSettled") or now > a.duration + a.timeout:
            break
        time.sleep(3)
    elapsed = time.time() - t0

    out = sh(*PSQL, "SELECT priority, extract(epoch FROM coalesce(completed_at, now()) - accepted_at), "
                    "(coalesce(completed_at, now()) > deadline_at)::int FROM settlement_jobs")
    by = {"URGENT": [], "NORMAL": []}
    miss = {"URGENT": 0, "NORMAL": 0}
    for line in out.splitlines():
        p, lat, m = line.split(",")
        by[p].append(float(lat))
        miss[p] += int(m)
    for v in by.values():
        v.sort()
    row = {"mode": mode, "seed": seed, "jobs": sent, "urgent_jobs": len(by["URGENT"]),
           "urgent_miss_pct": round(100 * miss["URGENT"] / max(1, len(by["URGENT"])), 2),
           "urgent_p50_s": pct(by["URGENT"], 0.5), "urgent_p95_s": pct(by["URGENT"], 0.95),
           "urgent_max_s": pct(by["URGENT"], 1.0),
           "normal_miss_pct": round(100 * miss["NORMAL"] / max(1, len(by["NORMAL"])), 2),
           "normal_p50_s": pct(by["NORMAL"], 0.5), "normal_p95_s": pct(by["NORMAL"], 0.95),
           "normal_max_s": pct(by["NORMAL"], 1.0), "elapsed_s": round(elapsed, 1),
           "consistent": bool(rep.get("consistent") and rep.get("allSettled")),
           "lost": rep.get("statusResultMismatch"), "amount_mismatch": rep.get("amountMismatch")}
    fleet.cleanup()
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--modes", nargs="*", default=list(MODES))
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--kill", type=int, default=3, help="한꺼번에 회수되는 워커 수")
    ap.add_argument("--kill-at", type=float, default=120)
    ap.add_argument("--recovery", type=float, default=300, help="교체 워커 준비까지(초). 느린 복구")
    ap.add_argument("--rate", type=float, default=1.5)
    ap.add_argument("--urgent-share", type=float, default=0.2)
    ap.add_argument("--duration", type=float, default=600, help="부하를 넣는 시간(초)")
    ap.add_argument("--work-micros", type=int, default=20000, help="거래 100건 × 20ms = 작업 약 2초")
    ap.add_argument("--timeout", type=float, default=1500)
    a = ap.parse_args()

    rows = []
    for r in range(a.rounds):
        for mode in a.modes:
            print(f"== {mode} (round {r + 1})", flush=True)
            row = run_one(mode, 2000 + r, a)
            print(json.dumps(row, ensure_ascii=False), flush=True)
            rows.append(row)
    os.makedirs("data/priority-drill", exist_ok=True)
    path = f"data/priority-drill/{int(time.time())}.csv"
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print("\n| 방식 | 작업 | 급한 작업 | 급한 작업 마감 초과 | 급한 p95 | 보통 작업 마감 초과 | 보통 p95 | 보통 최대 | 정합성 |")
    print("|---|---|---|---|---|---|---|---|---|")
    for x in rows:
        print(f"| {x['mode']} | {x['jobs']} | {x['urgent_jobs']} | {x['urgent_miss_pct']}% | {x['urgent_p95_s']} | "
              f"{x['normal_miss_pct']}% | {x['normal_p95_s']} | {x['normal_max_s']} | "
              f"{'OK' if x['consistent'] else 'FAIL'} |")
    print("saved", path)
    sys.exit(0 if all(x["consistent"] for x in rows) else 1)


if __name__ == "__main__":
    main()
