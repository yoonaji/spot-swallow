#!/usr/bin/env python3
"""
일정한 도착률로 정산 작업을 넣는 부하 발생기 (포아송 도착). 제어기 실험에서 e2e.py 대신 쓴다.

  python3 scripts/load.py --rate 1.8 --duration 1800        # 거래 적재 후 30분간 초당 1.8건
  python3 scripts/load.py --surge 600:900:2.0 ...            # 600~900초 구간은 2배

끝나면 대사 결과를 확인해 유실·중복·금액 불일치를 출력한다.
"""
import argparse
import json
import math
import os
import random
import sys
import time
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(__file__))
from e2e import call, wait_for_api  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://localhost:8080")
    p.add_argument("--rate", type=float, default=1.8)
    p.add_argument("--duration", type=float, default=1800)
    p.add_argument("--surge", action="append", default=[], help="시작:끝:배수 (초)")
    p.add_argument("--merchants", type=int, default=200)
    p.add_argument("--tx", type=int, default=200)
    p.add_argument("--start", default="2026-01-01")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--drain-timeout", type=int, default=900)
    a = p.parse_args()

    rng = random.Random(a.seed)
    surges = [tuple(float(x) for x in s.split(":")) for s in a.surge]
    expected = a.rate * a.duration * max([1.0] + [m for _, _, m in surges])
    days = math.ceil(expected * 1.2 / a.merchants)
    wait_for_api(a.base)
    _, seeded = call(a.base, "POST", f"/admin/seed?merchants={a.merchants}&days={days}"
                                     f"&txPerMerchantDay={a.tx}&startDate={a.start}")
    print("seeded:", seeded, flush=True)

    start = date.fromisoformat(a.start)
    jobs = [(m, d) for d in range(days) for m in range(1, a.merchants + 1)]
    t0 = time.time()
    t = 0.0
    sent = 0
    while True:
        rate = a.rate * next((m for s, e, m in surges if s <= t < e), 1.0)
        t += rng.expovariate(rate)
        if t >= a.duration or sent >= len(jobs):
            break
        delay = t0 + t - time.time()
        if delay > 0:
            time.sleep(delay)
        m, d = jobs[sent]
        call(a.base, "POST", "/settlement-jobs",
             {"merchantId": f"M{m:04d}", "settlementDate": (start + timedelta(days=d)).isoformat()})
        sent += 1
        if sent % 500 == 0:
            print(f"  {time.time() - t0:6.0f}s sent {sent}", flush=True)
    print(f"sent {sent} jobs in {time.time() - t0:.0f}s; waiting for drain", flush=True)

    deadline = time.time() + a.drain_timeout
    while time.time() < deadline:
        _, rep = call(a.base, "GET", "/reconciliation")
        if rep.get("allSettled"):
            break
        time.sleep(5)
    print(json.dumps(rep, indent=2))
    ok = rep.get("consistent") and rep.get("allSettled")
    print("RESULT:", "PASS" if ok else "FAIL")


if __name__ == "__main__":
    main()
