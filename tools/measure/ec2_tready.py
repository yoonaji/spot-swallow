#!/usr/bin/env python3
"""
EC2 Spot 복구 시간(Tready) 측정.

Tready = 새 워커 생성을 요청한 시점부터 그 워커가 큐를 소비할 수 있게 될 때까지의 시간 (중간보고서 3.2절).
스팟 인스턴스를 반복해서 띄우고 지우며 아래 구간을 나눠 기록한다.

  t_request  RunInstances 호출 직전 (로컬 시계)
  t_running  인스턴스 상태가 running 이 된 것을 처음 본 시점 (로컬 시계, 폴링 간격만큼 오차)
  t_boot     인스턴스 안에서 cloud-init 이 사용자 스크립트를 실행한 시점 (인스턴스 시계, NTP 동기화)
  t_ready    인스턴스 안에서 워커와 같은 JVM 을 띄워 준비를 마친 시점 (인스턴스 시계)

t_boot, t_ready 는 인스턴스가 콘솔 출력에 남긴 "SWALLOW_MARK <이름> <epoch_ms>" 줄에서 읽는다.
콘솔 출력은 몇 분 늦게 올라오므로, 인스턴스를 바로 지우지 않고 마크가 보일 때까지 기다린다.

준비:
  pip install boto3
  aws configure            # 또는 AWS_PROFILE / AWS_ACCESS_KEY_ID 등
  (필요 권한: ec2:RunInstances, DescribeInstances, GetConsoleOutput, TerminateInstances, CreateTags, ssm:GetParameter)

사용:
  python3 tools/measure/ec2_tready.py --dry-run                         # 권한만 확인, 인스턴스 생성 안 함
  python3 tools/measure/ec2_tready.py --runs 20 --instance-type c6i.large --region ap-northeast-2
  결과는 data/tready.csv 에 한 줄씩 추가된다.

비용: c6i.large 스팟은 시간당 수 센트 수준이고, 1회 측정은 보통 5분 이내다. 20회면 1달러 안쪽이다.
안전장치: 모든 인스턴스에 Project=spot-swallow 태그를 달고, 예외가 나도 finally 에서 반드시 종료한다.
"""
import argparse
import base64
import csv
import os
import re
import sys
import time
from datetime import datetime, timezone

MARK = re.compile(r"SWALLOW_MARK (\w+) (\d{13})")

# 인스턴스 안에서 실행되는 스크립트.
# 실제 워커 이미지는 JDK 와 jar 를 미리 구운 AMI 를 쓰겠지만, 여기서는 기본 AMI 에 JDK 를 설치하므로
# "설치" 구간을 따로 기록하고, JVM 기동(실제 워커 준비에 해당)은 설치 이후로 따로 잰다.
USER_DATA = r"""#!/bin/bash
mark() { line="SWALLOW_MARK $1 $(date +%s%3N)"; echo "$line"; echo "$line" > /dev/console 2>/dev/null || true; }
mark boot
dnf install -y -q java-21-amazon-corretto-headless >/dev/null 2>&1
mark jdk_installed
cat > /tmp/Ready.java <<'EOF'
public class Ready { public static void main(String[] a) throws Exception {
  // 워커 기동 비용을 흉내: 클래스 로딩 + 짧은 워밍업
  long s = 0; for (int i = 0; i < 50_000_000; i++) s += i % 7; System.out.println(s); } }
EOF
cd /tmp && java Ready.java >/dev/null 2>&1
mark ready
"""


def now_ms():
    return int(time.time() * 1000)


def latest_ami(ssm, arch):
    name = f"/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-{arch}"
    return ssm.get_parameter(Name=name)["Parameter"]["Value"]


def wait_running(ec2, iid, timeout_s):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = ec2.describe_instances(InstanceIds=[iid])["Reservations"][0]["Instances"][0]
        state = r["State"]["Name"]
        if state == "running":
            return now_ms(), r.get("Placement", {}).get("AvailabilityZone")
        if state in ("terminated", "shutting-down"):
            raise RuntimeError(f"{iid} became {state}: {r.get('StateReason', {}).get('Message')}")
        time.sleep(1)
    raise TimeoutError(f"{iid} not running after {timeout_s}s")


def wait_marks(ec2, iid, timeout_s):
    """콘솔 출력에서 boot / jdk_installed / ready 마크를 찾는다."""
    deadline = time.time() + timeout_s
    marks = {}
    while time.time() < deadline:
        out = ec2.get_console_output(InstanceId=iid, Latest=True).get("Output") or ""
        for name, ts in MARK.findall(out):
            marks.setdefault(name, int(ts))
        if "ready" in marks:
            return marks
        time.sleep(10)
    return marks


def one_run(ec2, args, ami, run_no):
    t_request = now_ms()
    try:
        resp = ec2.run_instances(
            ImageId=ami, InstanceType=args.instance_type, MinCount=1, MaxCount=1,
            InstanceMarketOptions={"MarketType": "spot",
                                   "SpotOptions": {"SpotInstanceType": "one-time",
                                                   "InstanceInterruptionBehavior": "terminate"}},
            UserData=base64.b64encode(USER_DATA.encode()).decode(),
            TagSpecifications=[{"ResourceType": "instance",
                                "Tags": [{"Key": "Project", "Value": "spot-swallow"},
                                         {"Key": "Purpose", "Value": "tready-measurement"}]}],
            **({"SubnetId": args.subnet} if args.subnet else {}),
        )
    except Exception as e:  # 스팟 용량 부족 등: 확보 실패 자체도 기록한다
        return {"run": run_no, "t_request": t_request, "error": type(e).__name__ + ": " + str(e)[:200]}

    iid = resp["Instances"][0]["InstanceId"]
    row = {"run": run_no, "instance_id": iid, "t_request": t_request}
    try:
        row["t_running"], row["az"] = wait_running(ec2, iid, args.running_timeout)
        marks = wait_marks(ec2, iid, args.ready_timeout)
        row.update({f"t_{k}": v for k, v in marks.items()})
        if "ready" not in marks:
            row["error"] = "ready mark not seen before timeout"
    except Exception as e:
        row["error"] = type(e).__name__ + ": " + str(e)[:200]
    finally:
        ec2.terminate_instances(InstanceIds=[iid])
    return row


def derive(row):
    """구간 길이(초)를 계산한다. 인스턴스 시계와 로컬 시계 차이는 NTP 수준(수십 ms)으로 본다."""
    d = {}
    tr = row.get("t_request")
    if tr and row.get("t_running"):
        d["provision_s"] = round((row["t_running"] - tr) / 1000, 1)
    if tr and row.get("t_boot"):
        d["boot_s"] = round((row["t_boot"] - tr) / 1000, 1)
    if row.get("t_jdk_installed") and row.get("t_boot"):
        d["install_s"] = round((row["t_jdk_installed"] - row["t_boot"]) / 1000, 1)
    if row.get("t_ready") and row.get("t_jdk_installed"):
        d["app_start_s"] = round((row["t_ready"] - row["t_jdk_installed"]) / 1000, 1)
    if tr and row.get("t_boot") and "app_start_s" in d:
        # 미리 구운 이미지라면 설치 구간이 없으므로 Tready = 부팅까지 + 앱 기동
        d["tready_baked_s"] = round(d["boot_s"] + d["app_start_s"], 1)
    if tr and row.get("t_ready"):
        d["tready_this_ami_s"] = round((row["t_ready"] - tr) / 1000, 1)
    return d


FIELDS = ["run", "started_at", "region", "instance_type", "az", "instance_id",
          "provision_s", "boot_s", "install_s", "app_start_s", "tready_baked_s", "tready_this_ami_s", "error"]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--region", default=os.environ.get("AWS_REGION", "ap-northeast-2"))
    p.add_argument("--instance-type", default="c6i.large")
    p.add_argument("--arch", default="x86_64", choices=["x86_64", "arm64"])
    p.add_argument("--subnet", help="기본 VPC 가 없으면 서브넷 ID 를 지정")
    p.add_argument("--runs", type=int, default=20)
    p.add_argument("--pause", type=int, default=30, help="측정 사이 대기(초)")
    p.add_argument("--running-timeout", type=int, default=600)
    p.add_argument("--ready-timeout", type=int, default=900)
    p.add_argument("--out", default="data/tready.csv")
    p.add_argument("--dry-run", action="store_true", help="권한만 확인하고 인스턴스는 만들지 않는다")
    a = p.parse_args()

    try:
        import boto3
        from botocore.exceptions import ClientError
    except ImportError:
        sys.exit("boto3 가 필요합니다: pip install boto3")

    ec2 = boto3.client("ec2", region_name=a.region)
    ssm = boto3.client("ssm", region_name=a.region)
    ami = latest_ami(ssm, a.arch)
    print(f"region={a.region} type={a.instance_type} ami={ami}")

    if a.dry_run:
        try:
            ec2.run_instances(ImageId=ami, InstanceType=a.instance_type, MinCount=1, MaxCount=1,
                              InstanceMarketOptions={"MarketType": "spot"}, DryRun=True)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            print("OK: 권한 확인됨" if code == "DryRunOperation" else f"실패: {code} {e}")
        return

    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    new_file = not os.path.exists(a.out)
    with open(a.out, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        for i in range(1, a.runs + 1):
            started = datetime.now(timezone.utc).isoformat(timespec="seconds")
            row = one_run(ec2, a, ami, i)
            out = {"run": i, "started_at": started, "region": a.region, "instance_type": a.instance_type,
                   "az": row.get("az"), "instance_id": row.get("instance_id"), "error": row.get("error"),
                   **derive(row)}
            w.writerow(out)
            f.flush()
            print(out, flush=True)
            if i < a.runs:
                time.sleep(a.pause)

    print(f"saved {a.out}. 요약: python3 tools/measure/summarize_tready.py {a.out}")


if __name__ == "__main__":
    main()
