#!/usr/bin/env python3
"""
GameDay 장애 주입기 (플랜 "장애 대응 경험"). 대응하는 사람은 무엇이 주입됐는지 모른 채 대시보드·알림·런북만으로 복구한다.

  docker compose up -d --scale settlement-worker=3
  python3 scripts/load.py --rate 1.0 --duration 3600 &
  python3 scripts/gameday.py start            # 2~10분 사이 무작위 시각에 4종 중 1~2개 장애를 몰래 주입
  python3 scripts/gameday.py detected         # 이상을 알아챈 순간 (알림을 봤을 때) 실행
  python3 scripts/gameday.py recovered        # 복구하고 대사 consistent 를 확인한 뒤 실행
  python3 scripts/gameday.py reveal           # 끝난 뒤 정답과 MTTD/MTTR 확인

기록은 data/gameday/<회차>.json. 정답은 reveal 전에는 출력하지 않는다.
"""
import json
import os
import random
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(__file__))
from alert_drill import inject_dlq, inject_outbox, inject_job_age, inject_recon  # noqa: E402

DIR = "data/gameday"
CURRENT = os.path.join(DIR, "current")


def sh(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()


def kill_half_workers():
    ids = sh("docker", "ps", "-q", "--filter", "label=com.docker.compose.service=settlement-worker").split()
    victims = ids[: max(1, len(ids) // 2)]
    sh("docker", "kill", *victims)


def restart_postgres():
    sh("docker", "compose", "restart", "postgres")


# 이름: 주입 함수. 되돌리는 함수는 쓰지 않는다 (복구는 대응하는 사람이 한다).
FAULTS = {
    "poison": inject_dlq,                 # DLQ 알림
    "ledger-tamper": inject_recon,        # 대사 불일치 알림
    "broker-pause": inject_outbox,        # 아웃박스 알림 (rabbitmq unpause 가 복구)
    "workers-stopped": inject_job_age,    # 작업 나이 알림 (워커 재기동이 복구)
}
# 리허설(2026-11-24)에서 알림이 5분 안에 울리지 않아 뺀 장애. 제어기 없이 돌리면 대응할 신호가 없다.
#   워커 3대 중 1대 남기고 종료: 남은 워커가 부하를 따라가 작업 나이 알림까지 가지 않음
#   PostgreSQL 재시작: 워커가 재시도로 스스로 회복 (장애 주입 매트릭스에서 확인)
QUIET = {"mass-eviction": kill_half_workers, "postgres-restart": restart_postgres}


def path(run):
    return os.path.join(DIR, f"{run}.json")


def load():
    run = open(CURRENT).read().strip()
    return run, json.load(open(path(run)))


def save(run, rec):
    json.dump(rec, open(path(run), "w"), ensure_ascii=False, indent=2)


def start():
    os.makedirs(DIR, exist_ok=True)
    run = time.strftime("%Y%m%d-%H%M%S")
    faults = random.sample(sorted(FAULTS), random.choice([1, 2]))
    delay = random.uniform(120, 600)
    rec = {"run": run, "armed_at": time.time(), "faults": faults, "delay_s": round(delay)}
    save(run, rec)
    open(CURRENT, "w").write(run)
    print(f"GameDay {run} 시작. 지금부터 10분 안에 무언가가 일어난다. 이 창은 닫지 말 것.", flush=True)
    time.sleep(delay)
    # 주입 시각은 주입 전에 저장하고, 끝난 뒤 다시 저장하지 않는다. 대응자가 그 사이에 detected 를 기록하면
    # 끝에 다시 저장할 때 덮어써지기 때문이다 (1회차에서 실제로 감지 시각이 사라졌다).
    rec = json.load(open(path(run)))
    rec["injected_at"] = time.time()
    save(run, rec)
    for f in faults:
        FAULTS[f]()
        time.sleep(random.uniform(0, 30))
    print("(주입기 종료)", flush=True)


def mark(key):
    run, rec = load()
    rec[key] = time.time()
    if key == "recovered_at":
        rep = json.load(urllib.request.urlopen("http://localhost:8080/reconciliation", timeout=10))
        rec["reconciliation"] = {k: rep.get(k) for k in ("consistent", "allSettled", "statusResultMismatch",
                                                          "amountMismatch", "doubleCompleted")}
        print("대사:", rec["reconciliation"])
    save(run, rec)
    print(key, time.strftime("%H:%M:%S"))


def reveal():
    run, rec = load()
    t0 = rec.get("injected_at")
    print("회차:", run, "| 주입:", ", ".join(rec["faults"]))
    if t0 and rec.get("detected_at"):
        print(f"MTTD {rec['detected_at'] - t0:.0f}초")
    if t0 and rec.get("recovered_at"):
        print(f"MTTR {rec['recovered_at'] - t0:.0f}초")
    print("기록:", path(run))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    {"start": start, "detected": lambda: mark("detected_at"), "recovered": lambda: mark("recovered_at"),
     "reveal": reveal}.get(cmd, lambda: sys.exit(__doc__))()
