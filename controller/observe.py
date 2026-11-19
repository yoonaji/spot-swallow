"""
제어기 입력 수집.

  - RabbitMQ 관리 API: 대기 작업 수 Q, 누적 발행 건수(→ 도착률 λ), 소비자 IP(→ 워커 준비 완료 판정)
  - PostgreSQL job_events: 완료된 작업의 처리 시간(→ 처리율 μ). DB 가 정산의 원장이므로 워커가 회수되어도 기록이 남는다.
  - 시장 상태 파일(injector.py 가 씀): 스팟 용량 유무, 회수 경고
"""
import base64
import json
import os
import subprocess
import urllib.request

PSQL = ["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "swallow", "-d", "swallow", "-At", "-F", ",", "-c"]


class RabbitObserver:
    def __init__(self, base="http://localhost:15672", user="swallow", password="swallow", queue="settlement.jobs"):
        self.base = base
        self.queue = queue
        self.auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    def _get(self, path):
        req = urllib.request.Request(self.base + path, headers={"Authorization": self.auth})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.load(r)

    def queue_stats(self):
        q = self._get(f"/api/queues/%2F/{self.queue}")
        stats = q.get("message_stats", {})
        return {"ready": q.get("messages_ready", 0), "unacked": q.get("messages_unacknowledged", 0),
                "published_total": stats.get("publish", 0)}

    def consumer_ips(self):
        ips = set()
        for c in self._get("/api/consumers"):
            if c.get("queue", {}).get("name") != self.queue:
                continue
            peer = c.get("channel_details", {}).get("peer_host")
            if peer:
                ips.add(peer)
        return ips


class DbObserver:
    """job_events 에서 새로 완료된 작업의 처리 시간을 가져온다."""

    def __init__(self, project_dir="."):
        self.dir = project_dir
        self.last_id = 0

    def new_processing_times(self):
        sql = ("SELECT event_id, detail FROM job_events WHERE event_type='COMPLETED' "
               f"AND event_id > {self.last_id} ORDER BY event_id")
        out = subprocess.run(PSQL + [sql], cwd=self.dir, capture_output=True, text=True).stdout
        times = []
        for line in out.strip().splitlines():
            eid, detail = line.split(",", 1)
            self.last_id = max(self.last_id, int(eid))
            if "processing_ms=" in detail:
                times.append(int(detail.split("processing_ms=")[1].split(";")[0]) / 1000)
        return times


class Market:
    """injector.py 와 주고받는 파일. 스팟 용량과 회수 경고."""

    def __init__(self, path="data/live/market.json"):
        self.path = path

    def read(self):
        try:
            with open(self.path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"available": 1, "notices": {}}

    def write(self, state):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, self.path)
