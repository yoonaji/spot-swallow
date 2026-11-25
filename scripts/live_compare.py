#!/usr/bin/env python3
"""
실제 시스템에서 정책 비교: 복구(부팅)가 긴 환경에서도 시뮬레이터 결론이 나오는지 확인한다.

정책마다 시스템을 새로 띄우고, 같은 회수 기록 구간(같은 시작점, 같은 배속, 같은 seed)과 같은 부하로
제어기(controller.main)를 돌린다. 끝나면 DB 에서 완료 시간과 목표 시간 위반율을, 제어기 로그에서
스팟·온디맨드 워커 사용량(비용)을 구하고, 대사로 정합성을 확인한다.

  python3 scripts/live_compare.py --boot-median 600 --policies aws-rebalance fixed-2 proposed+rebalance

결과: 표준 출력에 표, data/live-compare/<시각>.csv
"""
import argparse
import csv
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from e2e import call, wait_for_api  # noqa: E402

BASE = "http://localhost:8080"
PSQL = ["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "swallow", "-d", "swallow", "-At", "-c"]
SPOT, OD = 0.037, 0.107   # capacity.policies.Prices 와 같은 가정 가격 ($/h)


def sh(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()


def cleanup():
    ids = sh("docker", "ps", "-aq", "--filter", "label=swallow.kind").split()
    if ids:
        sh("docker", "rm", "-f", *ids)
    sh("docker", "compose", "down", "-v", "--remove-orphans")


def usage(log_path):
    """제어기 로그(주기마다 한 줄)에서 스팟·온디맨드 워커-분을 적분한다."""
    rows = list(csv.DictReader(open(log_path)))
    spot = od = 0.0
    for a, b in zip(rows, rows[1:]):
        dt = (float(b["t"]) - float(a["t"])) / 60
        spot += int(a["spot_alive"]) * dt
        od += int(a["od_alive"]) * dt
    return spot, od, rows


def run_one(policy, a, seed):
    cleanup()
    for _ in range(3):
        up = subprocess.run(["docker", "compose", "up", "-d", "postgres", "rabbitmq", "settlement-api"],
                            capture_output=True, text=True, timeout=300)
        if up.returncode == 0:
            break
        # 가끔 새 RabbitMQ 볼륨의 .erlang.cookie 권한 오류(eacces)로 뜨지 않는다. 볼륨을 비우고 다시 띄운다.
        cleanup()
        sh("docker", "volume", "prune", "-af")
    if up.returncode != 0:
        sys.exit(f"docker compose up failed: {up.stderr[-400:]}")
    wait_for_api(BASE, timeout=180)
    os.makedirs("data/live-compare", exist_ok=True)
    log = f"data/live-compare/{policy}{a.tag}-boot{int(a.boot_median)}-s{seed}-{int(time.time())}.csv"
    env = {**os.environ, "WORK_MICROS_PER_TX": str(a.work_micros)}
    out = open(log.replace(".csv", ".out"), "w")
    ctl = subprocess.Popen([sys.executable, "-m", "controller.main", "--policy", policy, "--duration", str(a.duration),
                            "--speedup", str(a.speedup), "--boot-median", str(a.boot_median),
                            "--trace-start-h", str(a.trace_start_h), "--seed", str(seed), "--metrics-port", "0",
                            "--injector-delay", str(a.injector_delay), "--log", log],
                           env=env, stdout=out, stderr=subprocess.STDOUT)
    time.sleep(40)   # 초기 워커 준비
    load = subprocess.run([sys.executable, "scripts/load.py", "--rate", str(a.rate), "--duration",
                           str(a.duration - 40 - a.tail), "--seed", str(seed), "--drain-timeout", "1"],
                          capture_output=True, text=True)
    ctl.wait()
    out.close()
    starts = [float(x.split()[-1]) for x in open(log.replace(".csv", ".out")) if x.startswith("INJECTOR START")]
    inj_start = starts[0] if starts else 0.0
    # 제어기가 끝난 뒤 남은 워커가 남은 작업을 마저 처리한다
    rep = {}
    deadline = time.time() + a.tail + 600
    while time.time() < deadline:
        _, rep = call(BASE, "GET", "/reconciliation")
        if rep.get("allSettled"):
            break
        time.sleep(5)
    # 회수 주입이 시작된 뒤 접수된 작업만 센다 (워밍업 구간은 회수가 없어 위반율을 희석한다)
    lat = sorted(float(x) for x in sh(*PSQL, "SELECT extract(epoch FROM coalesce(completed_at, now()) - accepted_at) "
                                             f"FROM settlement_jobs WHERE accepted_at >= to_timestamp({inj_start})"
                                      ).split())
    n = len(lat)
    viol = sum(1 for x in lat if x > a.slo)
    spot_min, od_min, rows = usage(log)
    # 워밍업(회수 주입 전) 구간에 정책 전환으로 처리 능력이 빠지지 않았는지 본다
    warm = [r for r in rows if float(r["t"]) < a.injector_delay][2:]
    after = [r for r in rows if float(r["t"]) >= a.injector_delay]
    lat_all = sorted(float(x) for x in sh(*PSQL, "SELECT extract(epoch FROM coalesce(completed_at, now()) - accepted_at) "
                                                 "FROM settlement_jobs").split())
    row = {
        "policy": policy, "boot_median_s": a.boot_median, "seed": seed, "jobs_after_injection": n,
        "violation_pct": round(100 * viol / n, 2) if n else 0,
        "p50_s": round(lat[n // 2], 1) if n else 0, "p95_s": round(lat[int(0.95 * (n - 1))], 1) if n else 0,
        "max_s": round(lat[-1], 1) if n else 0,
        "violation_all_pct": round(100 * sum(1 for x in lat_all if x > a.slo) / len(lat_all), 2) if lat_all else 0,
        "warm_min_ready": min((int(r["ready"]) for r in warm), default=0),
        "warm_max_queue": max((int(r["queue"]) for r in warm), default=0),
        "after_max_queue": max((int(r["queue"]) for r in after), default=0),
        "spot_worker_min": round(spot_min, 1), "od_worker_min": round(od_min, 1),
        "cost_usd_per_h_equiv": round((spot_min * SPOT + od_min * OD) / 60, 3),
        "evictions": rows[-1]["evictions"] if rows else 0,
        "max_od": max((int(r["od_alive"]) for r in rows), default=0),
        "consistent": bool(rep.get("consistent") and rep.get("allSettled")),
        "lost": rep.get("statusResultMismatch"), "amount_mismatch": rep.get("amountMismatch"),
        "load_tail": load.stdout.strip().splitlines()[-1:] and load.stdout.strip().splitlines()[-1][:120],
        "log": log,
    }
    cleanup()
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--policies", nargs="*", default=["aws-rebalance", "fixed-2", "proposed+rebalance"])
    ap.add_argument("--seeds", nargs="*", type=int, default=[0, 1])
    ap.add_argument("--boot-median", type=float, default=600, help="부팅 지연 중앙값(초). 실측 CPU 는 약 10초")
    ap.add_argument("--duration", type=float, default=2700)
    ap.add_argument("--tail", type=float, default=300, help="부하를 멈춘 뒤 제어기를 더 돌리는 시간(초)")
    ap.add_argument("--speedup", type=float, default=1, help="1 이어야 부팅 시간과 회수 간격의 비율이 실제와 같다")
    ap.add_argument("--trace-start-h", type=float, default=321.70, help="321.83h 에 4대 중 3대, 이어서 전부 회수")
    ap.add_argument("--injector-delay", type=float, default=900, help="워밍업(초): 부팅이 긴 정책도 목표 대수를 갖출 시간")
    ap.add_argument("--rate", type=float, default=1.8)
    ap.add_argument("--work-micros", type=int, default=10000)
    ap.add_argument("--slo", type=float, default=300)
    ap.add_argument("--tag", default="", help="로그 파일 이름에 붙일 표시 (예: mbb)")
    a = ap.parse_args()

    rows = []
    for seed in a.seeds:
        for p in a.policies:
            print(f"== {p} boot={a.boot_median}s seed={seed}", flush=True)
            r = run_one(p, a, seed)
            print(json.dumps(r, ensure_ascii=False), flush=True)
            rows.append(r)
    path = f"data/live-compare/summary-{int(time.time())}.csv"
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print("\n| 정책 | 부팅 중앙값 | seed | 작업 | 위반율 | p95 | 최대 | 스팟 워커-분 | 온디맨드 워커-분 | 비용 | 정합성 |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for x in rows:
        print(f"| {x['policy']} | {x['boot_median_s']:.0f}초 | {x['seed']} | {x['jobs_after_injection']} | {x['violation_pct']}% | "
              f"{x['p95_s']} | {x['max_s']} | {x['spot_worker_min']} | {x['od_worker_min']} | "
              f"{x['cost_usd_per_h_equiv']} | {'OK' if x['consistent'] else 'FAIL'} |")
    print("saved", path)


if __name__ == "__main__":
    main()
