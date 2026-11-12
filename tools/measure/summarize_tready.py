#!/usr/bin/env python3
"""ec2_tready.py 결과 요약: 구간별 평균과 분위수. 제어기의 위험 구간 Trisk = q_p(Tready) + Δ 에 쓴다."""
import csv
import math
import statistics
import sys


def quantile(xs, q):
    xs = sorted(xs)
    pos = (len(xs) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def main(path):
    rows = list(csv.DictReader(open(path)))
    failed = [r for r in rows if r.get("error")]
    print(f"runs {len(rows)}, failed {len(failed)}")
    for col in ("provision_s", "boot_s", "install_s", "app_start_s", "tready_baked_s", "tready_this_ami_s"):
        xs = [float(r[col]) for r in rows if r.get(col)]
        if not xs:
            continue
        print(f"{col:18s} n={len(xs):3d} mean={statistics.fmean(xs):6.1f}s "
              f"p50={quantile(xs, .5):6.1f} p90={quantile(xs, .9):6.1f} p95={quantile(xs, .95):6.1f} max={max(xs):6.1f}")
    for r in failed:
        print("  failed:", r["run"], r["error"])


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/tready.csv")
