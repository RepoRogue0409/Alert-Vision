"""Round-2 Phase A smoke tests: telemetry broadcast, criticality, credential masking.

These tests import the REAL modules (alert_vision / analytics) and run without hardware.
"""
import asyncio
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from alert_vision import AlertVisionBackend, SharedSystemState, CameraPipeline, _source_display  # noqa: E402
from analytics import level_for, density_forced_level, CRITICAL_AT  # noqa: E402


class FakeBroadcaster:
    def __init__(self):
        self.sent = []

    def active_count(self):
        return 1

    async def broadcast(self, packet):
        self.sent.append(packet)


def test_A1_telemetry_broadcast_sent_each_tick_with_zero_alerts():
    """A1: full telemetry packet must be broadcast every tick even with zero alerts."""
    backend = AlertVisionBackend([], stride=1)
    fake = FakeBroadcaster()
    backend.broadcaster = fake

    packet = asyncio.run(backend._broadcast_tick())

    assert packet.get("type") == "telemetry", f"expected telemetry packet, got {packet.get('type')}"
    assert isinstance(packet.get("cameras"), list)
    # Zero alerts -> exactly one broadcast (the telemetry packet), no alert packets.
    assert len(fake.sent) == 1, f"expected 1 broadcast, got {len(fake.sent)}"
    assert fake.sent[0] is packet


def test_A3_critical_reachable_via_formula_and_density():
    """A3: CRITICAL must be reachable (formula at >= CRITICAL_AT, density at 6/m^2)."""
    # Formula: risk >= critical_at -> CRITICAL
    assert level_for(0.9, 0.55, CRITICAL_AT) == "CRITICAL"
    # Calm crowd (low risk) reaches at least HIGH when density is dangerous.
    assert density_forced_level(4, 1.0) == "HIGH"
    # Chaotic saturated crowd at 6/m^2 -> forced CRITICAL.
    assert density_forced_level(6, 1.0) == "CRITICAL"
    # 40-person chaotic converging crowd -> CRITICAL at critical threshold.
    assert level_for(0.95, 0.55, CRITICAL_AT) == "CRITICAL"


def test_A5_no_raw_source_in_telemetry():
    """A5: telemetry must send masked source + source_value_masked, never raw creds."""
    state = SharedSystemState([], stride=1)
    pipe = CameraPipeline(
        "cam1", "CAM 1", "rtsp://admin:s3cret@192.168.1.50:554/stream1", state, area_sqm=None
    )
    snap = pipe.snapshot()
    assert "source_value" not in snap, "raw source_value must not appear in telemetry"
    assert "source_value_masked" in snap, "source_value_masked must be present in telemetry"
    masked = str(snap.get("source_value_masked") or snap.get("source") or "")
    assert "s3cret" not in masked, "credential must be masked"
    assert "***@" in masked, "masked source should hide user:pass"
    assert str(snap.get("source") or "") == masked
    offline = pipe._offline_packet()
    assert "source_value" not in offline
    assert "source_value_masked" in offline
    assert "s3cret" not in str(offline.get("source_value_masked") or "")
