"""Phase F unit coverage for gaps listed in I-F1."""
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from alert_vision import (  # noqa: E402
    DEESCALATE_HOLD_SECONDS,
    ESCALATE_HOLD_SECONDS,
    CameraPipeline,
    SharedSystemState,
    _validate_source,
)


def test_F5_validate_source_accepts_supported_and_rejects_dangerous():
    assert _validate_source(0) == 0
    assert _validate_source(" http://127.0.0.1:4747/video ") == "http://127.0.0.1:4747/video"
    assert _validate_source("https://example.invalid/stream").startswith("https://")
    assert _validate_source("rtsp://127.0.0.1/live").startswith("rtsp://")
    for bad in ("file:///etc/passwd", "concat:file:///x", "pipes:///x", "/etc/passwd", "", None, -1, "http://x\x00y"):
        with pytest.raises(ValueError):
            _validate_source(bad)


def test_F6_reconnect_backoff_grows_and_caps_at_30s():
    state = SharedSystemState([], stride=1)
    pipe = CameraPipeline("cam1", "CAM 1", "http://127.0.0.1:4747/video", state)
    delays = []
    for _ in range(9):
        delays.append(pipe._reconnect_delay())
        pipe.reconnect_attempts = min(pipe.reconnect_attempts + 1, 6)
    assert delays[:5] == [2.0, 4.0, 8.0, 16.0, 30.0]
    assert max(delays) <= 30.0
    pipe.reconnect_attempts = 0
    assert pipe._reconnect_delay() == 2.0, "success must reset the backoff"
    state.shutdown()


def test_F7_hot_swap_queues_source_without_touching_handle():
    state = SharedSystemState([], stride=1)
    state.apply_cameras([{"id": "cam1", "label": "CAM 1", "source": 0}], initial=True)
    pipe = state.cameras["cam1"]
    pipe._source_dirty = False
    new_source = "http://127.0.0.1:4747/video"
    pipe.set_source(new_source)
    assert pipe._source_dirty is True
    assert pipe._pending_source == new_source
    assert pipe.source == 0, "handle must not change until the worker applies the swap"
    # Idempotent re-set is ignored.
    pipe.set_source(new_source)
    assert pipe._pending_source == new_source
    state.shutdown()


def test_F8_hysteresis_constants_are_asymmetric():
    assert ESCALATE_HOLD_SECONDS == 1.5
    assert DEESCALATE_HOLD_SECONDS == 4.0
    assert DEESCALATE_HOLD_SECONDS > ESCALATE_HOLD_SECONDS, \
        "de-escalation must be slower to avoid flapping"


def test_F9_credentials_never_appear_in_masked_source():
    from alert_vision import _source_display
    masked = _source_display("rtsp://admin:supersecret@192.168.1.10:554/live")
    assert "supersecret" not in masked and "admin" not in masked
    assert "***" in masked


def test_F10_dev_requirement_matches_installed_pytest():
    import pytest as pytest_module
    with open(os.path.join(PROJECT_ROOT, "requirements-dev.txt"), "r", encoding="utf-8") as handle:
        spec = handle.read()
    assert "pytest>=8.3.3,<10" in spec
    major, minor, *_ = pytest_module.__version__.split(".")
    assert (int(major), int(minor)) >= (8, 3)
    assert int(major) < 10
