#!/usr/bin/env python3
"""
End-to-end 검증: 합성 거래 적재 → 정산 작업 접수 → 전부 처리될 때까지 대기 → 대사 결과 확인.

  python3 scripts/e2e.py                       # 기본: 가맹점 50 x 2일 = 작업 100건
  python3 scripts/e2e.py --kill-every 3        # 3초마다 워커 컨테이너 하나를 docker kill (스팟 회수 흉내)
  python3 scripts/e2e.py --resubmit            # 같은 요청을 한 번 더 보내 API 멱등성까지 확인

대사 결과가 consistent=true 이고 allSettled=true 이면 종료 코드 0.
표준 라이브러리만 사용한다.
"""
import argparse
import http.client
import json
import random
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import date, timedelta


def call(base, method, path, body=None, retries=200):
    """API 가 장애 주입으로 재시작 중이면 재시도한다.
    POST /settlement-jobs 는 멱등이라 응답을 못 받고 다시 보내도 작업이 두 번 생기지 않는다."""
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(retries):
        req = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except (urllib.error.URLError, http.client.HTTPException, ConnectionError) as e:
            if isinstance(e, urllib.error.HTTPError) or attempt == retries - 1:
                raise
            time.sleep(3)


def wait_for_api(base, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            call(base, "GET", "/reconciliation")
            return
        except Exception:
            time.sleep(3)
    sys.exit("API did not become ready")


WORKER_FILTER = ["--filter", "label=com.docker.compose.service=settlement-worker"]


def docker_ids(*extra):
    return subprocess.run(["docker", "ps", "-q", *WORKER_FILTER, *extra],
                          capture_output=True, text=True).stdout.split()


def evictor(interval, stop):
    """주기적으로 실행 중인 워커 하나를 SIGKILL 한다 (경고 없는 스팟 회수)."""
    kills = 0
    while not stop.wait(interval):
        running = docker_ids()
        if running:
            victim = random.choice(running)
            subprocess.run(["docker", "kill", "-s", "KILL", victim], capture_output=True)
            kills += 1
            print(f"  [evict] killed worker {victim[:12]} (total {kills})", flush=True)
    evictor.kills = kills


def replacer(recovery, stop):
    """Auto Scaling 그룹 역할: 종료된 워커를 발견하면 복구 시간(recovery)만큼 뒤에 다시 띄운다.
    장애 주입으로 스스로 죽은 워커와 evictor 가 죽인 워커 모두 대상이다."""
    seen = {}
    replaced = 0
    while not stop.wait(1):
        now = time.time()
        for cid in docker_ids("-a", "--filter", "status=exited"):
            seen.setdefault(cid, now)
            if now - seen[cid] >= recovery:
                subprocess.run(["docker", "start", cid], capture_output=True)
                seen.pop(cid)
                replaced += 1
    replacer.replaced = replaced


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://localhost:8080")
    p.add_argument("--merchants", type=int, default=50)
    p.add_argument("--days", type=int, default=2)
    p.add_argument("--tx", type=int, default=200, help="가맹점·일자별 거래 수")
    p.add_argument("--start", default="2026-11-01")
    p.add_argument("--skip-seed", action="store_true")
    p.add_argument("--resubmit", action="store_true")
    p.add_argument("--kill-every", type=float, default=0, help="초 단위. 0 이면 강제 종료 없음")
    p.add_argument("--recovery", type=float, default=3, help="워커 교체까지 걸리는 시간(초), 부팅 시간 별도")
    p.add_argument("--timeout", type=int, default=600)
    a = p.parse_args()

    wait_for_api(a.base)
    if not a.skip_seed:
        _, seeded = call(a.base, "POST",
                         f"/admin/seed?merchants={a.merchants}&days={a.days}&txPerMerchantDay={a.tx}&startDate={a.start}")
        print("seeded:", seeded)

    stop = threading.Event()
    killer = None
    supervisor = threading.Thread(target=replacer, args=(a.recovery, stop), daemon=True)
    supervisor.start()
    if a.kill_every > 0:
        killer = threading.Thread(target=evictor, args=(a.kill_every, stop), daemon=True)
        killer.start()

    start = date.fromisoformat(a.start)
    requests = [{"merchantId": f"M{m:04d}", "settlementDate": (start + timedelta(days=d)).isoformat()}
                for m in range(1, a.merchants + 1) for d in range(a.days)]
    t0 = time.time()
    created = sum(1 for r in requests if call(a.base, "POST", "/settlement-jobs", r)[1]["created"])
    print(f"submitted {len(requests)} jobs ({created} newly created)")
    if a.resubmit:
        again = sum(1 for r in requests if call(a.base, "POST", "/settlement-jobs", r)[1]["created"])
        print(f"resubmitted {len(requests)} identical requests -> {again} newly created (expected 0)")

    report = {}
    while time.time() - t0 < a.timeout:
        _, report = call(a.base, "GET", "/reconciliation")
        if report["allSettled"]:
            break
        print(f"  {int(time.time() - t0):4d}s  completed {report['completedJobs']}/{report['acceptedJobs']}"
              f"  absorbed-duplicates {report['duplicateDeliveriesAbsorbed']}", flush=True)
        time.sleep(3)

    stop.set()
    supervisor.join()
    print(f"workers replaced during run: {getattr(replacer, 'replaced', 0)}")
    if killer:
        killer.join()
        print(f"workers killed during run: {getattr(evictor, 'kills', 0)}")
    print(f"elapsed: {time.time() - t0:.1f}s")
    print(json.dumps(report, indent=2))
    ok = report.get("consistent") and report.get("allSettled")
    print("RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
