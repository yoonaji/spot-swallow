"""convert.py 가 만든 data/traces/<이름>.csv 를 시뮬레이터 입력으로 읽는다."""
import csv
import json
from functools import lru_cache
from pathlib import Path

TRACE_DIR = Path(__file__).resolve().parent.parent / "data" / "traces"


@lru_cache(maxsize=None)
def load(name, trace_dir=str(TRACE_DIR)):
    """[(t_s, available, evicted)], gap_s."""
    rows = []
    with open(Path(trace_dir) / f"{name}.csv") as f:
        for r in csv.DictReader(f):
            rows.append((int(float(r["t_s"])), int(r["available"]), int(r["evicted"])))
    gap = rows[1][0] - rows[0][0] if len(rows) > 1 else 300
    return tuple(rows), gap


def segment(name, start_s, duration_s, trace_dir=str(TRACE_DIR)):
    """
    시뮬레이션 시각 0 을 trace 의 start_s 에 맞춘다.
    history: 0 이전의 기록 (제어기가 f_w 를 추정할 때 쓰는 과거, 시각은 음수)
    future : 시뮬레이션 구간의 기록 (회수 일정과 스팟 용량 유무를 만든다)
    """
    rows, gap = load(name, trace_dir)
    shifted = [(t - start_s, a, e) for t, a, e in rows]
    history = [x for x in shifted if x[0] <= 0]
    future = [x for x in shifted if 0 < x[0] <= duration_s + gap]
    return history, future, gap


def duration(name, trace_dir=str(TRACE_DIR)):
    rows, gap = load(name, trace_dir)
    return rows[-1][0] + gap


def summary(trace_dir=str(TRACE_DIR)):
    return {s["name"]: s for s in json.load(open(Path(trace_dir) / "summary.json"))}
