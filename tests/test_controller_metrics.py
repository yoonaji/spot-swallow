import urllib.request

from controller.metrics import Metrics


def test_render_prometheus_text():
    m = Metrics()
    m.set("swallow_controller_target_workers", 5, kind="spot")
    m.set("swallow_controller_target_workers", 1, kind="od")
    m.set("swallow_controller_evictions_total", 3)
    text = m.render()
    assert "# TYPE swallow_controller_target_workers gauge" in text
    assert 'swallow_controller_target_workers{kind="spot"} 5.0' in text
    assert "# TYPE swallow_controller_evictions_total counter" in text
    assert text.count("# TYPE swallow_controller_target_workers") == 1


def test_http_endpoint():
    m = Metrics(port=0).start()
    try:
        m.set("swallow_controller_ready_workers", 4)
        port = m.server.server_address[1]
        body = urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5).read().decode()
        assert "swallow_controller_ready_workers 4.0" in body
    finally:
        m.stop()
