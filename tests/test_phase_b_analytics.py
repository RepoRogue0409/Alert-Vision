"""Phase B regression tests: 10 synthetic CV/analytics scenarios.

All tests import the REAL modules and use synthetic signals only - no camera,
no YOLO weights download, no labelled crowd dataset required.
"""
import math
import os
import sys
from collections import deque

import numpy as np
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from alert_vision import (  # noqa: E402
    DEFAULT_ALERT_THRESHOLD,
    MAX_FRAME_WIDTH,
    CameraPipeline,
    SharedSystemState,
)
from analytics import (  # noqa: E402
    CRITICAL_AT,
    DEFAULT_WEIGHTS,
    DENSITY_CRITICAL_PER_SQM,
    NOMINAL_SAMPLE_DT,
    LiveAnalyzer,
    combine_levels,
    compute_flow_signal,
    density_forced_level,
    density_history_vote,
    density_per_sqm,
    flow_velocity,
    level_for,
)


def _rate_analyzer():
    a = LiveAnalyzer(window=64, min_people=3)
    a.weights = dict(DEFAULT_WEIGHTS)
    return a


# ---------------------------------------------------------------------------
# Scenario 1 (I-B1): rate-of-change is FPS/stride independent
# ---------------------------------------------------------------------------
def test_B1_rate_term_is_fps_independent():
    def run(fps):
        dt = 1.0 / fps
        a = LiveAnalyzer(window=64, min_people=3)
        for i in range(int(round(1.0 / dt))):
            t = (i + 1) * dt
            count = 3.0 + 5.0 * t  # +5 people/second, same real-world rise
            a.add(count, 0.0, 0.0, w_density=0.0, w_rate_of_change=1.0,
                  w_convergence=0.0, w_turbulence=0.0, dt=dt)
        return a.last_terms["rate_of_change"]

    assert run(15) == pytest.approx(run(5), abs=1e-6), \
        "same real-world rise over the same second must score the same at 5/15 FPS"


# ---------------------------------------------------------------------------
# Scenario 2 (I-B2): EMA smoothing is time-based, not sample-count based
# ---------------------------------------------------------------------------
def test_B2_ema_is_time_constant_independent_of_fps():
    def run(fps, seconds=3.0):
        dt = 1.0 / fps
        a = LiveAnalyzer(window=128, min_people=3)
        for _ in range(int(round(seconds / dt))):
            a.add(8.0, 0.0, 0.0, w_density=1.0, w_rate_of_change=0.0,
                  w_convergence=0.0, w_turbulence=0.0, dt=dt)
        return a.smooth_risk

    fast, slow = run(15), run(5)
    assert fast == pytest.approx(slow, abs=1e-6), \
        "3 seconds of constant input must smooth identically at 5/15 FPS"
    expected = 1.0 - (1.0 - LiveAnalyzer.LEVELING_ALPHA) ** (3.0 / NOMINAL_SAMPLE_DT)
    assert fast == pytest.approx(expected, abs=1e-6)


# ---------------------------------------------------------------------------
# Scenario 3 (I-B3): flow velocity is per second, divergence uses dt
# ---------------------------------------------------------------------------
def test_B3_flow_velocity_and_divergence_use_dt():
    flow = np.zeros((32, 32, 2), dtype=np.float32)
    flow[..., 0] = np.arange(32, dtype=np.float32) * 0.1
    assert flow_velocity(flow, 0.1) == pytest.approx(flow_velocity(flow, 0.2) * 2.0, rel=1e-6)

    _, div_a = compute_flow_signal(None, None, dt=0.1, flow=flow, compensate_zoom=False)
    _, div_b = compute_flow_signal(None, None, dt=0.2, flow=flow, compensate_zoom=False)
    assert abs(div_a) > 0.0
    assert div_a == pytest.approx(div_b * 2.0, rel=1e-5)


# ---------------------------------------------------------------------------
# Scenario 4 (I-B4): zoom compensation + motion-masked divergence
# ---------------------------------------------------------------------------
def test_B4_zoom_flow_is_compensated():
    ys, xs = np.indices((64, 64), dtype=np.float32)
    flow = np.zeros((64, 64, 2), dtype=np.float32)
    flow[..., 0] = 0.05 * (xs - 31.5)
    flow[..., 1] = 0.05 * (ys - 31.5)
    _, raw = compute_flow_signal(None, None, dt=1.0, flow=flow, compensate_zoom=False)
    _, compensated = compute_flow_signal(None, None, dt=1.0, flow=flow, compensate_zoom=True)
    assert abs(raw) > 0.01, "synthetic zoom must produce a divergence signal before compensation"
    assert abs(compensated) < abs(raw) * 0.25, "radial zoom term must be largely removed"

    # Static background is excluded: no motion pixels -> no divergence.
    still = np.zeros((64, 64, 2), dtype=np.float32)
    _, still_div = compute_flow_signal(None, None, dt=1.0, flow=still, compensate_zoom=False)
    assert still_div == 0.0


# ---------------------------------------------------------------------------
# Scenario 5 (I-B5): calibrated density feeds the risk density term
# ---------------------------------------------------------------------------
def test_B5_calibrated_density_can_only_add_risk():
    def risk(area):
        a = LiveAnalyzer(window=16, min_people=3)
        return a.add(5.0, 0.0, 0.0, w_density=1.0, w_rate_of_change=0.0,
                     w_convergence=0.0, w_turbulence=0.0, dt=NOMINAL_SAMPLE_DT,
                     area_sqm=area)

    uncalibrated = risk(None)
    calibrated = risk(1.0)  # 5 people / 1 m^2 -> 0.8 density-normalized
    assert calibrated > uncalibrated
    assert density_per_sqm(5, 1.0) == pytest.approx(5.0)
    assert density_forced_level(5, 1.0) == "HIGH"
    assert density_forced_level(6, 1.0) == "CRITICAL"
    assert density_forced_level(5, 0.0) is None


# ---------------------------------------------------------------------------
# Scenario 6 (I-B6): forecast is an honest, bounded estimate
# ---------------------------------------------------------------------------
def test_B6_forecast_estimate_has_eta_and_caveat():
    a = LiveAnalyzer(window=16)
    a.risk_times = [100.0 + i * 0.5 for i in range(8)]
    a.risk = [0.2 + i * 0.05 for i in range(8)]  # slope 0.1/s, last = 0.55
    est = a.forecast_estimate(critical_at=0.85, min_samples=6)
    assert est["eta_s"] == pytest.approx(3.0, abs=1e-6)
    assert est["r2"] > 0.99
    assert est["samples"] == 8
    assert "estimate" in est["caveat"].lower()

    flat = LiveAnalyzer(window=16)
    flat.risk_times = [100.0 + i * 0.5 for i in range(8)]
    flat.risk = [0.4] * 8
    assert flat.forecast_estimate(critical_at=0.85, min_samples=6)["eta_s"] is None

    duplicate = LiveAnalyzer(window=16)
    duplicate.risk_times = [100.0] * 8
    duplicate.risk = [0.2 + i * 0.05 for i in range(8)]
    assert duplicate.forecast_estimate(critical_at=0.85, min_samples=6)["eta_s"] is None


# ---------------------------------------------------------------------------
# Scenario 7 (I-B7): density history vote debounces clearing
# ---------------------------------------------------------------------------
def test_B7_density_history_vote_debounces_clear():
    history = deque(maxlen=8)
    for _ in range(8):
        history.append("HIGH")
    assert density_history_vote(history, agree_needed=4) == "HIGH"
    for _ in range(5):
        history.append("SAFE")
    assert density_history_vote(history, agree_needed=4) is None, \
        "five SAFE samples leave fewer than four HIGH votes -> cleared"

    state = SharedSystemState([], stride=1)
    pipe = CameraPipeline("cam1", "CAM 1", 0, state)
    assert hasattr(pipe, "density_level_history") and pipe.density_level_history.maxlen == 8
    state.shutdown()


# ---------------------------------------------------------------------------
# Scenario 8 (I-B8): track IDs are never presented as unique people
# ---------------------------------------------------------------------------
def test_B8_track_ids_are_labelled_not_unique_people():
    state = SharedSystemState([], stride=1)
    pipe = CameraPipeline("cam1", "CAM 1", 0, state)
    packet = pipe._offline_packet()
    assert "unique_ids" in packet  # backward compatibility
    assert packet["track_ids_seen"] == packet["unique_ids"]
    meaning = packet["unique_ids_meaning"].lower()
    assert "not unique people" in meaning and "cumulative" in meaning
    state.shutdown()


# ---------------------------------------------------------------------------
# Scenario 9 (I-B9): one HIGH threshold drives formula and alert gate
# ---------------------------------------------------------------------------
def test_B9_high_threshold_has_single_source_of_truth():
    state = SharedSystemState([], stride=1)
    state.update_config({"alert_threshold": 0.70})
    cfg = state.config_snapshot()
    assert cfg["alert_threshold"] == pytest.approx(0.70)
    assert cfg["risk_thresholds"]["high"] == pytest.approx(0.70)
    assert level_for(0.71, cfg["alert_threshold"], CRITICAL_AT) == "HIGH"
    assert 0.71 >= cfg["alert_threshold"]

    state.update_config({"risk_thresholds": {"high": 0.62}})
    cfg = state.config_snapshot()
    assert cfg["alert_threshold"] == pytest.approx(cfg["risk_thresholds"]["high"]) == pytest.approx(0.62)

    state.update_config({"alert_threshold": 0.99})
    cfg = state.config_snapshot()
    assert cfg["alert_threshold"] < cfg["risk_thresholds"]["critical"]
    assert cfg["alert_threshold"] == pytest.approx(cfg["risk_thresholds"]["high"])
    state.shutdown()


# ---------------------------------------------------------------------------
# Scenario 10 (I-B10): frame/stride pre-processing contract (latency only)
# ---------------------------------------------------------------------------
def test_B10_preprocessing_contract_and_bounded_stride():
    assert MAX_FRAME_WIDTH == 640
    state = SharedSystemState([], stride=1)
    state.update_config({"stride": 99})
    assert state.config_snapshot()["stride"] == 10
    state.update_config({"stride": 0})
    assert state.config_snapshot()["stride"] == 1
    state.shutdown()
    # Accuracy / ID-switch rate is NOT asserted here: it needs a labelled
    # dataset. docs/ACCURACY_LIMITATIONS.md records this as NOT VERIFIED.


def test_level_helpers_never_downgrade_synthetic_levels():
    assert combine_levels("SAFE", "HIGH") == "HIGH"
    assert combine_levels("CRITICAL", "MODERATE") == "CRITICAL"
    assert level_for(0.0, DEFAULT_ALERT_THRESHOLD) == "SAFE"
    assert math.isfinite(density_per_sqm(10, 4.0))
