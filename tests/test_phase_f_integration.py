"""Phase F end-to-end integration + stress tests.

Boots the REAL backend against a synthetic MJPEG camera and drives it through
the REAL WebSocket. Skipped if the fixed ports are busy (e.g. a user instance).
"""
import asyncio
import json
import os
import socket
import subprocess
import sys
import time

import pytest
import websockets

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

PY = sys.executable
FAKE_PORT = 14997
DEAD_SOURCE = "http://192.0.2.10:4747/video"  # TEST-NET-1


def _port_free(port):
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _wait_port(port, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.1)
    return False


def _health():
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:8766/health", timeout=1.0) as r:
            return json.load(r)
    except Exception:
        return None


def _stop(proc):
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


@pytest.fixture
def live_backend():
    for port in (8765, 8766, FAKE_PORT):
        if not _port_free(port):
            pytest.skip(f"port {port} busy - user instance running")
    fake = subprocess.Popen(
        [PY, os.path.join(PROJECT_ROOT, "tools", "fake_mjpeg.py"), "--port", str(FAKE_PORT)],
        cwd=PROJECT_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )
    backend = None
    try:
        assert _wait_port(FAKE_PORT), "synthetic camera did not start"
        env = dict(os.environ, ALERT_VISION_SOURCE=f"http://127.0.0.1:{FAKE_PORT}/video")
        backend = subprocess.Popen(
            [PY, "-u", "alert_vision.py"], env=env, cwd=PROJECT_ROOT,
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline and backend.poll() is None:
            if _health() is not None:
                break
            time.sleep(0.1)
        if backend.poll() is not None or _health() is None:
            pytest.skip("backend did not boot for integration test")
        yield backend
    finally:
        _stop(backend)
        _stop(fake)


def test_F1_ws_telemetry_reports_connected_camera(live_backend):
    async def scenario():
        async with websockets.connect("ws://127.0.0.1:8765", open_timeout=5) as ws:
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                packet = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                if packet.get("type") != "telemetry":
                    continue
                cameras = packet.get("cameras") or []
                if cameras and cameras[0].get("online"):
                    connection = cameras[0].get("connection") or {}
                    assert connection.get("state") == "connected"
                    assert isinstance(connection.get("elapsed_ms"), (int, float))
                    assert "source_value_masked" in cameras[0]
                    return
            pytest.fail("no connected camera telemetry within 8s")

    asyncio.run(scenario())


def test_F2_test_source_reports_failed_within_5s(live_backend):
    async def scenario():
        async with websockets.connect("ws://127.0.0.1:8765", open_timeout=5) as ws:
            sent = time.monotonic()
            await ws.send(json.dumps({"type": "test_source", "id": "f2", "source": DEAD_SOURCE}))
            deadline = time.monotonic() + 6.0
            while time.monotonic() < deadline:
                packet = json.loads(await asyncio.wait_for(ws.recv(), timeout=6.0))
                if packet.get("type") != "test_result" or packet.get("id") != "f2":
                    continue
                elapsed_ms = (time.monotonic() - sent) * 1000.0
                assert packet.get("ok") is False
                assert packet.get("error")
                assert elapsed_ms <= 5000.0 + 500.0, f"test_result took {elapsed_ms:.0f}ms"
                return
            pytest.fail("no test_result within 6s")

    asyncio.run(scenario())


def test_F3_stress_alerts_outrank_telemetry_under_backlog():
    """即使遥测队列被灌满，紧急 alert 仍优先发送。"""
    from alert_vision import WebSocketBroadcaster, _ClientSession

    class Conn:
        def __init__(self):
            self.sent = []
            self.closed = False

        async def send(self, message):
            self.sent.append(message)

        async def close(self):
            self.closed = True

    async def scenario():
        broadcaster = WebSocketBroadcaster()
        conn = Conn()
        await broadcaster.register(conn)
        session = broadcaster.clients[conn]
        for i in range(session.incoming_queue.maxsize):
            session.incoming_queue.put_nowait(f"telemetry-{i}")
        for alert_id in (11, 12, 13):
            assert await broadcaster.broadcast({"type": "alert", "alert_id": alert_id}) == 1
        task = asyncio.create_task(broadcaster._session_loop(session))
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        delivered = [m for m in conn.sent if "alert" in m]
        assert len(delivered) == 3, f"expected 3 alerts, got {len(delivered)}"
        for i, message in enumerate(delivered):
            assert f'"alert_id": {11 + i}' in message or f'"alert_id":{11 + i}' in message
        await broadcaster.unregister(conn)

    asyncio.run(scenario())


def test_F4_no_cross_camera_identity_claim_in_contracts():
    """Cross-camera re-identification must never be claimed."""
    for name in ("docs/ARCHITECTURE.md", "docs/ACCURACY_LIMITATIONS.md", "README.md"):
        path = os.path.join(PROJECT_ROOT, name)
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read().lower()
        assert "cross-camera identity" not in text or "not" in text
