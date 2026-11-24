#!/usr/bin/env python3
"""
알림 점검(drill): 장애를 하나씩 일부러 만들고, Grafana 알림이 울리기까지(감지) 걸린 시간과
복구 후 꺼지기까지 걸린 시간을 잰다. GameDay 전에 "알림이 실제로 울리는가"를 확인하는 용도다.

  docker compose up -d --scale settlement-worker=3
  python3 scripts/load.py --rate 1.0 --duration 3600 &                 # 부하가 있어야 작업 나이·아웃박스가 움직인다
  python3 scripts/alert_drill.py dlq recon outbox job-age              # 제어기 없이
  python3 -m controller.main --no-injector --boot-median 60 &          # 워커 부족 알림은 제어기 지표가 필요
  python3 scripts/alert_drill.py workers

결과는 표로 출력하고 data/drill/<시각>.csv 에도 남긴다.
"""
import argparse
import base64
import csv
import json
import os
import subprocess
import time
import urllib.request

GRAFANA = "http://localhost:3000"
RABBIT = "http://localhost:15672"
RABBIT_AUTH = "Basic " + base64.b64encode(b"swallow:swallow").decode()
PSQL = ["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "swallow", "-d", "swallow", "-At", "-c"]

RULE = {"dlq": "dlq", "recon": "recon-mismatch", "outbox": "outbox-stuck", "job-age": "job-age",
        "workers": "workers-below-nbase"}


def http(url, method="GET", body=None, auth=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **({"Authorization": auth} if auth else {})})
    with urllib.request.urlopen(req, timeout=10) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def rule_states():
    """규칙 uid → 상태(inactive | pending | firing)."""
    d = http(f"{GRAFANA}/api/prometheus/grafana/api/v1/rules")
    uid_by_title = {r["title"]: r["uid"] for r in http(f"{GRAFANA}/api/v1/provisioning/alert-rules")}
    out = {}
    for g in d["data"]["groups"]:
        for r in g["rules"]:
            out[uid_by_title.get(r["name"], r["name"])] = r["state"]
    return out


def wait_state(uid, want, timeout):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if rule_states().get(uid) == want:
            return time.time() - t0
        time.sleep(1)
    return None


def sh(*cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print("  !", " ".join(cmd[:4]), r.stderr.strip()[:200], flush=True)
    return r.stdout.strip()


def psql(sql):
    return sh(*PSQL, sql)


def workers():
    return sh("docker", "ps", "-q", "--filter", "label=com.docker.compose.service=settlement-worker").split()


# ------------------------------------------------------------- 장애 만들기 / 되돌리기
def inject_dlq():
    # 존재하지 않는 작업 ID: 워커가 처리할 수 없어 DLQ 로 보낸다(독성 메시지)
    http(f"{RABBIT}/api/exchanges/%2F/amq.default/publish", "POST",
         {"properties": {"delivery_mode": 2}, "routing_key": "settlement.jobs",
          "payload": json.dumps({"jobId": f"DRILL-{int(time.time())}"}), "payload_encoding": "string"}, RABBIT_AUTH)
    return lambda: http(f"{RABBIT}/api/queues/%2F/settlement.jobs.dlq/contents", "DELETE", auth=RABBIT_AUTH)


def inject_recon():
    # 확정된 정산 결과 하나의 금액을 1원 바꾼다 (원장 변조 흉내)
    job = psql("UPDATE settlement_results SET gross_amount = gross_amount + 1, net_amount = net_amount + 1 "
               "WHERE job_id = (SELECT job_id FROM settlement_results ORDER BY job_id LIMIT 1) RETURNING job_id")
    if not job:
        raise RuntimeError("정산 결과가 아직 없다. 부하를 먼저 넣어야 한다")
    job = job.splitlines()[0]
    return lambda: psql(f"UPDATE settlement_results SET gross_amount = gross_amount - 1, net_amount = net_amount - 1 WHERE job_id = '{job}'")


def inject_outbox():
    # 브로커를 멈추면 발행기가 아웃박스를 비우지 못한다
    sh("docker", "compose", "pause", "rabbitmq")
    return lambda: sh("docker", "compose", "unpause", "rabbitmq")


def inject_job_age():
    # 워커를 모두 멈춘다 (제어기 없이 돌릴 것: 제어기는 곧바로 새 워커를 띄운다)
    ids = workers()
    sh("docker", "stop", "-t", "1", *ids)
    return lambda: sh("docker", "start", *ids)


def inject_workers():
    # 동시 대량 회수: 제어기가 띄운 워커까지 모두 강제 종료
    ids = sh("docker", "ps", "-q", "--filter", "label=swallow.kind").split() + workers()
    sh("docker", "kill", *ids)
    return lambda: None   # 제어기가 다시 띄운다


INJECT = {"dlq": inject_dlq, "recon": inject_recon, "outbox": inject_outbox, "job-age": inject_job_age,
          "workers": inject_workers}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenarios", nargs="+", choices=list(INJECT))
    ap.add_argument("--timeout", type=int, default=420, help="알림·복구 대기 한도(초)")
    a = ap.parse_args()

    rows = []
    for name in a.scenarios:
        uid = RULE[name]
        if rule_states().get(uid) != "inactive":
            print(f"[{name}] 시작 전 알림이 이미 켜져 있어 건너뜀")
            continue
        print(f"[{name}] 장애 주입", flush=True)
        t_inject = time.time()
        recover = INJECT[name]()
        detect = wait_state(uid, "firing", a.timeout)
        print(f"[{name}] 감지 {detect if detect is None else round(detect)}초", flush=True)
        recover()
        clear = wait_state(uid, "inactive", a.timeout)
        print(f"[{name}] 복구 후 알림 해제 {clear if clear is None else round(clear)}초", flush=True)
        rows.append({"scenario": name, "rule": uid, "injected_at": time.strftime("%H:%M:%S", time.localtime(t_inject)),
                     "detect_s": None if detect is None else round(detect, 1),
                     "clear_s": None if clear is None else round(clear, 1)})
        time.sleep(10)

    os.makedirs("data/drill", exist_ok=True)
    path = f"data/drill/{int(time.time())}.csv"
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["scenario"])
        w.writeheader()
        w.writerows(rows)
    print("\n| 장애 | 알림 | 감지(초) | 복구 후 해제(초) |\n|---|---|---|---|")
    for r in rows:
        print(f"| {r['scenario']} | {r['rule']} | {r['detect_s']} | {r['clear_s']} |")
    print("saved", path)


if __name__ == "__main__":
    main()
