#!/usr/bin/env python3
"""
보안 시험: 스팟 워커가 털려도 확정된 결과를 바꿀 수 없는가, 누가 몰래 바꾸면 드러나는가.

전제: 스팟 = 언제 꺼질지 모르고 믿을 수도 없는 서버. 워커 DB 계정(swallow_worker_app)이 새도 되도록 만든다.

  docker compose up -d --build --scale settlement-worker=3 postgres rabbitmq settlement-api settlement-worker
  python3 scripts/e2e.py --merchants 20 --days 2
  python3 scripts/security_drill.py --out docs/data/security-drill-2026-11-25.csv

시험 (모두 통과해야 RESULT: PASS):
  A. 워커 계정으로 결과·이력·감사 기록을 바꾸거나 지우려 하면 거부된다. 트리거를 끄거나 감사 함수를 직접 부를 수도 없다.
  B. 관리자 계정으로 결과를 바꾸면(GameDay 1회차와 같은 변조) 감사 체인에 "누가" 남고, 대사가 금액 불일치로 잡는다.
  C. 관리자가 감사 트리거를 끄고 금액이 아닌 값(worker_id)을 몰래 바꾸면, 금액 대사로는 못 잡지만 감사 체인이 잡는다.
  D. 감사 기록은 소유자도 지울 수 없다. 트리거를 끄고 감사 기록 자체를 고치면 해시 체인이 끊겨 드러난다.
  E. 관리자가 흔적을 완전히 지우려고 감사 기록을 고친 뒤 체인 전체를 다시 계산하면, DB 안의 검사는 모두 통과하지만
     DB 밖에 남긴 끝 해시(앵커)와 달라 드러난다.
D·E 는 감사 기록을 되돌릴 수 없게 바꾸므로 맨 마지막에 한다. 끝나면 scripts/reset.sh 로 초기화한다.
"""
import argparse
import csv
import json
import subprocess
import sys
import urllib.request

ADMIN = ["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "swallow", "-d", "swallow", "-At", "-v",
         "ON_ERROR_STOP=1", "-c"]
# 워커 계정. 컨테이너 안 로컬 접속이라 비밀번호 확인은 생략되지만, 권한은 이 계정 그대로다.
# (비밀번호로 실제 접속하는 것은 워커 컨테이너가 매번 한다: e2e 가 통과하면 확인된 것)
WORKER = ["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "swallow_worker_app", "-d", "swallow", "-At",
          "-v", "ON_ERROR_STOP=1", "-c"]

rows = []


def run(cmd, sql):
    r = subprocess.run(cmd + [sql], capture_output=True, text=True)
    return r.returncode, (r.stdout.strip() or r.stderr.strip())


def post(base, path):
    req = urllib.request.Request(base + path, data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def recon(base):
    with urllib.request.urlopen(base + "/reconciliation", timeout=30) as resp:
        return json.loads(resp.read())


def check(group, name, ok, detail):
    rows.append({"group": group, "check": name, "result": "PASS" if ok else "FAIL", "detail": detail[:200]})
    print(f"  [{'PASS' if ok else 'FAIL'}] {group} {name}: {detail[:160]}", flush=True)


def refused(name, sql):
    code, out = run(WORKER, sql)
    ok = code != 0 and ("permission denied" in out or "must be owner" in out)
    check("A", name, ok, out.splitlines()[-1] if out else "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8080")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    job = run(ADMIN, "SELECT job_id FROM settlement_results ORDER BY job_id LIMIT 1")[1]
    if not job:
        sys.exit("확정된 결과가 없다. 먼저 scripts/e2e.py 로 작업을 처리한다.")
    r0 = recon(args.api)
    check("0", "시작 상태", r0["consistent"] and r0["auditChain"]["ok"]
          and r0["auditChain"]["entries"] == r0["results"],
          f"results={r0['results']} auditEntries={r0['auditChain']['entries']} consistent={r0['consistent']}")
    a0 = post(args.api, "/admin/audit-anchor")
    check("0", "체인 끝 해시를 DB 밖에 남김", a0["anchors"] >= 1 and a0["anchorMismatch"] == 0,
          f"anchors={a0['anchors']} lastAnchoredSeq={a0.get('lastAnchoredSeq')}")

    print("A. 워커 계정(스팟이 털렸다고 가정)", flush=True)
    refused("결과 UPDATE", f"UPDATE settlement_results SET net_amount = net_amount + 1 WHERE job_id = '{job}'")
    refused("결과 DELETE", f"DELETE FROM settlement_results WHERE job_id = '{job}'")
    refused("이력 UPDATE", "UPDATE job_events SET detail = 'x'")
    refused("이력 DELETE", "DELETE FROM job_events")
    refused("입력 거래 UPDATE", "UPDATE transactions SET amount = amount + 1")
    refused("작업 내용 UPDATE (완료 표시 외 칸)", f"UPDATE settlement_jobs SET merchant_id = 'X' WHERE job_id = '{job}'")
    refused("감사 기록 읽기", "SELECT count(*) FROM result_audit")
    refused("감사 기록 직접 추가", "INSERT INTO result_audit (job_id, action, payload, db_user, recorded_at, prev_hash, hash) "
            "VALUES ('x','INSERT','x','x',now(),repeat('0',64),repeat('0',64))")
    refused("감사 함수 직접 호출 (가짜 기록)", "SELECT result_audit_record('x', 'INSERT', 'fake')")
    refused("감사 트리거 끄기", "ALTER TABLE settlement_results DISABLE TRIGGER trg_settlement_results_audit")
    # GRANT 는 오류 대신 "no privileges were granted" 경고만 내므로, 실제로 권한이 생겼는지 따로 본다
    _, out = run(WORKER, "GRANT UPDATE ON settlement_results TO swallow_worker_app")
    has = run(ADMIN, "SELECT has_table_privilege('swallow_worker_app', 'settlement_results', 'UPDATE')")[1]
    check("A", "권한 스스로 올리기", has == "f", f"{out.splitlines()[-1] if out else ''} / UPDATE 권한={has}")

    print("B. 관리자 계정으로 결과 변경 (GameDay 1회차와 같은 변조)", flush=True)
    run(ADMIN, f"UPDATE settlement_results SET gross_amount = gross_amount + 1, net_amount = net_amount + 1 "
               f"WHERE job_id = '{job}'")
    r = recon(args.api)
    who = run(ADMIN, f"SELECT db_user || ' ' || action FROM result_audit WHERE job_id = '{job}' ORDER BY seq DESC LIMIT 1")[1]
    check("B", "누가 바꿨는지 남음", who == "swallow UPDATE", f"마지막 감사 기록: {who}")
    check("B", "대사가 금액 불일치로 잡음", r["amountMismatch"] == 1 and not r["consistent"],
          f"amountMismatch={r['amountMismatch']} consistent={r['consistent']} audit.ok={r['auditChain']['ok']}")
    run(ADMIN, f"UPDATE settlement_results SET gross_amount = gross_amount - 1, net_amount = net_amount - 1 "
               f"WHERE job_id = '{job}'")
    r = recon(args.api)
    check("B", "되돌린 뒤 정상, 변경 이력은 남음", r["consistent"]
          and r["auditChain"]["changesAfterCommit"].get("UPDATE by swallow") == 2,
          f"consistent={r['consistent']} changes={r['auditChain']['changesAfterCommit']}")

    print("C. 관리자가 감사 트리거를 끄고 금액이 아닌 값을 몰래 변경", flush=True)
    run(ADMIN, "ALTER TABLE settlement_results DISABLE TRIGGER trg_settlement_results_audit; "
               f"UPDATE settlement_results SET worker_id = 'someone-else' WHERE job_id = '{job}'; "
               "ALTER TABLE settlement_results ENABLE TRIGGER trg_settlement_results_audit;")
    r = recon(args.api)
    check("C", "금액 대사로는 안 보임", r["amountMismatch"] == 0, f"amountMismatch={r['amountMismatch']}")
    check("C", "감사 체인이 잡음", r["auditChain"]["untracked"] == 1 and not r["consistent"],
          f"untracked={r['auditChain']['untracked']} consistent={r['consistent']}")

    print("D. 감사 기록 자체를 고치기", flush=True)
    code, out = run(ADMIN, "DELETE FROM result_audit")
    check("D", "소유자도 감사 기록 DELETE 거부", code != 0 and "append-only" in out, out.splitlines()[-1] if out else "")
    code, out = run(ADMIN, "TRUNCATE result_audit")
    check("D", "소유자도 감사 기록 TRUNCATE 거부", code != 0 and "append-only" in out, out.splitlines()[-1] if out else "")
    # 트리거를 끄고, C 에서 몰래 바꾼 값에 맞춰 감사 기록을 고쳐 흔적을 지우려 한다
    run(ADMIN, "ALTER TABLE result_audit DISABLE TRIGGER trg_result_audit_append_only; "
               "UPDATE result_audit a SET payload = result_payload(r) FROM settlement_results r "
               f"WHERE a.job_id = r.job_id AND a.job_id = '{job}' "
               f"AND a.seq = (SELECT max(seq) FROM result_audit WHERE job_id = '{job}'); "
               "ALTER TABLE result_audit ENABLE TRIGGER trg_result_audit_append_only;")
    r = recon(args.api)
    check("D", "흔적을 지우려고 감사 기록을 고치면 해시 체인이 끊김", r["auditChain"]["brokenLinks"] >= 1
          and r["auditChain"]["untracked"] == 0 and not r["consistent"],
          f"brokenLinks={r['auditChain']['brokenLinks']} untracked={r['auditChain']['untracked']} "
          f"consistent={r['consistent']}")

    print("E. 흔적을 완전히 지우려고 감사 기록을 고치고 체인 전체를 다시 계산", flush=True)
    # C 에서 몰래 바꾼 worker_id 에 맞춰 그 작업의 감사 기록을 모두 고치고, 처음부터 해시를 다시 이어 붙인다
    code, out = run(ADMIN, "ALTER TABLE result_audit DISABLE TRIGGER trg_result_audit_append_only; "
                    "UPDATE result_audit SET payload = regexp_replace(payload, '[|][^|]*$', '|someone-else') "
                    f"WHERE job_id = '{job}' AND action <> 'DELETE'; "
                    "DO $$ DECLARE r record; prev CHAR(64) := repeat('0', 64); h CHAR(64); BEGIN "
                    "FOR r IN SELECT * FROM result_audit ORDER BY seq LOOP "
                    "h := result_audit_hash(prev, r.seq, r.job_id, r.action, r.payload, r.db_user, r.recorded_at); "
                    "UPDATE result_audit SET prev_hash = prev, hash = h WHERE seq = r.seq; prev := h; "
                    "END LOOP; END $$; "
                    "ALTER TABLE result_audit ENABLE TRIGGER trg_result_audit_append_only;")
    r = recon(args.api)
    a = r["auditChain"]
    check("E", "DB 안의 검사는 모두 속음", code == 0 and a["brokenLinks"] == 0 and a["untracked"] == 0
          and r["amountMismatch"] == 0, f"brokenLinks={a['brokenLinks']} untracked={a['untracked']} "
          f"amountMismatch={r['amountMismatch']}")
    check("E", "DB 밖 앵커와 달라 드러남", a["anchorMismatch"] >= 1 and not r["consistent"],
          f"anchorMismatch={a['anchorMismatch']} consistent={r['consistent']}")

    passed = all(x["result"] == "PASS" for x in rows)
    if args.out:
        with open(args.out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["group", "check", "result", "detail"])
            w.writeheader()
            w.writerows(rows)
    print(f"{sum(x['result'] == 'PASS' for x in rows)}/{len(rows)} 통과")
    print("RESULT:", "PASS" if passed else "FAIL")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
