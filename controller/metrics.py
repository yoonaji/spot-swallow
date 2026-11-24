"""
제어기 상태를 Prometheus 텍스트 형식으로 내보낸다 (외부 라이브러리 없이).

제어기는 호스트에서 돌기 때문에 Prometheus 컨테이너는 host.docker.internal:9108 로 긁어 간다.
대시보드와 알림("준비된 워커 < Nbase")이 이 값을 쓴다.
"""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HELP = {
    "swallow_controller_target_workers": "제어기가 정한 목표 워커 수",
    "swallow_controller_alive_workers": "살아 있는 워커 수 (부팅 중 포함)",
    "swallow_controller_ready_workers": "큐를 소비하고 있는 워커 수",
    "swallow_controller_nbase": "평균 부하를 처리하는 데 필요한 최소 워커 수 ceil(lambda/mu)",
    "swallow_controller_lambda": "추정 도착률 (건/초)",
    "swallow_controller_mu": "추정 워커 한 대 처리율 (건/초)",
    "swallow_controller_tready_quantile_seconds": "복구 시간 Tready 의 p 분위수",
    "swallow_controller_concurrent_eviction_fraction": "동시 회수 비율 f_p",
    "swallow_controller_spot_available": "스팟 용량 있음 1 / 없음 0",
    "swallow_controller_evictions_total": "주입된 회수 누적 수",
}


class Metrics:
    def __init__(self, port=9108):
        self.port = port
        self.values = {}   # (name, labels tuple) -> value
        self.lock = threading.Lock()
        self.server = None

    def set(self, name, value, **labels):
        with self.lock:
            self.values[(name, tuple(sorted(labels.items())))] = float(value)

    def render(self):
        with self.lock:
            items = sorted(self.values.items())
        out, seen = [], set()
        for (name, labels), v in items:
            if name not in seen:
                seen.add(name)
                kind = "counter" if name.endswith("_total") else "gauge"
                out.append(f"# HELP {name} {HELP.get(name, name)}")
                out.append(f"# TYPE {name} {kind}")
            lab = ",".join(f'{k}="{val}"' for k, val in labels)
            out.append(f"{name}{{{lab}}} {v}" if lab else f"{name} {v}")
        return "\n".join(out) + "\n"

    def start(self):
        metrics = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = metrics.render().encode() if self.path.startswith("/metrics") else b""
                self.send_response(200 if body else 404)
                self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("0.0.0.0", self.port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        if self.server:
            self.server.shutdown()
