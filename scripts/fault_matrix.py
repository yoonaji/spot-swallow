#!/usr/bin/env python3
"""
장애 주입 매트릭스 자동 실행 (플랜 "실험 설계 > 장애 주입 매트릭스").

행마다 시스템을 새로 띄우고, 작업을 넣고, 처리 도중 장애를 주입한 뒤, 끝까지 처리되면 대사로
"유실 0, 중복 반영 0, 금액 일치"를 확인한다. 표준 라이브러리와 docker 만 쓴다.

  python3 scripts/fault_matrix.py                      # 전체 (약 20분)
  python3 scripts/fault_matrix.py rabbitmq-restart     # 한 행만
  python3 scripts/fault_matrix.py --rounds 3           # 행마다 3번 반복

결과: 표준 출력에 표, data/fault-matrix/<시각>.csv
"""
import argparse
import csv
import json
import os
import random
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(__file__))
from e2e import call, wait_for_api  # noqa: E402

BASE = "http://localhost:8080"
SLO_S = 300
WORKERS = 4
PSQL = ["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "swallow", "-d", "swallow", "-At", "-c"]


def sh(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()


def ids(service, *extra):
    return sh("docker", "ps", "-q", *extra, "--filter", f"label=com.docker.compose.service={service}").split()


def rabbit_publish(routing_key, payload):
    import base64
    req = urllib.request.Request(
        "http://localhost:15672/api/exchanges/%2F/amq.default/publish", method="POST",
        data=json.dumps({"properties": {"delivery_mode": 2}, "routing_key": routing_key, "payload": payload,
                         "payload_encoding": "string"}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Basic " + base64.b64encode(b"swallow:swallow").decode()})
    urllib.request.urlopen(req, timeout=10).read()


def dlq_depth():
    import base64
    req = urllib.request.Request("http://localhost:15672/api/queues/%2F/settlement.jobs.dlq",
                                 headers={"Authorization": "Basic " + base64.b64encode(b"swallow:swallow").decode()})
    return json.load(urllib.request.urlopen(req, timeout=10)).get("messages", 0)


# ---------------------------------------------------------------- 장애 주입 (처리 도중 실행)
def kill_api(log):
    # 접수 커밋 직후·발행 전 등 발행기 어디서든 죽는다. 아웃박스가 남아 재시작 후 다시 발행해야 한다.
    for _ in range(3):
        time.sleep(4)
        for c in ids("settlement-api"):
            sh("docker", "kill", "-s", "KILL", c)
            log("killed settlement-api")


def kill_workers_periodically(log):
    for _ in range(10):
        time.sleep(3)
        running = ids("settlement-worker")
        if running:
            sh("docker", "kill", "-s", "KILL", random.choice(running))
            log("killed a worker")


def restart_rabbitmq(log):
    time.sleep(8)
    sh("docker", "compose", "restart", "rabbitmq")
    log("restarted rabbitmq")


def restart_postgres(log):
    time.sleep(8)
    sh("docker", "compose", "restart", "postgres")
    log("restarted postgres")


def poison(log):
    time.sleep(5)
    rabbit_publish("settlement.jobs", json.dumps({"jobId": "POISON-does-not-exist"}))
    log("published a poison message")


def mass_eviction(log):
    time.sleep(10)
    victims = ids("settlement-worker")[: WORKERS // 2]
    sh("docker", "kill", "-s", "KILL", *victims)
    log(f"killed {len(victims)}/{WORKERS} workers at once")


# 이름: (장애 지점, 주입 방법, 기대 동작, 막는 장치, compose 환경 변수, 주입 함수)
MATRIX = {
    "api-kill": ("API 커밋 직후, 발행 전", "발행기(API) 프로세스 SIGKILL 3회", "재시작 후 아웃박스에서 다시 발행",
                 "Outbox", {}, kill_api),
    "publisher-crash-after-send": ("발행 후, PUBLISHED 표시 전", "브로커 수신 직후 1% 확률로 발행기 종료",
                                   "같은 메시지가 두 번 발행되어도 결과는 한 번", "Outbox 재시도 + 결과 PK",
                                   {"CHAOS_PUBLISHER_CRASH_PROBABILITY": "0.01"}, None),
    "worker-kill": ("워커 계산 중", "3초마다 워커 SIGKILL 10회", "메시지 재전달, 다른 워커가 처리", "수동 Ack",
                    {}, kill_workers_periodically),
    "after-commit-before-ack": ("워커 커밋 후, Ack 전", "커밋 직후 5% 확률로 워커 종료",
                                "재전달되지만 결과 PK 충돌로 Ack 만 전송", "결과 PK 멱등성",
                                {"CHAOS_CRASH_POINT": "AFTER_COMMIT_BEFORE_ACK", "CHAOS_CRASH_PROBABILITY": "0.05"},
                                None),
    "rabbitmq-restart": ("RabbitMQ 재시작", "docker compose restart rabbitmq", "durable 큐의 메시지 유지",
                         "durable 큐, persistent 메시지", {}, restart_rabbitmq),
    "postgres-restart": ("PostgreSQL 재시작", "docker compose restart postgres", "커밋 실패 시 Ack 안 함, 재시도",
                         "커밋 후 Ack 순서", {}, restart_postgres),
    "poison": ("독성 메시지", "존재하지 않는 작업 ID 메시지 투입", "DLQ 로 격리, 나머지는 정상 처리", "DLQ",
               {}, poison),
    "mass-eviction": ("동시 대량 회수", f"워커 {WORKERS}대 중 절반 동시 SIGKILL", "목표 시간(5분) 내 복구",
                      "교체 + 수동 Ack", {}, mass_eviction),
}


def supervisor(stop, log, recovery):
    """Auto Scaling 역할: 종료된 워커와 API 를 recovery 초 뒤 다시 띄운다."""
    seen = {}
    while not stop.wait(1):
        now = time.time()
        for svc in ("settlement-worker", "settlement-api"):
            for c in ids(svc, "-a", "--filter", "status=exited"):
                seen.setdefault(c, now)
                if now - seen[c] >= recovery:
                    sh("docker", "start", c)
                    seen.pop(c)
                    log(f"restarted {svc}")


def run_one(name, merchants, days, tx, timeout, recovery):
    point, how, expect, guard, env, inject = MATRIX[name]
    env_all = {**os.environ, "WORK_MICROS_PER_TX": "5000", **env}
    sh("docker", "compose", "down", "-v", "--remove-orphans")
    up = subprocess.run(["docker", "compose", "up", "-d", "--scale", f"settlement-worker={WORKERS}", "postgres",
                         "rabbitmq", "settlement-api", "settlement-worker"], env=env_all, capture_output=True, text=True,
                        timeout=300)
    if up.returncode != 0:
        sys.exit(f"docker compose up failed: {up.stderr[-500:]}")
    wait_for_api(BASE, timeout=180)
    call(BASE, "POST", f"/admin/seed?merchants={merchants}&days={days}&txPerMerchantDay={tx}&startDate=2026-03-01")

    events = []
    t0 = time.time()

    def log(msg):
        events.append(f"{time.time() - t0:5.1f}s {msg}")

    stop = threading.Event()
    sup = threading.Thread(target=supervisor, args=(stop, log, recovery), daemon=True)
    sup.start()
    if inject:
        threading.Thread(target=inject, args=(log,), daemon=True).start()

    start = date(2026, 3, 1)
    for m in range(1, merchants + 1):
        for d in range(days):
            call(BASE, "POST", "/settlement-jobs", {"merchantId": f"M{m:04d}",
                                                    "settlementDate": (start + timedelta(days=d)).isoformat()})
    rep = {}
    while time.time() - t0 < timeout:
        _, rep = call(BASE, "GET", "/reconciliation")
        if rep.get("allSettled"):
            break
        time.sleep(2)
    elapsed = time.time() - t0
    time.sleep(3)
    stop.set()
    sup.join()

    max_latency = float(sh(*PSQL, "SELECT coalesce(max(extract(epoch FROM completed_at - accepted_at)), 0) "
                                  "FROM settlement_jobs") or 0)
    dlq = dlq_depth()
    # 앱 내부 장애 주입(CHAOS_*)은 하네스가 모르므로 로그에서 센다. 같은 컨테이너가 재시작돼도 로그는 남는다.
    chaos = sh("docker", "compose", "logs", "--no-log-prefix", "settlement-api", "settlement-worker").count("CHAOS:")
    ok = bool(rep.get("consistent") and rep.get("allSettled"))
    if name == "poison":
        ok = ok and dlq == 1
    if name == "mass-eviction":
        ok = ok and max_latency <= SLO_S
    return {"scenario": name, "point": point, "how": how, "expect": expect, "guard": guard,
            "jobs": rep.get("acceptedJobs"), "completed": rep.get("completedJobs"),
            "lost": rep.get("statusResultMismatch"), "amount_mismatch": rep.get("amountMismatch"),
            "double_completed": rep.get("doubleCompleted"), "duplicates_absorbed": rep.get("duplicateDeliveriesAbsorbed"),
            "dlq": dlq, "max_latency_s": round(max_latency, 1), "elapsed_s": round(elapsed, 1),
            "injections": sum(1 for e in events if "killed" in e or "restarted r" in e or "restarted p" in e
                              or "poison" in e) + chaos,
            "result": "PASS" if ok else "FAIL", "events": " | ".join(events[:12])}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenarios", nargs="*", help="비우면 전체: " + ", ".join(MATRIX))
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--merchants", type=int, default=50)
    ap.add_argument("--days", type=int, default=8)
    ap.add_argument("--tx", type=int, default=100)
    ap.add_argument("--recovery", type=float, default=5, help="종료된 워커·API 를 다시 띄우기까지(초)")
    ap.add_argument("--timeout", type=int, default=600)
    a = ap.parse_args()
    unknown = [s for s in a.scenarios if s not in MATRIX]
    if unknown:
        sys.exit(f"알 수 없는 행: {unknown}")

    rows = []
    for r in range(a.rounds):
        for name in a.scenarios or list(MATRIX):
            print(f"== {name} (round {r + 1})", flush=True)
            row = run_one(name, a.merchants, a.days, a.tx, a.timeout, a.recovery)
            row["round"] = r + 1
            print(json.dumps({k: v for k, v in row.items() if k != "events"}, ensure_ascii=False), flush=True)
            print("   ", row["events"], flush=True)
            rows.append(row)

    os.makedirs("data/fault-matrix", exist_ok=True)
    path = f"data/fault-matrix/{int(time.time())}.csv"
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print("\n| 죽는 지점 | 주입 | 작업 | 유실 | 금액 불일치 | 이중 확정 | 흡수한 중복 | DLQ | 최대 완료시간(초) | 결과 |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for x in rows:
        print(f"| {x['point']} | {x['how']} | {x['completed']}/{x['jobs']} | {x['lost']} | {x['amount_mismatch']} | "
              f"{x['double_completed']} | {x['duplicates_absorbed']} | {x['dlq']} | {x['max_latency_s']} | {x['result']} |")
    print("saved", path)
    sys.exit(0 if all(x["result"] == "PASS" for x in rows) else 1)


if __name__ == "__main__":
    main()
