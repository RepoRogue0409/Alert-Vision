"""
Alert Vision - CrowdShield pure risk analytics
=============================================

Pure (no Streamlit, no cv2 UI) port of the CrowdShield crowd-risk engine.

This module contains ONLY the math / state:

* ``compute_flow_signal``            - optical-flow based turbulence + divergence
* ``LiveAnalyzer``                    - EMA-smoothed risk / density / convergence state
* ``level_for`` / ``density_*`` / ``combine_levels`` - level mapping + density cross-check
* ``confidence_live``                 - detection-confidence based risk confidence
* forecasting helpers                 - trend ETA to CRITICAL

It must stay free of any UI / streaming dependencies so it can be imported
and unit-tested directly (``from analytics import ...``).

Physics anchors (Fruin / Keith Still crowd-density research):
    * free-flow crowd density  ~2 people/m^2
    * restricted movement      ~4 people/m^2  -> forced HIGH
    * dangerous / crush risk  ~6 people/m^2  -> forced CRITICAL
"""

from __future__ import annotations

import math
import time
from collections import deque

import numpy as np

CRITICAL_AT = 0.85
DENSITY_WARN_PER_SQM = 4.0
DENSITY_CRITICAL_PER_SQM = 6.0
RISK_LEVELS = ("SAFE", "MODERATE", "HIGH", "CRITICAL")
LEVEL_RANK = {"SAFE": 0, "MODERATE": 1, "HIGH": 2, "CRITICAL": 3}
DEFAULT_WEIGHTS = {"density": 0.4, "rate_of_change": 0.2, "convergence": 0.2, "turbulence": 0.2}
DEFAULT_MIN_PEOPLE = 3


# ---------------------------------------------------------------------------
# Optical flow signal
# ---------------------------------------------------------------------------
def compute_flow_signal(prev_gray, gray, dt: float = 0.066, mask=None, flow=None):
    """Return ``(turbulence, divergence)`` for one frame pair.

    * turbulence: ``1 - |resultant flow angle|`` over samples with magnitude
      above the motion threshold (0 = no direction change, 1 = full chaos).
    * divergence: spatial divergence proxy derived from flow gradients.

    Camera-shake compensation subtracts the global median flow vector. Both
    outputs are ``0..1``-family signals (turbulence is exactly 0..1); ``dt`` is
    accepted for API compatibility and clamped out of the angle-based ratio.

    ``flow`` may carry a precomputed Farneback flow matrix (advanced callers);
    when omitted it is computed from the two frames.
    """
    if prev_gray is None or gray is None:
        return 0.0, 0.0
    import cv2

    if flow is None:
        flow = cv2.calcOpticalFlowFarneback(prev_gray, gray, None, 0.5, 3, 15, 3, 5, 1.2, 0)
    flow_arr = np.asarray(flow)
    fx = flow_arr[..., 0].astype(np.float32)
    fy = flow_arr[..., 1].astype(np.float32)
    median_flow = np.array([float(np.median(fx)), float(np.median(fy))], dtype=np.float32)
    fx = fx - median_flow[0]
    fy = fy - median_flow[1]
    magnitude, angles = cv2.cartToPolar(fx, fy)
    magnitude = magnitude.astype(np.float32)
    angles = angles.astype(np.float32)
    if mask is None:
        mask = magnitude > 0.3
    sampled = angles[mask]
    if sampled.size > 0:
        resultant = float(np.hypot(float(np.mean(np.sin(sampled))), float(np.mean(np.cos(sampled)))))
        turbulence = float(1.0 - resultant)
    else:
        turbulence = 0.0
    divergence = float(np.mean(np.gradient(fx, axis=1) + np.gradient(fy, axis=0))) if fx.size else 0.0
    return float(turbulence), float(divergence)


# ---------------------------------------------------------------------------
# Live analyzer
# ---------------------------------------------------------------------------
class LiveAnalyzer:
    """Rolling-window crowd-risk analyser (EMA smoothed, presence gated).

    The risk value fed to ``level_for`` is a 0..1 score. Density terms use the
    crowd density (people per frame-area), the rate of change of density, flow
    convergence and turbulence - each scaled by the UI weight set.
    """

    TURB_LOW = 0.15
    TURB_FULL = 0.75
    LEVELING_ALPHA = 0.25

    def __init__(self, window: int = 50, weights=None, min_people: float = DEFAULT_MIN_PEOPLE, critical_at: float = CRITICAL_AT):
        self.window = int(window)
        self.weights = dict(weights) if weights else dict(DEFAULT_WEIGHTS)
        self.min_people = float(min_people)
        self.critical_at = float(critical_at)
        self.people = deque(maxlen=self.window)
        self.turb = deque(maxlen=self.window)
        self.div = deque(maxlen=self.window)
        self.risk = deque(maxlen=self.window)
        self.risk_times = deque(maxlen=self.window)
        self.prev_gray = None
        self.prev_den = 0.0
        self.smooth_turb = 0.0
        self.smooth_risk = 0.0
        self.last_terms = {"density": 0.0, "rate_of_change": 0.0, "convergence": 0.0, "turbulence": 0.0}
        self.t0 = time.time()

    @staticmethod
    def _demean_norm(value, low: float, full: float) -> float:
        """Map a raw value to 0..1 with a deadband: below ``low`` -> 0."""
        return float(np.clip((float(value) - float(low)) / (float(full) - float(low) + 1e-9), 0.0, 1.0))

    def reset(self) -> None:
        """Clear all rolling state (used when a camera source is hot-swapped)."""
        self.people.clear()
        self.turb.clear()
        self.div.clear()
        self.risk.clear()
        self.risk_times.clear()
        self.prev_gray = None
        self.prev_den = 0.0
        self.smooth_turb = 0.0
        self.smooth_risk = 0.0
        self.last_terms = {"density": 0.0, "rate_of_change": 0.0, "convergence": 0.0, "turbulence": 0.0}

    def _resolve_weights(self, w_density, w_rate_of_change, w_convergence, w_turbulence, min_people):
        weights = {
            "density": float(w_density if w_density is not None else self.weights.get("density", DEFAULT_WEIGHTS["density"])),
            "rate_of_change": float(w_rate_of_change if w_rate_of_change is not None else self.weights.get("rate_of_change", DEFAULT_WEIGHTS["rate_of_change"])),
            "convergence": float(w_convergence if w_convergence is not None else self.weights.get("convergence", DEFAULT_WEIGHTS["convergence"])),
            "turbulence": float(w_turbulence if w_turbulence is not None else self.weights.get("turbulence", DEFAULT_WEIGHTS["turbulence"])),
        }
        total = float(sum(weights.values()))
        if not math.isfinite(total) or total <= 1e-9:
            weights = dict(DEFAULT_WEIGHTS)
            total = float(sum(weights.values()))
        return weights, float(min_people if min_people is not None else self.min_people)

    def add(self, count, turb, div, w_density=None, w_rate_of_change=None, w_convergence=None, w_turbulence=None, min_people=None) -> float:
        """Feed one analysed frame; returns the EMA-smoothed risk score (0..1)."""
        count = float(count)
        turb = float(turb)
        div = float(div)
        self.people.append(count)
        self.turb.append(turb)
        self.div.append(div)

        weights, mp = self._resolve_weights(w_density, w_rate_of_change, w_convergence, w_turbulence, min_people)
        window_peak = float(max(self.people)) if len(self.people) else 0.0
        den = self._demean_norm(count, mp - 1.0, mp + 4.0)
        ddt = den - self.prev_den
        self.prev_den = den

        presence = float(np.clip(count / max(mp, 1.0), 0.0, 1.0))
        turb_n = self._demean_norm(turb, self.TURB_LOW, self.TURB_FULL)
        self.smooth_turb = self.LEVELING_ALPHA * turb_n + (1.0 - self.LEVELING_ALPHA) * self.smooth_turb
        turb_smooth = self.smooth_turb * presence

        div_arr = np.asarray(self.div)
        conv = float(np.clip(-div / (float(np.std(div_arr)) * 3.0 + 1e-9), 0.0, 1.0)) * presence

        weight_total = float(
            weights["density"] + weights["rate_of_change"] + weights["convergence"] + weights["turbulence"]
        )
        risk_val = float(
            np.clip(
                (
                    weights["density"] * den
                    + weights["rate_of_change"] * max(ddt, 0.0)
                    + weights["convergence"] * conv
                    + weights["turbulence"] * turb_smooth
                )
                / max(weight_total, 1e-9),
                0.0,
                1.0,
            )
        )
        if count == 0.0:
            self.smooth_risk = 0.0
        else:
            self.smooth_risk = (1.0 - self.LEVELING_ALPHA) * self.smooth_risk + self.LEVELING_ALPHA * risk_val
        self.last_terms = {
            "density": float(den),
            "rate_of_change": float(max(ddt, 0.0)),
            "convergence": float(conv),
            "turbulence": float(turb_smooth),
        }
        self.risk.append(float(self.smooth_risk))
        self.risk_times.append(time.time())
        return float(self.smooth_risk)

    def forecast_seconds_to_critical(self, critical_at: float = None, min_samples: int = 6, max_horizon: float = 180.0):
        """Linear-fit ETA (seconds) until the risk crosses ``critical_at``."""
        critical = float(self.critical_at if critical_at is None else critical_at)
        if len(self.risk) < int(min_samples):
            return None
        times = np.asarray(self.risk_times, dtype=float)
        risks = np.asarray(self.risk, dtype=float)
        t_rel = times - times[0]
        if t_rel[-1] <= 0.0:
            return None
        slope, _intercept = np.polyfit(t_rel, risks, 1)
        if slope <= 1e-4:
            return None
        current_risk = float(risks[-1])
        if current_risk >= critical:
            return None
        eta = (critical - current_risk) / float(slope)
        if eta <= 0.0 or eta > float(max_horizon):
            return None
        return float(eta)


# ---------------------------------------------------------------------------
# Level mapping / density cross-check
# ---------------------------------------------------------------------------
def level_for(risk: float, threshold: float, critical_at: float = CRITICAL_AT) -> str:
    risk = float(risk)
    threshold = float(threshold)
    critical_at = float(critical_at)
    if risk >= critical_at:
        return "CRITICAL"
    if risk >= threshold:
        return "HIGH"
    if risk >= threshold * 0.60:
        return "MODERATE"
    return "SAFE"


def density_per_sqm(people_count: int, area_sqm: float) -> float:
    area = float(area_sqm)
    if area <= 0.0:
        return 0.0
    return float(people_count) / area


def estimate_ground_area(height_m: float, tilt_deg: float, hfov_deg: float = 70.0, vfov_deg: float = 55.0):
    """Auto-estimate the visible floor area (sq. m) from camera geometry.

    Returns ``(area_sqm, near_distance_m, far_distance_m)`` or ``None`` when
    the geometry puts the horizon inside the view (shallow tilt).
    """
    height = float(height_m)
    theta = math.radians(float(tilt_deg))
    half_v = math.radians(float(vfov_deg) / 2.0)
    near_angle = theta + half_v
    far_angle = theta - half_v
    if far_angle <= math.radians(1.0):
        return None
    if near_angle >= math.radians(89.5):
        near_m = height * 0.05
    else:
        near_m = height / math.tan(near_angle)
    far_m = height / math.tan(far_angle)
    depth_m = far_m - near_m
    half_h = math.radians(float(hfov_deg) / 2.0)
    width_near_m = 2.0 * near_m * math.tan(half_h)
    width_far_m = 2.0 * far_m * math.tan(half_h)
    area_sqm = 0.5 * (width_near_m + width_far_m) * depth_m
    return float(area_sqm), float(near_m), float(far_m)


def density_forced_level(people_count: int, area_sqm: float):
    """Return ``"HIGH"`` / ``"CRITICAL"`` when physical density is dangerous."""
    density = density_per_sqm(people_count, area_sqm)
    if density >= DENSITY_CRITICAL_PER_SQM:
        return "CRITICAL"
    if density >= DENSITY_WARN_PER_SQM:
        return "HIGH"
    return None


def combine_levels(formula_level: str, density_level) -> str:
    """Never downgrade - take the more severe of formula / density level."""
    if density_level is None:
        return formula_level
    return density_level if LEVEL_RANK.get(density_level, 0) > LEVEL_RANK.get(formula_level, 0) else formula_level


def density_history_vote(history, agree_needed: int = 6):
    """Majority-vote debounce for density-forced levels."""
    if history is None or len(history) < getattr(history, "maxlen", len(history) or 0):
        return None
    crit_votes = sum(1 for h in history if h == "CRITICAL")
    high_votes = sum(1 for h in history if h in ("CRITICAL", "HIGH"))
    if crit_votes >= int(agree_needed):
        return "CRITICAL"
    if high_votes >= int(agree_needed):
        return "HIGH"
    return None


def confidence_live(samples: int, det_confs, fps: float = 15.0) -> float:
    """Detection-confidence based risk confidence (0..1, warm-up ramp)."""
    samples = int(samples)
    det_confs = list(det_confs or [])
    fps = float(fps) if fps and fps > 0 else 15.0
    warmup = min(samples / max(fps * 3.0, 1.0), 1.0)
    if len(det_confs) == 0:
        return float(np.clip(0.50 * warmup, 0.0, 1.0))
    mean_conf = float(np.mean([float(c) for c in det_confs]))
    return float(np.clip(mean_conf * (0.6 + 0.4 * warmup), 0.0, 0.99))
