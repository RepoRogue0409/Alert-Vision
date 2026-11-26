# Fact-check register for the Alert Vision research paper

Every important claim in `Alert_Vision_Research_Paper.md` is mapped below to the
repository source or measurement that supports it. Verification type codes:

- **CODE** — directly observable in implementation source.
- **TEST** — covered by an automated test in `tests/`.
- **MEASURED** — recorded output of a benchmark/verification tool.
- **DOC** — project documentation, used only where code was also checked.
- **NOT VERIFIED** — explicitly not claimed as validated.

## A. System and architecture

| # | Claim in paper | Evidence | Type |
|---:|---|---|---|
| A1 | Max six cameras | `alert_vision.py:93 MAX_CAMERAS=6`; `tests/test_phase_b_analytics.py::test_B10_*` | CODE/TEST |
| A2 | Ports 8000, 8765, 8766; all bound to 127.0.0.1 | `alert_vision.py:89-91`; `serve_static.py:12-13`; `docs/SECURITY.md` | CODE |
| A3 | Backend version 2.1.0 | `alert_vision.py:103 BACKEND_VERSION="2.1.0"`; `/health` payload | CODE |
| A4 | Static server whitelist serves `/`, `/index.html`, `/script.js` | `serve_static.py:35-46`; manual HTTP 200 check in `docs/VERIFICATION.md` | CODE/MEASURED |
| A5 | Per-camera worker thread plus model loader thread | `alert_vision.py::CameraPipeline.start()` and `_load_model()`; `docs/ARCHITECTURE.md` §1 | CODE/DOC |
| A6 | Model loads while capture connects; raw frames relayed with UNKNOWN risk | `alert_vision.py::_run()` offline/loading path; `script.js` badge handling | CODE |
| A7 | Capture open uses `CAP_FFMPEG` with bounded timeouts | `alert_vision.py::_open_capture()`; `docs/ARCHITECTURE.md` | CODE |
| A8 | DroidCam alternates `/video`, `/mjpegfeed?640x480`, `/mjpegfeed` | `alert_vision.py::_candidate_sources()` | CODE |
| A9 | Reconnect backoff 2→4→8→16→30 s and reset on success | `alert_vision.py::_reconnect_delay()`; `tests/test_phase_f_unit.py::test_F6_*` | CODE/TEST |
| A10 | Connection payload contains state, error, elapsed_ms, masked URL | `alert_vision.py::_connection_payload()`; test A4 | CODE/TEST |
| A11 | Capture handle released only by owning worker after join | `alert_vision.py::stop()` and `read_frame()`; Phase A changes | CODE |
| A12 | Credentials masked in logs/telemetry and excluded from localStorage | `alert_vision.py::_source_display()`; `script.js::savePersisted`; tests A5/F9 | CODE/TEST |

## B. Detection, tracking, and analytics

| # | Claim in paper | Evidence | Type |
|---:|---|---|---|
| B1 | YOLOv8n used for person detection | `alert_vision.py:117 YOLO_MODEL_PATH="yolov8n.pt"`; `requirements.txt` ultralytics pin; `README.md` | CODE |
| B2 | Person class id 0 | `alert_vision.py:119 PERSON_CLASS_ID=0` | CODE |
| B3 | Tracker is ByteTrack, configured by `bytetrack.yaml` | `alert_vision.py:121 TRACKER_CONFIG="bytetrack.yaml"`; `model.track(tracker=...)` at line 885 | CODE |
| B4 | No BoT-SORT claim | No `botsort` string in source; tracker config is ByteTrack | CODE |
| B5 | Confidence clamped 0.05–0.95 | `alert_vision.py:880-881` | CODE |
| B6 | Frames wider than 640 px downscaled with INTER_AREA | `alert_vision.py:847-854`; `docs/ACCURACY_LIMITATIONS.md` | CODE/DOC |
| B7 | Stride 1–10 applied to analysed frames | `alert_vision.py` config validation; `analytics` docs; tests B10 | CODE/TEST |
| B8 | Track IDs cumulative, not unique people | `alert_vision.py:1059-1062` `track_ids_seen` and meaning string; test B8 | CODE/TEST |
| B9 | No cross-camera identity claim | `docs/ACCURACY_LIMITATIONS.md`; `docs/VERIFICATION.md`; no cross-camera identity code | DOC/CODE |
| B10 | Four risk levels SAFE/MODERATE/HIGH/CRITICAL | `analytics.py:35 RISK_LEVELS`; `level_for()` lines 360-370 | CODE |
| B11 | Critical at 0.85 | `analytics.py:32 CRITICAL_AT=0.85` | CODE |
| B12 | Density gates 4/m² HIGH and 6/m² CRITICAL | `analytics.py:33-34`; `density_forced_level()` lines 406-413; test B5 | CODE/TEST |
| B13 | Uncalibrated density shown as "not calibrated" | `script.js` `updateFocus()`; packet field `density_calibrated`; test C4 | CODE/TEST |
| B14 | Farneback optical flow on half-resolution grayscale | `alert_vision.py::_flow_metrics()`; `analytics.py::compute_flow_signal()` | CODE |
| B15 | Median translation compensation | `analytics.py:96-98` | CODE |
| B16 | Motion mask magnitude > 0.3 | `analytics.py:102-105` | CODE |
| B17 | Radial zoom compensation | `analytics.py:119-131`; test B4 | CODE/TEST |
| B18 | Velocity in pixels/second | `analytics.py::flow_velocity()` lines 55-66 | CODE/TEST |
| B19 | Default weights 0.4/0.2/0.2/0.2 | `analytics.py:37 DEFAULT_WEIGHTS` | CODE |
| B20 | Time-normalized EMA and rate | `analytics.py::_effective_alpha()` and `add()` lines 216-292; tests B1/B2 | CODE/TEST |
| B21 | ETA is linear estimate with caveat, r², samples | `analytics.py::forecast_estimate()` lines 304-349; `script.js::formatEta()`; test B6 | CODE/TEST |
| B22 | Hysteresis 1.5 s escalate / 4 s de-escalate | `alert_vision.py:140-141`; process_frame lines 975-990; test F8 | CODE/TEST |
| B23 | Alert cooldown 30 s | `alert_vision.py` `ALERT_COOLDOWN_SECONDS=30.0`; process_frame lines 995-1005 | CODE |
| B24 | HIGH threshold single source of truth | `SharedSystemState.validate_config()` mirroring; test B9 | CODE/TEST |

## C. Communication, alerts, and frontend

| # | Claim in paper | Evidence | Type |
|---:|---|---|---|
| C1 | Telemetry broadcast every 100 ms | `alert_vision.py:95 BROADCAST_INTERVAL=0.1`; `_broadcast_tick()` | CODE |
| C2 | Separate alert queue (16) and telemetry queue (64) | `alert_vision.py:1634-1635` | CODE |
| C3 | Session loop drains alerts first | `alert_vision.py:1721-1753`; tests A5, F3 | CODE/TEST |
| C4 | `broadcast()` returns acceptance count | `alert_vision.py:1689-1719` | CODE |
| C5 | Queue acceptance distinct from browser receipt | `alert_vision.py` lifecycle fields; `script.js` sends `alert_ack` in `showAlarm()`; tests A6/frontend ack test | CODE/TEST |
| C6 | Frontend severity-ranked alert queue | `script.js::queueAlert`, `nextQueuedAlert`, `showAlarm`; test C1 | CODE/TEST |
| C7 | Telemetry does not re-trigger alarm banner | `script.js::handleTelemetry()` alert loop removed; test C2 | CODE/TEST |
| C8 | Incremental viewport DOM sync | `script.js::renderViewports()`; test C3 | CODE/TEST |
| C9 | CSV quoting and formula guard | `script.js::csvCell()`; test C5 | CODE/TEST |
| C10 | Ledger count increments | `script.js` `ledgerEventCount += 1`; test C6 | CODE/TEST |
| C11 | MJPEG re-attach on offline→online | `script.js::updateTile()`; `docs/VERIFICATION.md` | CODE |
| C12 | Alarm visual QA not completed | `docs/VERIFICATION.md` status table | DOC |

## D. Measurements and verification

| # | Claim in paper | Evidence | Type |
|---:|---|---|---|
| D1 | Hardware Windows 11, Ryzen 7 7435HS, 16 logical, 24,261 MB | `docs/PERFORMANCE.md` §Hardware; systeminfo output recorded during audit | MEASURED |
| D2 | CPU/RSS/telemetry/MJPEG table | `docs/PERFORMANCE.md` multi-camera table | MEASURED |
| D3 | YOLO latency table | `docs/PERFORMANCE.md` YOLO track latency section | MEASURED |
| D4 | 64 tests passed, 0 failed | `python -m pytest -q` output; `docs/VERIFICATION.md` | MEASURED |
| D5 | CONNECTED 2641 ms / FAILED 4641 ms | `tools/verify_connect.py` final run; `docs/VERIFICATION.md` | MEASURED |
| D6 | 4/4 benchmark runs successful | `tools/benchmark_multicam.py` output | MEASURED |
| D7 | Clean-venv pip dry run resolved pytest 9.2.0 | `pip install --dry-run -r requirements-dev.txt` output; `docs/VERIFICATION.md` | MEASURED |
| D8 | No tracked `.pyc` files | `git ls-files` check in Phase 0 | MEASURED |
| D9 | 12-client × 6-camera MJPEG not measured | `docs/PERFORMANCE.md` "What was NOT measured"; `docs/VERIFICATION.md` | DOC |
| D10 | Physical DroidCam not used in audit run | `docs/VERIFICATION.md`; benchmark uses fake_mjpeg | DOC |
| D11 | No accuracy/ID-switch validation | `docs/ACCURACY_LIMITATIONS.md` | DOC |

## E. Literature references

| Ref. | Claim supported | Verification performed |
|---:|---|---|
| [1] | Fruin pedestrian density context | HathiTrust catalogue record checked |
| [2] | Crowd panic/jamming simulation | Nature page and DOI `10.1038/35035023` checked |
| [3] | YOLO single-pass detection | IEEE/CVF CVF page and DOI `10.1109/CVPR.2016.91` checked |
| [4] | YOLOv8 software used | Official Ultralytics repo CITATION.cff and docs URL checked |
| [5] | ByteTrack method | ACM/Springer DOI `10.1007/978-3-031-20047-2_1` checked |
| [6] | Farneback polynomial optical flow | Springer/ScienceOpen DOI `10.1007/3-540-45103-X_50` checked |
| [7] | MCNN crowd counting | CVF page and DOI `10.1109/CVPR.2016.70` checked |
| [8] | CNN crowd-counting survey | DOI `10.1016/j.patrec.2017.07.007` checked |
| [9] | Recent crowd-density survey | IET DOI `10.1049/ipr2.13328` checked |
| [10] | Real-time crowd-analysis survey | MDPI/PubMed DOI `10.3390/s20185073` checked |
| [11] | Smart video surveillance review | ACM DOI `10.1145/3473141.3473240` checked |
| [12] | OpenCV 4.10.0 API documentation | Official docs URL fetched successfully |
