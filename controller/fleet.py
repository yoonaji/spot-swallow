"""
워커 집합(fleet) 관리. 로컬에서는 docker 컨테이너가 인스턴스 역할을 한다.

  spot 워커: 회수 주입기(injector.py)가 trace 에 따라 종료할 수 있다.
  od   워커: 온디맨드. 회수되지 않는다.

컨테이너에는 swallow.kind=spot|od 라벨을 단다. 상태는 아래 셋 중 하나다.
  booting  컨테이너는 떴지만 아직 RabbitMQ 소비자로 붙지 않음 (= Tready 가 끝나지 않음)
  ready    소비자로 붙어 작업을 받는 중
  dead     종료됨 (회수 또는 축소)
"""
import json
import subprocess
import time
from dataclasses import dataclass


@dataclass
class Worker:
    cid: str
    kind: str
    launched: float          # 생성 요청 시각 (epoch 초)
    ip: str = ""
    state: str = "booting"
    ready_at: float = None


def plan_changes(workers, target_spot, target_od, doomed=frozenset(), make_before_break=True):
    """
    목표 대수에 맞추기 위한 변경 계획 (순수 함수).
    회수 경고를 받은 워커(doomed)는 없는 셈 친다. 줄일 때는 부팅 중 → 최근에 뜬 순서로 내린다.

    make_before_break: 준비된(ready) 워커는 "준비된 워커 수가 목표 합계보다 많은 만큼"만 내린다.
      예) 스팟 4대 → (스팟 1, 온디맨드 3) 으로 바꿀 때, 온디맨드 3대가 준비되기 전에는 스팟 3대를 내리지 않는다.
      부팅 중인 워커는 아직 일을 하지 않으므로 언제든 내린다. 못 내린 워커는 다음 주기에 다시 계산한다.
      False 면 예전 동작(목표보다 많으면 바로 내림, 시뮬레이터 engine._reconcile 과 같은 규칙).
    반환: (띄울 spot 수, 띄울 od 수, 내릴 cid 목록)
    """
    launches = {}
    candidates = []
    for kind, target in (("spot", target_spot), ("od", target_od)):
        alive = [w for w in workers if w.kind == kind and w.state != "dead" and w.cid not in doomed]
        diff = target - len(alive)
        if diff > 0:
            launches[kind] = diff
        elif diff < 0:
            order = sorted(alive, key=lambda w: (w.state != "booting", -w.launched))
            candidates += order[:-diff]
    if not make_before_break:
        stops = [w.cid for w in candidates]
    else:
        ready = sum(1 for w in workers if w.state == "ready" and w.cid not in doomed)
        spare_ready = max(0, ready - (target_spot + target_od))
        stops = []
        for w in candidates:
            if w.state != "ready":
                stops.append(w.cid)
            elif spare_ready > 0:
                stops.append(w.cid)
                spare_ready -= 1
    return launches.get("spot", 0), launches.get("od", 0), stops


class DockerFleet:
    def __init__(self, project_dir=".", service="settlement-worker", env=None):
        self.dir = project_dir
        self.service = service
        self.env = env or {}
        self.workers = {}

    def _run(self, *args, check=True):
        return subprocess.run(list(args), cwd=self.dir, capture_output=True, text=True, check=check).stdout.strip()

    def launch(self, kind, boot_delay_s=0.0):
        env = {**self.env, "BOOT_DELAY_S": f"{boot_delay_s:.1f}"}
        cmd = ["docker", "compose", "run", "-d", "--no-deps", "--label", f"swallow.kind={kind}"]
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]
        t = time.time()
        name = self._run(*cmd, self.service).splitlines()[-1]
        cid = self._run("docker", "inspect", "-f", "{{.Id}}", name)[:12]
        self.workers[cid] = Worker(cid, kind, t)
        return cid

    def stop(self, cid, grace_s=10):
        # SIGTERM: Spring 이 처리 중인 작업을 마치고 Ack 한 뒤 종료한다 (graceful)
        subprocess.Popen(["docker", "stop", "-t", str(grace_s), cid], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        if cid in self.workers:
            self.workers[cid].state = "dead"

    def notify(self, cid, path="/tmp/spot-notice"):
        """회수 경고(2분 전 중단 알림)를 워커에 전달한다. 워커(SpotNoticeWatcher)는 새 작업을 멈추고 처리 중인 것만 끝낸다."""
        subprocess.Popen(["docker", "exec", cid, "touch", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def kill(self, cid):
        """회수: 경고 없이 즉시 종료 (kill -9)."""
        self._run("docker", "kill", "-s", "KILL", cid, check=False)
        if cid in self.workers:
            self.workers[cid].state = "dead"

    def refresh(self, consumer_ips):
        """docker 상태와 RabbitMQ 소비자 목록으로 각 워커의 상태를 갱신한다. 새로 ready 가 된 워커를 돌려준다."""
        out = self._run("docker", "ps", "-a", "--filter", "label=swallow.kind", "--format", "{{json .}}")
        seen = {}
        for line in out.splitlines():
            d = json.loads(line)
            seen[d["ID"][:12]] = d
        newly_ready = []
        for cid, w in list(self.workers.items()):
            d = seen.get(cid)
            if w.state == "dead":
                continue
            if d is None or d["State"] != "running":
                w.state = "dead"
                continue
            if not w.ip:
                w.ip = self._run("docker", "inspect", "-f",
                                 "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", cid, check=False)
            if w.state == "booting" and w.ip and w.ip in consumer_ips:
                w.state, w.ready_at = "ready", time.time()
                newly_ready.append(w)
        return newly_ready

    def alive(self, kind=None):
        return [w for w in list(self.workers.values()) if w.state != "dead" and (kind is None or w.kind == kind)]

    def cleanup(self):
        ids = self._run("docker", "ps", "-aq", "--filter", "label=swallow.kind", check=False).split()
        if ids:
            self._run("docker", "rm", "-f", *ids, check=False)
