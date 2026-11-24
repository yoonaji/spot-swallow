"""
회수 주입기: 공개 trace 를 실제 시간에 맞춰 재생하며 스팟 워커 컨테이너를 종료한다 (클라우드 역할).

  - trace 의 tick 마다 회수 비율 f = 회수 수 / 직전 가용 수 를 지금 살아 있는 spot 워커 수에 곱해(확률적 반올림)
    그만큼을 무작위로 골라, tick 안의 균등한 시각에 docker kill 한다. 시뮬레이터(sim/engine.py)와 같은 규칙이다.
  - 종료 notice_s 초 전에 회수 경고를 시장 파일에 쓴다 (AWS 의 2분 전 중단 알림에 해당).
  - trace 의 가용 수가 0 이면 시장 파일에 available=0 을 써서 제어기가 스팟을 새로 만들지 못하게 한다.
  - speedup: trace 시간을 몇 배 빠르게 재생할지. 1 이면 실제 시간 그대로, 5 면 5분 tick 이 1분마다 온다.
"""
import random
import threading
import time

from controller.observe import Market
from sim import traces


class Injector(threading.Thread):
    def __init__(self, fleet, trace, trace_start_s, speedup=1.0, notice_s=120.0, seed=0, market=None, log=print):
        super().__init__(daemon=True)
        self.fleet = fleet
        rows, self.gap = traces.load(trace)
        self.rows = [r for r in rows if r[0] >= trace_start_s]
        self.trace_start = trace_start_s
        self.speedup = speedup
        self.notice_s = notice_s
        self.rng = random.Random(seed)
        self.market = market or Market()
        self.log = log
        self.stop_flag = threading.Event()
        self.t0 = None
        self.kills = 0
        self.notices = {}

    def trace_time(self, now=None):
        return self.trace_start + ((now or time.time()) - self.t0) * self.speedup

    def _publish(self, available):
        self.market.write({"available": available, "notices": self.notices,
                           "trace_time": self.trace_time()})

    def run(self):
        self.t0 = time.time()
        prev = None
        pending = []   # (kill_at, notice_at, cid)
        for t, avail, evicted in self.rows:
            due = self.t0 + (t - self.trace_start) / self.speedup
            while time.time() < due and not self.stop_flag.is_set():
                self._fire(pending)
                time.sleep(0.2)
            if self.stop_flag.is_set():
                return
            self._publish(avail)
            if prev and evicted > 0:
                spots = [w for w in self.fleet.alive("spot")]
                x = min(1.0, evicted / prev) * len(spots)
                k = int(x) + (self.rng.random() < x - int(x))
                tick = self.gap / self.speedup
                for w in self.rng.sample(spots, min(k, len(spots))):
                    kill_at = time.time() + self.rng.random() * tick
                    notice_at = max(time.time(), kill_at - self.notice_s / self.speedup)
                    pending.append((kill_at, notice_at, w.cid))
            prev = avail

    def _fire(self, pending):
        now = time.time()
        changed = False
        for item in list(pending):
            kill_at, notice_at, cid = item
            if notice_at <= now and cid not in self.notices:
                self.notices[cid] = kill_at
                self.fleet.notify(cid)   # 워커에게도 알린다 (새 작업 중단, 처리 중인 것만 마무리)
                changed = True
            if kill_at <= now:
                self.fleet.kill(cid)
                self.kills += 1
                self.log(f"[evict] {cid} (total {self.kills})")
                pending.remove(item)
                self.notices.pop(cid, None)
                changed = True
        if changed:
            st = self.market.read()
            st["notices"] = self.notices
            self.market.write(st)
