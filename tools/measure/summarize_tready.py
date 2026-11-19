#!/usr/bin/env python3
"""ec2_tready.py 결과 요약: 조건(variant)별 구간 평균과 분위수. 제어기의 위험 구간 Trisk = q_p(Tready) + Δ 에 쓴다.

조건이 둘 이상이면(예: baseline 과 dyn-secret) 첫 조건 대비 tready_baked_s 증가분(보안 오버헤드)도 출력한다.
"""
import csv
import math
import statistics
import sys

COLS = ("provision_s", "boot_s", "install_s", "bootstrap_s", "app_start_s", "tready_baked_s", "tready_this_ami_s")


def quantile(xs, q):
    xs = sorted(xs)
    pos = (len(xs) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def col(rows, name):
    return [float(r[name]) for r in rows if r.get(name)]


def main(paths):
    rows = [r for p in paths for r in csv.DictReader(open(p))]
    variants = list(dict.fromkeys(r.get("variant") or "baseline" for r in rows))
    for v in variants:
        vr = [r for r in rows if (r.get("variant") or "baseline") == v]
        failed = [r for r in vr if r.get("error")]
        print(f"[{v}] runs {len(vr)}, failed {len(failed)}")
        for c in COLS:
            xs = col(vr, c)
            if xs:
                print(f"  {c:18s} n={len(xs):3d} mean={statistics.fmean(xs):6.1f}s p50={quantile(xs, .5):6.1f} "
                      f"p90={quantile(xs, .9):6.1f} p95={quantile(xs, .95):6.1f} max={max(xs):6.1f}")
        for r in failed:
            print("  failed:", r["run"], r["error"])
    if len(variants) > 1:
        base = col([r for r in rows if (r.get("variant") or "baseline") == variants[0]], "tready_baked_s")
        for v in variants[1:]:
            xs = col([r for r in rows if (r.get("variant") or "baseline") == v], "tready_baked_s")
            if base and xs:
                for q in (.5, .95):
                    b, x = quantile(base, q), quantile(xs, q)
                    print(f"overhead {v} vs {variants[0]} p{int(q*100)}: +{x - b:.1f}s ({(x - b) / b * 100:+.0f}%)")


if __name__ == "__main__":
    main(sys.argv[1:] or ["data/tready.csv"])
