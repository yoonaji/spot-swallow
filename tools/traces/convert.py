#!/usr/bin/env python3
"""
공개 스팟 회수 기록을 실험 입력으로 변환한다.

원본: Can't Be Late (NSDI'24) 저자 공개 trace  https://github.com/skypilot-org/spot-traces
      각 파일은 일정 간격(gap_seconds)마다 "살아 있는(또는 확보 가능한) 인스턴스 수" 를 기록한다.

출력 (data/traces/ 아래, git 에는 올리지 않는다):
  <이름>.csv      t_s, available, evicted, eviction_fraction
                  evicted = 직전 tick 대비 감소한 인스턴스 수 (Parcae 와 같이 "가용 수의 감소분" 으로 센다)
  summary.json    trace 별 회수 통계와, 위험 구간 길이 W 별 동시 회수 비율 f_w 의 분위수

동시 회수 비율 f_w (중간보고서 4.2절 (4)):
  기록을 길이 W 인 구간으로 나누고, 구간 시작 시점의 가용 수 a0 중 구간 안에서 회수된 비율.
  a0 = 0 인 구간은 회수될 워커가 없으므로 제외한다.

같은 디렉터리(같은 기간)의 1-node trace 들은 합쳐서 하나의 풀(pool)로도 만든다.
여러 영역에 흩어진 단일 인스턴스를 한 풀로 볼 때의 동시 회수를 보기 위해서다.

사용:
  git clone --depth 1 https://github.com/skypilot-org/spot-traces data/raw/spot-traces
  python3 tools/traces/convert.py --src data/raw/spot-traces --out data/traces
  python3 tools/traces/convert.py ... --train-fraction 0.6   # 앞 60% 로 분위수를 추정하고 나머지를 평가용으로 표시
"""
import argparse
import csv
import json
import math
from pathlib import Path

WINDOWS_S = [150, 300, 600, 900, 1800]
QUANTILES = [0.5, 0.9, 0.95, 0.99, 1.0]


def quantile(sorted_xs, q):
    """선형 보간 분위수 (numpy 'linear' 와 같다)."""
    if not sorted_xs:
        return None
    pos = (len(sorted_xs) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (pos - lo)


def evictions(series):
    return [0] + [max(0, a - b) for a, b in zip(series, series[1:])]


def window_fractions(series, gap_s, window_s):
    """길이 window_s 구간별 동시 회수 비율 f_w. 구간 길이가 gap 보다 짧으면 한 tick 으로 본다."""
    ticks = max(1, round(window_s / gap_s))
    drops = evictions(series)
    out = []
    for start in range(0, len(series) - ticks, ticks):
        a0 = series[start]
        if a0 <= 0:
            continue
        lost = sum(drops[start + 1:start + ticks + 1])
        out.append(min(1.0, lost / a0))
    return out, ticks * gap_s


def summarize(name, series, gap_s, train_fraction):
    drops = evictions(series)
    split = int(len(series) * train_fraction) if train_fraction else len(series)
    s = {
        "name": name,
        "gap_seconds": gap_s,
        "ticks": len(series),
        "duration_hours": round(len(series) * gap_s / 3600, 1),
        "capacity": max(series),
        "mean_available": round(sum(series) / len(series), 2),
        "eviction_events": sum(1 for d in drops if d > 0),
        "evicted_instances": sum(drops),
        "multi_eviction_events": sum(1 for d in drops if d > 1),
        "windows": {},
    }
    if train_fraction:
        s["train_ticks"] = split
    for w in WINDOWS_S:
        train, effective = window_fractions(series[:split], gap_s, w)
        entry = {
            "effective_window_s": effective,
            "n_windows": len(train),
            "nonzero_windows": sum(1 for f in train if f > 0),
            "quantiles": {str(q): round(quantile(sorted(train), q), 4) if train else None for q in QUANTILES},
        }
        if train_fraction:
            test, _ = window_fractions(series[split:], gap_s, w)
            entry["test_quantiles"] = {str(q): round(quantile(sorted(test), q), 4) if test else None for q in QUANTILES}
        s["windows"][str(w)] = entry
    return s


def write_csv(path, series, gap_s):
    drops = evictions(series)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_s", "available", "evicted", "eviction_fraction"])
        prev = None
        for i, (a, d) in enumerate(zip(series, drops)):
            frac = round(d / prev, 4) if prev else 0.0
            w.writerow([i * gap_s, a, d, frac])
            prev = a


def load(path):
    d = json.loads(Path(path).read_text())
    return [int(x) for x in d["data"]], int(d["metadata"]["gap_seconds"])


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--src", default="data/raw/spot-traces")
    p.add_argument("--out", default="data/traces")
    p.add_argument("--train-fraction", type=float, default=0.0,
                   help="0 이면 전체를 사용. 예: 0.6 이면 앞 60%% 로 분위수를 추정하고 나머지를 test_quantiles 로 따로 계산")
    a = p.parse_args()

    src, out = Path(a.src), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    summaries = []

    for kind in ["preemption", "availability"]:
        for d in sorted((src / kind).glob("*/*")):
            files = sorted(d.glob("*.json"))
            if not files:
                continue
            group = f"{kind}-{d.parent.name}-{d.name}"
            loaded = [(f.stem, *load(f)) for f in files]
            for stem, series, gap in loaded:
                name = f"{group}-{stem}"
                write_csv(out / f"{name}.csv", series, gap)
                summaries.append(summarize(name, series, gap, a.train_fraction))
            # 같은 기간의 1-node trace 들은 하나의 풀로 합친다.
            if d.parent.name == "1-node" and len(loaded) > 1 and len({g for _, _, g in loaded}) == 1:
                gap = loaded[0][2]
                n = min(len(s) for _, s, _ in loaded)
                pool = [sum(s[i] for _, s, _ in loaded) for i in range(n)]
                name = f"{group}-pool{len(loaded)}"
                write_csv(out / f"{name}.csv", pool, gap)
                summaries.append(summarize(name, pool, gap, a.train_fraction))

    (out / "summary.json").write_text(json.dumps(summaries, indent=2, ensure_ascii=False))
    print(f"{'trace':58s} {'cap':>3s} {'hours':>6s} {'evict':>6s} {'multi':>5s}  f_w q95 (W=300s/600s/1800s)")
    for s in summaries:
        q = [s["windows"][w]["quantiles"]["0.95"] for w in ("300", "600", "1800")]
        print(f"{s['name']:58s} {s['capacity']:3d} {s['duration_hours']:6.1f} {s['evicted_instances']:6d} "
              f"{s['multi_eviction_events']:5d}  {q}")


if __name__ == "__main__":
    main()
