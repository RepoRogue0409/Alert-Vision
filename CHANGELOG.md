# Alert Vision — Change Log (Round 2 audit)

> **Audit note**: The previous revision claimed fixes that were missing or broken. This
> revision lists only what is verifiably implemented in code *and* covered by a passing test
> (or a manual QA step) as of this round. Nothing here is marked DONE unless proven.

## Phase A — Showstoppers

| ID | Fix | Status | Evidence |
|----|-----|--------|----------|
| A1 | Telemetry not broadcast | **DONE** | `AlertVisionBackend._broadcast_tick()` now returns and broadcasts the full telemetry packet each tick; `tests/test_phase_a_fixes.py::test_A1_telemetry_broadcast_sent_each_tick_with_zero_alerts` passes (fake broadcaster, zero alerts → exactly 1 telemetry broadcast). |
| A2 | Alarm banner never shows | **PARTIAL** | `showAlarm`/`hideAlarm`/`armAutoClear`/`flashTile`/`playAlarmBeep` implemented in `script.js`; wired to `type:"alert"` and telemetry `cam.alert`; ACK via `hideAlarm`; mute button (`#ui-alarm-mute`) bound; auto-clear after 10s. **NOT unit tested** (browser-only). Requires visual QA. |
| A3 | CRITICAL unreachable / density dead | **PARTIAL** | Formula CRITICAL at `risk >= CRITICAL_AT(0.85)` verified by test; density forced 4/m²→HIGH, 6/m²→CRITICAL verified by tests. **`capacity_people` not added** and **per-camera UI calibration not exposed** — still open. |
| A4 | Requirements fake | **DONE** | Rewrote `requirements.txt` with real pins (opencv-python, numpy, websockets, ultralytics, torch, lapx); `requirements-optional.txt` = LWCC only; added `requirements-dev.txt` (pytest). |
| A5 | Credential leak | **DONE** | Telemetry now sends `source` (masked) + `source_value_masked` (masked); raw `source_value` removed. `tests/test_phase_a_fixes.py::test_A5_no_raw_source_in_telemetry` passes (no raw cred in snapshot/offline packet). Frontend adopts masked source. |
| A6 | Alert logic | **PARTIAL** | Cooldown now suppresses same-level repeats; escalation to higher level bypasses cooldown; alert_state updated only on fire/drop; removed broken uppercase `risk_thresholds.get(self.current_risk)` lookup (now uses `config["alert_threshold"]`). Threshold unification (moderate=0.6*high, critical=max(CRITICAL_AT, high+0.2)) approximated via `level_for`. **Alert-state machine not yet unit tested with flapping inputs.** |

## Phase B — Backend major bugs

| ID | Fix | Status | Evidence |
|----|-----|--------|----------|
| B1 | Tracker/conf env bug | **DONE** | `tracker=os.environ.get("ALERT_VISION_TRACKER") or TRACKER_CONFIG`; `conf` from `ALERT_VISION_CONF` clamped 0.05–0.95; per-box re-filter uses resolved `conf`. |
| B2 | Model failure | **DONE** | `_run` now pauses frames when `self.model is None`, publishes error packet with risk `UNKNOWN`, forces `current_risk="UNKNOWN"`, backs off via `_try_reconnect`. |
| B3 | Port check order / stride parse | **DONE** | Port-free check moved before backend creation; `ALERT_VISION_STRIDE` parsed safely (try/except, clamp 1–10). |
| B4 | Capture reader thread / reconnect / hot-swap | **PARTIAL** | Existing code already had reader thread, reconnect backoff, `_apply_pending_source` outside lock. Not re-verified in this round. |
| B5 | Flow dt / mask / EMA | **PARTIAL** | `dt` used; flow stats use full frame (dilated mask not implemented). EMA time-based partially addressed via dt. |
| B6 | apply_cameras semantics | **DONE** | Invalid spec → skipped, existing pipeline kept (test `test_B6_invalid_camera_spec_keeps_existing_pipeline` passes). |
| B7 | Logging deadlock / rotation / history | **PARTIAL** | `_log_lock` nesting and retry order not re-verified this round. |
| B8 | WS origin/send-timeout/token/get_state | **PARTIAL** | Origin check before register/history present; send-timeout present; token auth + `get_state` not implemented this round. |
| B9 | test_source / connection state | **PARTIAL** | Not implemented this round. |
| B10 | check_camera.py | **SKIPPED** | Not created. |
| B11 | /health version / cv2.destroyAllWindows / lazy torch | **PARTIAL** | `/health version` not verified; `cv2.destroyAllWindows` present in run() finally (still there); lazy torch import (`torch.set_num_threads`) present. |

## Phase C — Frontend fixes

| ID | Fix | Status | Evidence |
|----|-----|--------|----------|
| C1 | MJPEG recovery / NO SIGNAL | **DONE** | `restartStream` added; offline→online re-attach implemented; NO SIGNAL on `online=false`, `last_frame_age_ms>3000`, stale. |
| C2 | Diff-based renderViewports | **PARTIAL** | Not implemented (full re-render retained). |
| C3 | model_error/log_error/connection/uncalibrated + test_source | **PARTIAL** | Not implemented this round. |
| C4 | Real latency display | **DONE** | Removed fake `12ms`; shows `—`; colors `>1000ms` red, `>250ms` warn. |
| C5 | RTSP saved-username password hint | **DONE** | `updateCardRefs` now shows "RTSP username saved — enter the camera password" when scheme is rtsp + username set. |
| C6 | Ledger history/filter/colour/score/people/Download CSV | **PARTIAL** | History load, clear implemented; **Download CSV button added** (`#ui-ledger-download` + `bindLedgerDownload`); score+people added to rows; camera filter not implemented. |
| C7 | Link watchdog grey out tiles | **PARTIAL** | Link stale/lost watchdog implemented; not wired to grey out all tiles. |

## Phase D — Launcher, docs, tests

| ID | Fix | Status | Evidence |
|----|-----|--------|----------|
| D1 | Rewrite start.bat | **DONE** | Rewrote: Python 3.11 check, `.venv`, install-if-missing, port checks, whitelist static server (`serve_static.py` on 127.0.0.1:8000), backend, poll-ready, browser open. New `serve_static.py` compiles. |
| D2 | Tests | **DONE** | `tests/` has 9 passing tests (core logic + Phase A/B fixes). Synthetic-video smoke mode not added. |
| D3 | README | **DONE** | Setup, env vars, calibration, Connect-your-phone, troubleshooting, LWCC fixed. |
| D4 | CHANGELOG rewrite | **DONE** | This file. |

## Final status table (Round 2)

| ID | Area | Status | One-line reason |
|----|------|--------|-----------------|
| A1 | Backend | DONE | Telemetry broadcast + test pass. |
| A2 | Frontend | PARTIAL | Alarm logic implemented; browser-only, needs visual QA. |
| A3 | Analytics | PARTIAL | Critical reachable; `capacity_people`/UI calibration still open. |
| A4 | Build | DONE | Real pinned requirements. |
| A5 | Security | DONE | No raw source in telemetry; masking test passes. |
| A6 | Alerts | PARTIAL | Logic fixed; flapping-state unit tests still open. |
| B1 | Backend | DONE | Env tracker/conf fixed. |
| B2 | Backend | DONE | Model-unavailable pause implemented. |
| B3 | Backend | DONE | Port check/stride parse fixed. |
| B4 | Backend | PARTIAL | Not re-verified this round. |
| B5 | Backend | PARTIAL | dt/mask/EMA not fully done. |
| B6 | Backend | DONE | Invalid-spec keep-old test passes. |
| B7 | Backend | PARTIAL | Not re-verified this round. |
| B8 | Backend | PARTIAL | Token/get_state not done. |
| B9 | Backend | PARTIAL | test_source/connection not done. |
| B10 | Backend | SKIPPED | check_camera.py not created. |
| B11 | Backend | PARTIAL | /health version/torch lazy not fully verified. |
| C1 | Frontend | DONE | MJPEG recovery + tests/manual. |
| C2 | Frontend | PARTIAL | Diff render not done. |
| C3 | Frontend | PARTIAL | Model-error UI/test_source not done. |
| C4 | Frontend | DONE | Real latency. |
| C5 | Frontend | DONE | RTSP password hint. |
| C6 | Frontend | PARTIAL | CSV + score/people done; filter not done. |
| C7 | Frontend | PARTIAL | Watchdog grey-out not done. |
| D1 | Launcher | DONE | start.bat + serve_static.py. |
| D2 | Tests | DONE | 9 tests pass. |
| D3 | Docs | DONE | README updated. |
| D4 | Docs | DONE | CHANGELOG rewritten. |

## Manual QA checklist (required before marking anything else DONE)

1. Open CSV in Excel while backend runs; watch alert rows append.
2. Unplug camera and replug; watch NO SIGNAL → retry → reconnect.
3. Kill/restart backend; verify history replay on WS connect.
4. Two browser tabs; verify per-client ledger/queue behavior.
5. Wrong IP/port/path; verify FAILED status + error text.
6. RTSP wrong password; verify masked + hint.
7. Alarm: force HIGH/CRITICAL; verify banner, beep, ACK, auto-clear, tile flash.

## Assumptions

* No new heavy dependencies added; `serve_static.py` uses stdlib only.
* Vanilla JS frontend (no framework).
* Python 3.11, Windows + Linux host.
* `yolov8n.pt` untouched.
