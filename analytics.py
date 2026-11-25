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
# I-B1/I-B2: all time-derived terms are normalized against a nominal 15 FPS
# sample so the same real-world change scores the same at stride 1 or stride 10.
NOMINAL_SAMPLE_DT = 1.0 / 15.0
# A normalized-density rise of 2.0 per second (10 people/s at min_people=3)
# is treated as a full rate-of-change signal. Documented, not a physical law.
RATE_FULL_PER_SEC = 2.0
# Calibrated density anchor: 1 person/m^2 -> 0, 6 people/m^2 -> 1 (the
# CRITICAL density threshold). Used only when the camera has a calibrated area.
DENSITY_NORM_LOW = 1.0
FLOW_DT_MIN = 1e-3
FLOW_DT_MAX = 1.0


# ---------------------------------------------------------------------------
# Optical flow signal
# ---------------------------------------------------------------------------
def flow_velocity(flow, dt: float) -> float:
    """Mean optical-flow magnitude in pixels/second (I-B3).

    Callers must pass the measured wall-clock ``dt`` between the two frames; a
    per-frame magnitude is not comparable across camera FPS or inference stride.
    """
    flow_arr = np.asarray(flow)
    if flow_arr.size == 0:
        return 0.0
    secs = min(max(float(dt) if dt else NOMINAL_SAMPLE_DT, FLOW_DT_MIN), FLOW_DT_MAX)
    magnitude = np.hypot(flow_arr[..., 0].astype(np.float32), flow_arr[..., 1].astype(np.float32))
    return float(np.mean(magnitude)) / secs


def compute_flow_signal(prev_gray, gray, dt: float = NOMINAL_SAMPLE_DT, mask=None, flow=None,
                        compensate_zoom: bool = True):
    """Return ``(turbulence, divergence_per_second)`` for one frame pair.

    * turbulence: ``1 - |resultant flow angle|`` over moving pixels (0 =
      aligned motion, 1 = full chaos).
    * divergence: motion-masked spatial divergence proxy, normalized to
      **per second** using the measured ``dt`` (I-B3). Camera translation is
      removed via the global median; a least-squares radial term is optionally
      removed to suppress camera zoom (I-B4). Static background pixels are
      excluded from the divergence estimate (I-B4).

    ``flow`` may carry a precomputed Farneback flow matrix; when omitted it is
    computed from the two frames. A caller-supplied ``mask`` overrides the
    automatic motion mask.
    """
    # A caller may supply a precomputed flow matrix without the gray frames
    # (used by synthetic regression tests and advanced callers).
    if flow is None and (prev_gray is None or gray is None):
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
    else:
        mask = np.asarray(mask, dtype=bool)
    sampled = angles[mask]
    if sampled.size > 0:
        resultant = float(np.hypot(float(np.mean(np.sin(sampled))), float(np.mean(np.cos(sampled)))))
        turbulence = float(1.0 - resultant)
    else:
        turbulence = 0.0

    # I-B4: motion-masked divergence. Without this, stationary background
    # pixels dominate the mean gradient and dilute genuine crowd convergence.
    ys, xs = np.nonzero(mask)
    if ys.size < 4 or fx.size == 0:
        return float(turbulence), 0.0

    if compensate_zoom and ys.size > 16:
        # Remove a least-squares radial (zoom) component from the moving
        # pixels: flow ~= a * (x - cx, y - cy).  Pure translation is already
        # removed by the median above; pure zoom therefore collapses to ~0.
        cx = (fx.shape[1] - 1) / 2.0
        cy = (fx.shape[0] - 1) / 2.0
        rx = xs.astype(np.float32) - cx
        ry = ys.astype(np.float32) - cy
        denom = float(np.dot(rx, rx) + np.dot(ry, ry))
        if denom > 1e-6:
            radial = (float(np.dot(rx, fx[ys, xs])) + float(np.dot(ry, fy[ys, xs]))) / denom
            fx[ys, xs] -= radial * rx
            fy[ys, xs] -= radial * ry

    grad = np.gradient(fx, axis=1) + np.gradient(fy, axis=0)
    divergence_raw = float(np.mean(grad[ys, xs]))
    secs = min(max(float(dt) if dt else NOMINAL_SAMPLE_DT, FLOW_DT_MIN), FLOW_DT_MAX)
    return float(turbulence), float(divergence_raw / secs)


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
    # Reference smoothing per NOMINAL_SAMPLE_DT (15 FPS). Effective alpha is
    # recomputed from measured dt so a 5 FPS stream is smoothed the same as a
    # 15 FPS stream over the same wall-clock interval (I-B2).
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
        # I-B1/I-B2: wall-clock bookkeeping for FPS/stride-independent terms.
        self._last_add_at = None
        self._last_sample_dt = NOMINAL_SAMPLE_DT
        self.smooth_turb = 0.0
        self.smooth_risk = 0.0
        self.last_terms = {
            "density": 0.0, "rate_of_change": 0.0, "convergence": 0.0,
            "turbulence": 0.0, "density_per_sqm": 0.0,
        }
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
        self._last_add_at = None
        self._last_sample_dt = NOMINAL_SAMPLE_DT
        self.smooth_turb = 0.0
        self.smooth_risk = 0.0
        self.last_terms = {
            "density": 0.0, "rate_of_change": 0.0, "convergence": 0.0,
            "turbulence": 0.0, "density_per_sqm": 0.0,
        }

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

    @staticmethod
    def _effective_alpha(dt: float) -> float:
        """EMA alpha for ``dt`` seconds, equivalent to LEVELING_ALPHA @15 FPS."""
        ratio = min(max(float(dt) / NOMINAL_SAMPLE_DT, 0.05), 20.0)
        return float(1.0 - (1.0 - LiveAnalyzer.LEVELING_ALPHA) ** ratio)

    def add(self, count, turb, div, w_density=None, w_rate_of_change=None, w_convergence=None,
            w_turbulence=None, min_people=None, dt=None, area_sqm=None) -> float:
        """Feed one analysed frame; returns the time-smoothed risk score (0..1).

        ``dt`` is the measured seconds since the previous analysed frame. When
        omitted it is derived from wall-clock samples, so callers that already
        know the interval (the camera worker) should pass it explicitly.

        ``area_sqm`` (when calibrated, >0) feeds the physical density anchor
        into the density term (I-B5); with no calibration the legacy people-
        count normalization is used.
        """
        count = float(count)
        turb = float(turb)
        div = float(div)
        now = time.monotonic()
        if self._last_add_at is None:
            sample_dt = float(dt) if dt is not None else NOMINAL_SAMPLE_DT
        else:
            sample_dt = float(dt) if dt is not None else (now - float(self._last_add_at))
        sample_dt = min(max(sample_dt, 0.001), 2.0)
        self._last_add_at = now
        self._last_sample_dt = sample_dt
        alpha = self._effective_alpha(sample_dt)

        self.people.append(count)
        self.turb.append(turb)
        self.div.append(div)

        weights, mp = self._resolve_weights(w_density, w_rate_of_change, w_convergence, w_turbulence, min_people)
        den_count = self._demean_norm(count, mp - 1.0, mp + 4.0)
        psm = density_per_sqm(count, area_sqm) if area_sqm and float(area_sqm) > 0.0 else 0.0
        # I-B5: calibrated density can only ADD risk, never mask the count term.
        den_cal = self._demean_norm(psm, DENSITY_NORM_LOW, DENSITY_CRITICAL_PER_SQM) if psm > 0.0 else 0.0
        den = float(max(den_count, den_cal))

        # I-B1: first sample has no rate history -> no spurious spike.
        if len(self.people) <= 1:
            rate_norm = 0.0
        else:
            rate_norm = float(np.clip(max(den - self.prev_den, 0.0) / sample_dt / RATE_FULL_PER_SEC, 0.0, 1.0))
        self.prev_den = den

        presence = float(np.clip(count / max(mp, 1.0), 0.0, 1.0))
        turb_n = self._demean_norm(turb, self.TURB_LOW, self.TURB_FULL)
        self.smooth_turb = alpha * turb_n + (1.0 - alpha) * self.smooth_turb
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
                    + weights["rate_of_change"] * rate_norm
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
            self.smooth_risk = (1.0 - alpha) * self.smooth_risk + alpha * risk_val
        self.last_terms = {
            "density": float(den),
            "rate_of_change": float(rate_norm),
            "convergence": float(conv),
            "turbulence": float(turb_smooth),
            "density_per_sqm": float(psm),
        }
        self.risk.append(float(self.smooth_risk))
        self.risk_times.append(time.time())
        return float(self.smooth_risk)

    def forecast_estimate(self, critical_at: float = None, min_samples: int = 6,
                          max_horizon: float = 180.0, min_span_seconds: float = 2.0) -> dict:
        """Honest linear-trend estimate (I-B6).

        Returns a dict with ``eta_s``, ``slope_per_s``, ``r2``, ``samples`` and
        an explicit ``caveat``. A straight-line extrapolation of an EMA is a
        *trend estimate*, not a prediction or a calibrated time-to-event.
        ``None`` eta means "no reliable estimate", never "no risk".
        """
        empty = {
            "eta_s": None, "slope_per_s": None, "r2": None, "samples": len(self.risk),
            "method": "least-squares linear trend over recent EMA samples",
            "caveat": "estimate only; not a calibrated prediction",
        }
        critical = float(self.critical_at if critical_at is None else critical_at)
        if len(self.risk) < int(min_samples):
            return empty
        times = np.asarray(self.risk_times, dtype=float)
        risks = np.asarray(self.risk, dtype=float)
        t_rel = times - times[0]
        span = float(t_rel[-1] - t_rel[0])
        # Guard duplicate timestamps explicitly; avoids np.polyfit RankWarning.
        if span < float(min_span_seconds) or not np.any(np.diff(t_rel) > 0.0):
            return empty
        tc = t_rel - float(np.mean(t_rel))
        rc = risks - float(np.mean(risks))
        denom = float(np.dot(tc, tc))
        if denom <= 1e-9:
            return empty
        slope = float(np.dot(tc, rc)) / denom
        intercept = float(np.mean(risks)) - slope * float(np.mean(t_rel))
        predicted = intercept + slope * t_rel
        ss_res = float(np.sum((risks - predicted) ** 2))
        ss_tot = float(np.sum(rc ** 2))
        r2 = float(1.0 - ss_res / ss_tot) if ss_tot > 1e-12 else 0.0
        current_risk = float(risks[-1])
        eta = None
        if slope > 1e-4 and current_risk < critical:
            candidate = (critical - current_risk) / slope
            if 0.0 < candidate <= float(max_horizon):
                eta = float(candidate)
        empty.update({
            "eta_s": eta, "slope_per_s": slope, "r2": float(np.clip(r2, 0.0, 1.0)),
            "samples": len(self.risk),
        })
        return empty

    def forecast_seconds_to_critical(self, critical_at: float = None, min_samples: int = 6,
                                     max_horizon: float = 180.0):
        """Backward-compatible eta-only wrapper around :meth:`forecast_estimate`."""
        return self.forecast_estimate(critical_at, min_samples, max_horizon)["eta_s"]


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
