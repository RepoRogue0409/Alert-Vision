# CV accuracy and measurement limitations (Phase B)

> This document exists so the dashboard never implies an unmeasured accuracy
> claim. Risk levels are **heuristics**, not certified crowd-safety measurements.

## What IS verified by automated tests

| Property | Evidence |
|---|---|
| `level_for` ordering (SAFE/MODERATE/HIGH/CRITICAL) | `tests/test_core_logic.py`, `tests/test_phase_b_analytics.py` |
| Density thresholds: 4/m² HIGH, 6/m² CRITICAL (when calibrated) | `tests/test_phase_b_analytics.py::test_B5_*` |
| Rate-of-change and EMA are independent of FPS/stride | `tests/test_phase_b_analytics.py::test_B1_*`, `test_B2_*` |
| Optical-flow velocity is per second; divergence uses measured `dt` | `tests/test_phase_b_analytics.py::test_B3_*` |
| Camera zoom radial term is compensated; static pixels excluded | `tests/test_phase_b_analytics.py::test_B4_*` |
| Forecast ETA carries a caveat and refuses flat/duplicate-timestamp fits | `tests/test_phase_b_analytics.py::test_B6_*` |
| Cumulative ByteTrack IDs are labelled "NOT unique people" | `tests/test_phase_b_analytics.py::test_B8_*` |
| HIGH threshold has one source of truth | `tests/test_phase_b_analytics.py::test_B9_*` |

## What is NOT verified (no labelled dataset, no ground truth)

| Item | Status | Why |
|---|---|---|
| Person-detection recall at 640 px / `conf=0.25` | **NOT VERIFIED** | Needs a labelled video with small/distant people. |
| ByteTrack ID-switch rate | **NOT VERIFIED** | Needs labelled multi-person trajectories. `unique_ids` is explicitly a cumulative track-ID counter, never a headcount. |
| Small-person misses at stride 10 | **NOT VERIFIED** | Stride trades temporal sampling for CPU; no accuracy measurement exists. |
| Physical density (people/m²) | **ESTIMATE ONLY** | Derived from a single camera's `area_sqm`/geometry assumption, not a calibrated plan-view sensor. |
| Linear ETA to critical | **ESTIMATE ONLY** | Least-squares trend over recent EMA samples; `r2` and sample count are published, and the UI prefixes `~` / `(est)`. |
| Turbulence / convergence / divergence | **HEURISTIC SIGNALS** | Farneback flow proxies on downscaled grayscale frames; camera zoom is compensated, pan is median-compensated, but occlusion and lighting changes are not modelled. |

## Pre-processing contract (verified by code/tests)

* Frames wider than `MAX_FRAME_WIDTH = 640` are downscaled with `INTER_AREA`.
* Inference runs on every Nth captured frame where N = configured stride (1..10).
* YOLO class filter = person only; confidence threshold configurable via
  `ALERT_VISION_CONF` (clamped 0.05..0.95).
* Latency (not accuracy) can be measured with `tools/benchmark_inference.py`.

## Honest wording rules for UI/docs

* Say "estimated density", never "measured crowd density" unless a calibrated
  floor area is configured.
* Say "~Xs (est)" for ETA, never "will reach critical in Xs".
* Say "track IDs seen", never "unique people" for ByteTrack counters.
* Alert levels are decision support; the CSV/UI must not be presented as a
  certified life-safety system.
