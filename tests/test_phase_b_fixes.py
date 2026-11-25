"""Round-2 Phase B smoke tests: apply_cameras must keep old pipeline on invalid spec."""
import os
import sys

from alert_vision import SharedSystemState  # noqa: E402


def test_B6_invalid_camera_spec_keeps_existing_pipeline():
    """B6: an invalid new camera spec must NOT delete an existing pipeline."""
    state = SharedSystemState([], stride=1)
    state.apply_cameras(
        [{"id": "cam1", "label": "CAM 1", "source": 0}],
        initial=True,
    )
    assert "cam1" in state.cameras, "initial camera should be registered"
    existing = state.cameras["cam1"]

    # Invalid source spec (not http/https/rtsp) -> must be rejected, old pipeline kept.
    state.apply_cameras(
        [{"id": "cam1", "label": "CAM 1", "source": "bad://not-a-stream"}],
        initial=True,
    )
    assert "cam1" in state.cameras, "existing camera must NOT be deleted on invalid spec"
    assert state.cameras["cam1"] is existing, "existing pipeline must be preserved"
