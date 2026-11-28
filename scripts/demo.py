#!/usr/bin/env python3
"""
교수님 시연용: 스팟 3대가 한꺼번에 회수돼도 돈은 틀리지 않고, 몰래 바꾸면 바로 드러난다 (약 4분).

  mvn -q package -DskipTests
  docker compose up -d --build postgres rabbitmq settlement-api prometheus grafana
  python3 scripts/demo.py                 # 터미널에 단계가 나온다
  # 브라우저: http://localhost:3000/d/spot-swallow?refresh=5s&from=now-5m&to=now  (로그인 없이 열림)

흐름
  1. 제어기(제안 기법 + AWS 재조정)가 스팟 워커를 띄운다. 새 워커는 준비까지 --boot 초 걸린다 (클라우드 부팅 흉내).
  2. 초당 --rate 건씩 작업을 넣는다.
  3. 스팟 워커 --kill 대를 경고 없이 동시에 죽인다 (docker kill -9). 처리 중이던 작업은 Ack 전이라 큐로 돌아간다.
  4. 제어기가 교체 워커를 띄우고, 쌓인 작업을 처리한다.
  5. 부하가 끝나면 대사: 접수 = 확정, 유실 0, 중복 0, 금액 불일치 0.
  6. (--tamper) 확정 결과 1건을 DB 에서 직접 1원 바꾼다 → 대사·알림이 잡고, 감사 체인에 누가 바꿨는지 남는다 → 되돌린다.

단계마다 data/demo/stage.json 을 갱신한다. scripts/record_demo.js 가 이 파일을 보고 화면 자막을 바꾼다.
"""
import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from e2e import call, wait_for_api  # noqa: E402

STAGE = "data/demo/stage.json"
METRIC = "sum(swallow_recon_amount_mismatch + swallow_recon_status_result_mismatch + swallow_recon_double_completed + swallow_recon_audit_violations)"
PSQL = ["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "swallow", "-d", "swallow", "-At", "-c"]
_seq = 0


def stage(title, detail="", end=False):
    global _seq
    _seq += 1
    os.makedirs(os.path.dirname(STAGE), exist_ok=True)
    tmp = STAGE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"seq": _seq, "title": title, "detail": detail, "end": end, "at": time.time()}, f, ensure_ascii=False)
    os.replace(tmp, STAGE)
    print(f"\n=== [{time.strftime('%H:%M:%S')}] {title}\n    {detail}", flush=True)


def sh(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()


def spot_workers():
    return sh("docker", "ps", "-q", "--filter", "label=swallow.kind=spot").split()


def prom(query):
    """Prometheus 에서 값 하나 (대시보드가 보는 것과 같은 값)."""
    import urllib.parse
    import urllib.request
    try:
        url = "http://localhost:9090/api/v1/query?query=" + urllib.parse.quote(query)
        with urllib.request.urlopen(url, timeout=5) as r:
            res = json.loads(r.read())["data"]["result"]
        return float(res[0]["value"][1]) if res else 0.0
    except Exception:
        return -1.0


def wait_prom(query, pred, timeout):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred(prom(query)):
            return time.time() - t0
        time.sleep(1)
    return None


def recon(base):
    return call(base, "GET", "/reconciliation")[1]


def summary(r):
    return (f"접수 {r['acceptedJobs']} · 확정 {r['results']} · 유실 {r['statusResultMismatch']} · "
            f"이중 확정 {r['doubleCompleted']} · 금액 불일치 {r['amountMismatch']} · "
            f"흡수한 중복 전달 {r['duplicateDeliveriesAbsorbed']} → {'정상' if r['consistent'] else '불일치'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8080")
    ap.add_argument("--boot", type=float, default=60, help="새 워커 준비 시간(초)")
    ap.add_argument("--rate", type=float, default=1.8)
    ap.add_argument("--load-seconds", type=float, default=150)
    ap.add_argument("--kill-at", type=float, default=30, help="부하 시작 후 몇 초에 죽일지")
    ap.add_argument("--kill", type=int, default=3)
    ap.add_argument("--work-micros", type=int, default=10000, help="거래 1건당 처리 시간(마이크로초). 작업 1건 약 2초")
    ap.add_argument("--no-tamper", action="store_true")
    a = ap.parse_args()

    wait_for_api(a.base)
    stage("준비", f"제어기가 스팟 워커를 띄우는 중 (새 워커 준비 {a.boot:.0f}초)")
    os.makedirs("data/demo", exist_ok=True)
    log = open("data/demo/controller.log", "w")
    ctl = subprocess.Popen([sys.executable, "-m", "controller.main", "--policy", "proposed+rebalance", "--no-injector",
                            "--boot-median", str(a.boot), "--boot-sigma", "0.05", "--duration",
                            str(a.load_seconds + 600), "--metrics-port", "9108", "--log", "data/demo/controller.csv"],
                           stdout=log, stderr=subprocess.STDOUT,
                           env={**os.environ, "WORK_MICROS_PER_TX": str(a.work_micros)})
    try:
        deadline = time.time() + 150
        while time.time() < deadline and "initial workers ready" not in open("data/demo/controller.log").read():
            time.sleep(2)
        time.sleep(5)
        stage("평상시", f"초당 {a.rate}건 정산 작업 접수. 스팟 워커 {len(spot_workers())}대가 처리 중")
        load = subprocess.Popen([sys.executable, "scripts/load.py", "--base", a.base, "--rate", str(a.rate),
                                 "--duration", str(a.load_seconds), "--merchants", "100", "--drain-timeout", "600"],
                                stdout=open("data/demo/load.log", "w"), stderr=subprocess.STDOUT)
        time.sleep(a.kill_at)

        victims = spot_workers()[:a.kill]
        for cid in victims:
            subprocess.run(["docker", "kill", "-s", "KILL", cid], capture_output=True)
        stage(f"스팟 {len(victims)}대 동시 회수", "경고 없이 강제 종료. 처리 중이던 작업은 Ack 전이라 큐로 돌아간다. 큐가 쌓이기 시작")
        time.sleep(15)
        stage("제어기가 교체", f"회수를 보고 새 워커를 요청. 준비까지 약 {a.boot:.0f}초, 그동안 남은 워커가 처리")
        load.wait()
        stage("부하 끝, 남은 작업 처리 중", "큐가 비면 대사로 돈이 맞는지 확인한다")
        deadline = time.time() + 600
        while time.time() < deadline and not recon(a.base).get("allSettled"):
            time.sleep(3)
        r = recon(a.base)
        stage("대사: 돈이 맞는가", summary(r))
        time.sleep(20)

        if not a.no_tamper:
            job = sh(*PSQL, "SELECT job_id FROM settlement_results ORDER BY job_id LIMIT 1")
            sh(*PSQL, f"UPDATE settlement_results SET gross_amount = gross_amount + 1, net_amount = net_amount + 1 "
                      f"WHERE job_id = '{job}'")
            stage("누군가 DB 에서 결과 1건을 1원 바꿈", f"{job}. 워커 계정은 이걸 할 수 없다 (권한 거부). 관리자 계정으로 직접 변경")
            time.sleep(6)
            r = recon(a.base)
            who = sh(*PSQL, f"SELECT db_user || ' 계정, ' || to_char(recorded_at AT TIME ZONE 'Asia/Seoul', 'HH24:MI:SS') "
                            f"|| ', ' || action FROM result_audit WHERE job_id = '{job}' ORDER BY seq DESC LIMIT 1")
            stage("대사가 잡고, 감사 체인에 누가 남음",
                  f"대사: 금액 불일치 {r['amountMismatch']}. 감사 기록: {who}. 알림 지표는 15~25초 안에 반영")
            time.sleep(6)
            waited = wait_prom(METRIC, lambda v: v > 0, 90)
            stage("대시보드 '대사 불일치' 빨간불",
                  "알림 지표 반영 (대사 15초 주기 + 수집 5초). 사람이 대시보드만 봐도 알 수 있다" if waited is not None
                  else "알림 지표 반영 지연")
            time.sleep(15)
            sh(*PSQL, f"UPDATE settlement_results SET gross_amount = gross_amount - 1, net_amount = net_amount - 1 "
                      f"WHERE job_id = '{job}'")
            r = recon(a.base)
            stage("입력 거래로 다시 계산한 값으로 복구", summary(r) + " (바꾼 기록은 감사 체인에 그대로 남는다)")
            wait_prom(METRIC, lambda v: v == 0, 90)
            time.sleep(8)
        stage("끝", "회수에도 유실·중복 0, 몰래 바꾸면 드러난다", end=True)
    finally:
        ctl.terminate()
        try:
            ctl.wait(timeout=20)
        except subprocess.TimeoutExpired:
            ctl.kill()


if __name__ == "__main__":
    main()
