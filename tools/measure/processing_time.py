#!/usr/bin/env python3
"""
작업 처리 시간 분포 측정.

워커가 결과를 확정할 때 job_events.detail 에 남기는 processing_ms (메시지 수신부터 커밋까지)를 모아
평균, 변동계수(CV), 분위수, 워커 한 대의 처리율 μ 를 계산한다.
μ 는 제어기의 기본 필요 대수(식 4)에, CV 는 근사식(7) 오차 분석에 쓰인다.

먼저 작업을 처리해 둔다:
  scripts/reset.sh 3 && python3 scripts/e2e.py --merchants 100 --days 3
그다음:
  python3 tools/measure/processing_time.py            # docker compose 의 postgres 에서 읽는다
  python3 tools/measure/processing_time.py --csv data/processing_ms.csv
"""
import argparse
import csv
import math
import re
import statistics
import subprocess

QUERY = "SELECT detail FROM job_events WHERE event_type = 'COMPLETED' AND detail LIKE '%processing_ms=%'"


def quantile(xs, q):
    xs = sorted(xs)
    pos = (len(xs) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--csv", help="작업별 (tx, processing_ms) 를 CSV 로 저장")
    a = p.parse_args()

    out = subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "swallow", "-d", "swallow", "-At", "-c", QUERY],
        capture_output=True, text=True, check=True).stdout
    rows = []
    for line in out.splitlines():
        tx = re.search(r"tx=(\d+)", line)
        ms = re.search(r"processing_ms=(\d+)", line)
        if tx and ms:
            rows.append((int(tx.group(1)), int(ms.group(1))))
    if not rows:
        raise SystemExit("완료된 작업이 없습니다. 먼저 e2e.py 로 작업을 처리하세요.")

    ms = [m for _, m in rows]
    mean = statistics.fmean(ms)
    sd = statistics.pstdev(ms)
    print(f"jobs                 {len(ms)}")
    print(f"mean processing      {mean:.1f} ms")
    print(f"stdev / CV           {sd:.1f} ms / {sd / mean:.3f}")
    for q in (0.5, 0.9, 0.95, 0.99):
        print(f"p{int(q * 100):<19d} {quantile(ms, q):.0f} ms")
    print(f"max                  {max(ms)} ms")
    print(f"mu (jobs/s/worker)   {1000 / mean:.3f}")
    txs = [t for t, _ in rows]
    if len(set(txs)) > 1:
        # 처리 시간이 거래 수에 얼마나 비례하는지 (최소제곱 기울기)
        mt = statistics.fmean(txs)
        slope = sum((t - mt) * (m - mean) for t, m in rows) / sum((t - mt) ** 2 for t in txs)
        print(f"ms per tx (slope)    {slope:.3f}  intercept {mean - slope * mt:.1f} ms")

    if a.csv:
        with open(a.csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["tx_count", "processing_ms"])
            w.writerows(rows)
        print(f"saved {a.csv}")


if __name__ == "__main__":
    main()
