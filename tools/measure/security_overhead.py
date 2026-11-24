#!/usr/bin/env python3
"""
보안 부트스트랩(Vault 동적 DB 자격 증명)이 복구 시간 Tready 를 얼마나 늘리는지 EC2 에서 잰다.

  1. 측정하는 동안만 Vault + Postgres 호스트를 띄운다 (t4g.small 온디맨드, infra/bootstrap/vault-host.sh)
  2. 같은 서브넷에 스팟 워커를 baseline / dyn-secret 번갈아 띄운다 (시간대 차이가 한쪽에 몰리지 않게)
     dyn-secret 은 infra/bootstrap/dyn-secret.sh 를 JDK 설치 후·앱 기동 전에 실행하고, 그 구간을 bootstrap_s 로 잰다
  3. 끝나면(예외가 나도) Vault 호스트를 지운다. 호스트도 스스로 --host-watchdog-min 뒤 꺼진다

사용:
  python3 tools/measure/security_overhead.py --runs 20 --subnet subnet-...
  python3 tools/measure/summarize_tready.py data/tready-security.csv     # 조건별 표 + 증가분

두 인스턴스 모두 기본 보안 그룹에 들어간다. 기본 보안 그룹은 같은 그룹끼리만 들어오는 연결을 허용하므로
Vault(8200)·Postgres(5432)는 VPC 안의 워커에게만 열리고, 워커는 인바운드 포트를 쓰지 않는다.
"""
import argparse
import base64
import hashlib
import csv
import os
import re
import sys
import time
from datetime import datetime, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ec2_tready as T  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VAULT_LINE = re.compile(r"SWALLOW_VAULT (\w+) ?(\S*)")


def launch_vault_host(ec2, ssm, a, n_ids):
    ami = T.latest_ami(ssm, "arm64")
    ud = open(os.path.join(ROOT, "infra/bootstrap/vault-host.sh")).read()
    ud = ud.replace("__WATCHDOG_MIN__", str(a.host_watchdog_min)).replace("__N_SECRET_IDS__", str(n_ids))
    r = ec2.run_instances(
        ImageId=ami, InstanceType=a.host_type, MinCount=1, MaxCount=1, SubnetId=a.subnet,
        UserData=base64.b64encode(ud.encode()).decode(), InstanceInitiatedShutdownBehavior="terminate",
        TagSpecifications=[{"ResourceType": "instance",
                            "Tags": [{"Key": "Project", "Value": "spot-swallow"},
                                     {"Key": "Purpose", "Value": "vault-host"}]}])
    return r["Instances"][0]["InstanceId"]


def wait_vault(ec2, iid, timeout_s):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        out = ec2.get_console_output(InstanceId=iid, Latest=True).get("Output") or ""
        info = {"wrapped": [], "ca_part": []}
        for k, v in VAULT_LINE.findall(out):
            if k in ("wrapped", "ca_part"):
                info[k].append(v)
            else:
                info[k] = v
        if "error" in info:
            raise RuntimeError(f"vault host setup failed: {info['error']}")
        if "SWALLOW_MARK vault_ready" in out:
            info["ca"] = "".join(info.pop("ca_part"))
            if hashlib.sha256(info["ca"].encode()).hexdigest() != info.get("ca_sha256"):
                raise RuntimeError("CA certificate garbled in console output")
            return info
        time.sleep(15)
    raise TimeoutError("vault host not ready")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--region", default="ap-northeast-2")
    p.add_argument("--subnet", required=True, help="Vault 호스트와 워커를 같은 서브넷(영역)에 둔다")
    p.add_argument("--instance-type", default="c6i.large")
    p.add_argument("--host-type", default="t4g.small")
    p.add_argument("--runs", type=int, default=20, help="조건별 반복 수")
    p.add_argument("--pause", type=int, default=10)
    p.add_argument("--host-watchdog-min", type=int, default=180)
    p.add_argument("--only", choices=["baseline", "dyn-secret"], help="한 조건만 (점검용)")
    p.add_argument("--debug", action="store_true", help="부트스트랩 실패 시 콘솔 출력 끝부분을 보여 준다")
    p.add_argument("--out", default="data/tready-security.csv")
    a = p.parse_args()

    import boto3
    ec2 = boto3.client("ec2", region_name=a.region)
    ssm = boto3.client("ssm", region_name=a.region)
    ami = T.latest_ami(ssm, "x86_64")
    tmpl = open(os.path.join(ROOT, "infra/bootstrap/dyn-secret.sh")).read()

    host = launch_vault_host(ec2, ssm, a, n_ids=a.runs + 5)
    print("vault host", host, flush=True)
    try:
        t0 = time.time()
        v = wait_vault(ec2, host, 1200)
        print(f"vault ready in {time.time() - t0:.0f}s, selftest={v.get('selftest')}, ids={len(v['wrapped'])}",
              flush=True)
        if v.get("selftest") != "ok":
            raise RuntimeError("vault selftest failed")

        os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
        new_file = not os.path.exists(a.out)
        with open(a.out, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=T.FIELDS)
            if new_file:
                w.writeheader()
            for i in range(1, a.runs + 1):
                order = ("baseline", "dyn-secret") if i % 2 else ("dyn-secret", "baseline")
                if a.only:
                    order = (a.only,)
                for variant in order:
                    body = ""
                    if variant == "dyn-secret":
                        body = (tmpl.replace("__VAULT_ADDR__", v["addr"]).replace("__VAULT_CA_B64__", v["ca"])
                                .replace("__ROLE_ID__", v["role_id"]).replace("__WRAPPED__", v["wrapped"][i - 1]))
                    args = SimpleNamespace(instance_type=a.instance_type, subnet=a.subnet, running_timeout=600,
                                           ready_timeout=900, user_data=T.build_user_data(body, 30))
                    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    row = T.one_run(ec2, args, ami, i)
                    err = row.get("error")
                    if variant == "dyn-secret" and not err and "t_secret_ok" not in row:
                        err = "secret not received"
                        if a.debug:
                            print(ec2.get_console_output(InstanceId=row["instance_id"], Latest=True).get("Output", "")[-4000:])
                    out = {"run": i, "variant": variant, "started_at": started, "region": a.region,
                           "instance_type": a.instance_type, "az": row.get("az"), "instance_id": row.get("instance_id"),
                           "error": err, **T.derive(row)}
                    w.writerow(out)
                    f.flush()
                    print(out, flush=True)
                    time.sleep(a.pause)
    finally:
        ec2.terminate_instances(InstanceIds=[host])
        print("terminated vault host", host, flush=True)


if __name__ == "__main__":
    main()
