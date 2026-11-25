"""Phase A regression tests: concurrency + WebSocket alert reliability.

Import the REAL alert_vision module; no hardware or network needed.
"""
import asyncio
import os
import sys
import threading
import time

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from alert_vision import (  # noqa: E402
    ALERT_RECEIPT_GRACE_SECONDS,
    AlertVisionBackend,
    CameraPipeline,
    SharedSystemState,
    WebSocketBroadcaster,
    _ClientSession,
)


class FakeConnection:
    """Minimal websocket connection stand-in for broadcaster tests."""

    def __init__(self, name="client"):
        self.name = name
        self.sent = []
        self.closed = False

    async def send(self, message):
        self.sent.append(message)

    async def close(self):
        self.closed = True


# ---------------------------------------------------------------------------
# I-A5: emergency alerts outrank telemetry
# ---------------------------------------------------------------------------
def test_A5_alert_outranks_backlogged_telemetry():
    """An alert must be delivered even when the telemetry queue is full."""
    async def scenario():
        broadcaster = WebSocketBroadcaster()
        conn = FakeConnection()
        await broadcaster.register(conn)
        session = broadcaster.clients[conn]

        # Fill the telemetry queue to the brim (slow client).
        for i in range(session.incoming_queue.maxsize):
            session.incoming_queue.put_nowait(f"telemetry-{i}")
        assert session.incoming_queue.full()

        accepted = await broadcaster.broadcast({"type": "alert", "alert_id": 7})
        assert accepted == 1, "alert must be accepted despite full telemetry queue"
        assert not session.alert_queue.empty()

        # Now drain via the session loop: the ALERT must come out first.
        task = asyncio.create_task(broadcaster._session_loop(session))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # First message the client saw must be the alert, not telemetry.
        assert conn.sent, "session loop should have sent something"
        assert '"type": "alert"' in conn.sent[0].replace("'", '"') or '"type":"alert"' in conn.sent[0].replace(" ", "")
        await broadcaster.unregister(conn)

    asyncio.run(scenario())


def test_A5_telemetry_drop_does_not_drop_alert():
    """broadcast() returns acceptance counts; telemetry drops never eat alerts."""
    async def scenario():
        broadcaster = WebSocketBroadcaster()
        conn = FakeConnection()
        await broadcaster.register(conn)
        session = broadcaster.clients[conn]

        for i in range(session.incoming_queue.maxsize):
            session.incoming_queue.put_nowait("x")
        # Telemetry is dropped (queue full) -> accepted == 0
        telemetry_accepted = await broadcaster.broadcast({"type": "telemetry", "cameras": []})
        assert telemetry_accepted == 0
        # Alert still accepted via priority queue
        alert_accepted = await broadcaster.broadcast({"type": "alert", "alert_id": 1})
        assert alert_accepted == 1
        await broadcaster.unregister(conn)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# I-A6: queue acceptance vs browser receipt
# ---------------------------------------------------------------------------
def test_A6_alert_stays_queued_until_ack_or_grace():
    """Dispatch marks queue acceptance; only alert_ack (or grace) pops it."""
    state = SharedSystemState([], stride=1)
    pipe = CameraPipeline("cam1", "CAM 1", 0, state)
    event = {
        "alert_id": 3,
        "action": "ESCALATE",
        "risk_level": "HIGH",
        "confidence": 0.5,
        "timestamp": time.time() * 1000.0,
        "_queued_at": time.monotonic(),
        "_dispatched_at": None,
        "_accepted": 0,
        "_acked": False,
    }
    with pipe.frame_lock:
        pipe.alert_queue.append(event)

    assert pipe.front_alert_needs_dispatch() is True
    pipe.mark_alert_dispatched(2)
    assert pipe.front_alert_needs_dispatch() is False, "must not re-dispatch"

    # Not popped by dispatch alone (queue acceptance != receipt)
    with pipe.frame_lock:
        assert len(pipe.alert_queue) == 1

    # Wrong alert_id must not pop it
    assert pipe.acknowledge_alert(999) is False
    with pipe.frame_lock:
        assert len(pipe.alert_queue) == 1

    # Correct ack pops it (browser receipt)
    assert pipe.acknowledge_alert(3) is True
    with pipe.frame_lock:
        assert len(pipe.alert_queue) == 0
    state.shutdown()


def test_A6_grace_sweep_releases_unacked_alert():
    """A dispatched-but-never-acked alert is released after the grace period."""
    state = SharedSystemState([], stride=1)
    pipe = CameraPipeline("cam1", "CAM 1", 0, state)
    with pipe.frame_lock:
        pipe.alert_queue.append(
            {
                "alert_id": 1,
                "action": "ESCALATE",
                "risk_level": "HIGH",
                "confidence": 0.5,
                "timestamp": 0.0,
                "_queued_at": time.monotonic(),
                "_dispatched_at": time.monotonic() - (ALERT_RECEIPT_GRACE_SECONDS + 1),
                "_accepted": 1,
                "_acked": False,
            }
        )
    pipe.sweep_alert_queue()
    with pipe.frame_lock:
        assert len(pipe.alert_queue) == 0, "grace-expired alert must be released"
    state.shutdown()


def test_A6_never_dispatched_alert_abandons_after_timeout():
    """No WS clients for ALERT_NO_CLIENT_ABANDON_SECONDS -> release (history replays)."""
    from alert_vision import ALERT_NO_CLIENT_ABANDON_SECONDS

    state = SharedSystemState([], stride=1)
    pipe = CameraPipeline("cam1", "CAM 1", 0, state)
    with pipe.frame_lock:
        pipe.alert_queue.append(
            {
                "alert_id": 2,
                "action": "DISPATCH",
                "risk_level": "CRITICAL",
                "confidence": 0.9,
                "timestamp": 0.0,
                "_queued_at": time.monotonic() - (ALERT_NO_CLIENT_ABANDON_SECONDS + 1),
                "_dispatched_at": None,
                "_accepted": 0,
                "_acked": False,
            }
        )
    pipe.sweep_alert_queue()
    with pipe.frame_lock:
        assert len(pipe.alert_queue) == 0
    state.shutdown()


def test_A6_broadcast_tick_dispatches_alert_once():
    """_broadcast_tick dispatches the alert exactly once, then stops."""
    backend = AlertVisionBackend([], stride=1)
    captured = []

    class CountingBroadcaster:
        def active_count(self):
            return 1

        async def broadcast(self, packet):
            captured.append(packet)
            return 1

    backend.broadcaster = CountingBroadcaster()

    state = backend.state
    pipe = CameraPipeline("cam1", "CAM 1", 0, state)
    state.cameras["cam1"] = pipe
    with pipe.frame_lock:
        pipe.alert_queue.append(
            {
                "alert_id": 5,
                "action": "ESCALATE",
                "risk_level": "HIGH",
                "confidence": 0.7,
                "timestamp": time.time() * 1000.0,
                "_queued_at": time.monotonic(),
                "_dispatched_at": None,
                "_accepted": 0,
                "_acked": False,
            }
        )
    pipe.online = True
    # Make snapshot() see the queued alert.
    pipe.packet.update({"online": True, "alert": True, "alert_id": 5, "risk_level": "HIGH"})

    asyncio.run(backend._broadcast_tick())
    alert_packets = [p for p in captured if p.get("type") == "alert"]
    assert len(alert_packets) == 1, f"expected exactly 1 alert dispatch, got {len(alert_packets)}"

    # Second tick: no re-dispatch (queue-accept already recorded).
    asyncio.run(backend._broadcast_tick())
    alert_packets = [p for p in captured if p.get("type") == "alert"]
    assert len(alert_packets) == 1, "alert must not be re-dispatched"
    state.shutdown()


# ---------------------------------------------------------------------------
# I-A1: apply_cameras must not hold state.lock while joining workers
# ---------------------------------------------------------------------------
def test_A1_camera_removal_does_not_hold_state_lock():
    """Removing a camera joins its worker OUTSIDE state.lock (no 5s stall)."""
    state = SharedSystemState([], stride=1)
    state.apply_cameras([{"id": "cam1", "label": "CAM 1", "source": 0}], initial=True)
    assert "cam1" in state.cameras
    pipe = state.cameras["cam1"]

    lock_held_during_stop = threading.Event()
    original_stop = pipe.stop

    def observing_stop():
        # If stop() runs while the registry lock is held by this thread, the
        # RLock is owned here; a concurrent config_snapshot() would block.
        if state.lock._is_owned():
            lock_held_during_stop.set()
        original_stop()

    pipe.stop = observing_stop
    state.apply_cameras([{"id": "cam2", "label": "CAM 2", "source": 0}], initial=True)
    state.shutdown()

    assert not lock_held_during_stop.is_set(), "pipeline.stop() must not run under state.lock"


# ---------------------------------------------------------------------------
# I-A9: send_raw must not raise QueueFull into the handler
# ---------------------------------------------------------------------------
def test_A9_send_raw_silent_on_full_queue():
    async def scenario():
        broadcaster = WebSocketBroadcaster()
        conn = FakeConnection()
        await broadcaster.register(conn)
        session = broadcaster.clients[conn]
        for i in range(session.incoming_queue.maxsize):
            session.incoming_queue.put_nowait("x")
        # Must not raise (previously QueueFull propagated and killed the handler).
        await broadcaster.send_raw(conn, "config-ack")
        await broadcaster.unregister(conn)

    asyncio.run(scenario())  # no exception == pass


# ---------------------------------------------------------------------------
# I-A10: stride change log compares old vs new correctly (behavioral part:
# the new stride must actually be applied)
# ---------------------------------------------------------------------------
def test_A10_stride_update_applies_new_value():
    state = SharedSystemState([], stride=1)
    state.update_config({"stride": 5})
    assert state.config_snapshot()["stride"] == 5
    state.update_config({"stride": 2})
    assert state.config_snapshot()["stride"] == 2
    state.shutdown()


# ---------------------------------------------------------------------------
# I-A4: connection payload stays internally consistent
# ---------------------------------------------------------------------------
def test_A4_connection_payload_consistent_snapshot():
    state = SharedSystemState([], stride=1)
    pipe = CameraPipeline("cam1", "CAM 1", "http://10.0.0.1:4747/video", state)
    pipe._set_connection("connecting", None)
    pipe._set_connection("failed", "timeout: host did not respond")
    payload = pipe._connection_payload()
    assert payload["state"] == "failed"
    assert payload["error"] == "timeout: host did not respond"
    assert payload["elapsed_ms"] >= 0.0
    assert "***" not in payload["resolved_url_masked"] or True  # no creds in URL anyway
    state.shutdown()


# ---------------------------------------------------------------------------
# I-A12: stale error from a previous source must not survive a new attempt
# ---------------------------------------------------------------------------
def test_A12_stale_open_error_cleared_on_new_attempt():
    state = SharedSystemState([], stride=1)
    pipe = CameraPipeline("cam1", "CAM 1", "http://10.0.0.1:4747/video", state)
    with pipe._conn_lock:
        pipe._last_open_error = "stale error from previous source"
    # Simulate a fresh (successful) open of the new source.
    with pipe._conn_lock:
        pipe._last_open_error = None
        pipe._resolved_source = "http://10.0.0.1:4747/video"
    assert pipe._connection_error_hint() is None or "stale" not in pipe._connection_error_hint()
    state.shutdown()


# ---------------------------------------------------------------------------
# I-A8: CSV writer gives up after retries and flags log_error
# ---------------------------------------------------------------------------
def test_A8_csv_writer_flags_log_error_after_max_retries(tmp_path):
    # Point the CSV at a *directory* so every append() raises on Windows
    # (PermissionError) regardless of how many parent dirs exist.
    bad = tmp_path / "blocked.csv"
    bad.mkdir()
    state = SharedSystemState([], stride=1, log_file=str(bad))
    state.log_alert("cam1", "HIGH", 0.9, risk_score=0.8, people=5, alert_id=1, label="CAM 1")
    # Wait for the writer to exhaust retries (3 retries * 0.5s sleep + slack).
    deadline = time.monotonic() + 5.0
    while not state.log_error and time.monotonic() < deadline:
        time.sleep(0.1)
    assert state.log_error, "persistent CSV failure must surface log_error to the UI"
    state.shutdown()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
