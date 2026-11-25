# Alert Vision — Phase 0 Issue Register

> Every issue found during the full code read-through. Status changes as phases
> complete: OPEN → FIXED (with test) / ACCEPTED (documented limitation).
> Severity: S1 = data loss / crash / false safety signal, S2 = wrong behavior,
> S3 = honesty/UX, S4 = cleanup.

| ID | Sev | Area | Issue (file:line evidence) | Phase | Status |
|----|-----|------|---------------------------|-------|--------|
| I-A1 | S2 | concurrency | `apply_cameras` calls `pipeline.stop()` (joins thread up to 5s) **while holding `state.lock`** → broadcast loop stalls up to 5s/camera during camera removal (`alert_vision.py:1334-1338`) | A | FIXED |
| I-A2 | S2 | concurrency | `CameraPipeline.stop()` writes `self.online=False` and releases `cap` from the *caller* thread while worker may be mid-`cap.read()`; `online`/`frame_time` shared with no lock (`alert_vision.py:377-388`) | A | FIXED |
| I-A3 | S2 | concurrency | `read_frame` grabs `cap` under lock but calls `cap.read()` **outside** the lock (`alert_vision.py:644-661`) → concurrent `release()` from `stop()`/hot-swap can free the handle mid-read | A | FIXED |
| I-A4 | S3 | concurrency | `_conn_state/_conn_error/_conn_started/_resolved_source` mutated by worker, read by broadcast loop with no lock → torn `connection` payload (state from one attempt, error from another) | A | FIXED |
| I-A5 | S1 | WS reliability | **Alerts share the per-client telemetry queue; `broadcast()` drops messages when full — including `type:"alert"`** → emergency alert can be lost behind slow telemetry drain (`alert_vision.py:1475-1489`) | A | FIXED |
| I-A6 | S1 | WS reliability | `acknowledge_alert()` pops the alert as soon as it is *queued* for ≥1 client; queue-acceptance is indistinguishable from browser receipt, and with 0 clients the alert is never acked but also never re-signalled distinctly | A | FIXED |
| I-A7 | S2 | logging | `_handle_connection` ends in bare `except Exception: pass` (`alert_vision.py:1794-1795`) — swallows handler faults wholesale | A | FIXED |
| I-A8 | S3 | logging | `_log_writer` retry loop re-queues a failing row forever every 0.5s with no cap and never sets `log_error` → silent infinite retry, UI never shows log failure | A | FIXED |
| I-A9 | S4 | WS | `send_raw`/`send_history` use `put_nowait` without full-check → `QueueFull` raised into handler (caught by I-A7's blanket except, killing the client silently) | A | FIXED |
| I-A10 | S4 | config | `update_config` stride-change log compares new value against already-updated `self.stride` → message never fires for actual changes (`alert_vision.py:1249-1251`) | A | FIXED |
| I-A11 | S2 | camera | `_run` offline path publishes `_offline_packet()` **before** `_try_reconnect()` sets `connection.state` for that attempt → one tick of stale `failed`/old error; also `time.sleep(0.1)` fixed regardless of backoff | A | FIXED |
| I-A12 | S3 | camera | `_apply_pending_source` failure leaves `_last_open_error` from a *previous* source (only cleared on success) → wrong reason shown after hot-swap to a new bad source | A | FIXED |
| I-B1 | S2 | CV | `LiveAnalyzer.add` `rate_of_change` term = raw per-frame delta of normalized density → **stride/FPS dependent**: same real-world crowd rise scores ~stride× higher at stride 10 (`analytics.py:160-161,178`) | B | FIXED |
| I-B2 | S2 | CV | EMA (`LEVELING_ALPHA=0.25` per sample) applied per *frame* not per *second* → smoothing strength varies with FPS/stride (`analytics.py:165,190`) | B | FIXED |
| I-B3 | S3 | CV | `compute_flow_signal` `dt` accepted but explicitly unused; `velocity` = mean px/frame with no dt normalization → not comparable across FPS | B | FIXED |
| I-B4 | S2 | CV | Farneback divergence is frame-wide mean gradient — dominated by static background pixels (no motion mask); camera zoom (not just pan) not compensated (median subtraction only removes translation) | B | FIXED |
| I-B5 | S3 | CV | `density` (box-pixel occupancy fraction) is unbounded-by-calibration and published as `density` alongside `density_per_sqm`; risk `den` uses `_demean_norm(count, mp-1, mp+4)` i.e. *count*, not density → density term ignores `area_sqm` entirely | B | FIXED |
| I-B6 | S3 | honesty | `forecast_seconds_to_critical` = linear fit over ≤50 EMA samples published as `forecast_eta_s` / "ETA to critical" with no uncertainty or "estimate" caveat; `np.polyfit` on identical timestamps → `RankWarning` risk | B | FIXED |
| I-B7 | S4 | CV | `density_history_vote` defined but never called anywhere (dead code) | B | FIXED |
| I-B8 | S3 | CV | ByteTrack IDs: `seen_ids` counts *cumulative* track IDs (cleared at >512) → `unique_ids` grows monotonically with re-IDs; must never be read as "unique people" | B | FIXED |
| I-B9 | S2 | CV | Alert gate requires `risk_score >= alert_threshold` even when formula level is HIGH via `level_for(risk, alert_threshold)` — consistent today, but density-forced HIGH with low score bypasses (by design, `density_forced`) while **formula-HIGH at `risk_thresholds.high` ≠ `alert_threshold` would never alert** if thresholds diverge | B | FIXED |
| I-B10 | S3 | CV | YOLO input: frames downscaled to 640px width only if wider — stride-10 + 640px + `conf=0.25` documented but no measurement of ID-switch rate or small-person misses | B | ACCEPTED (NOT VERIFIED; latency harness added) |
| I-C1 | S2 | frontend | Single alarm banner: a new **lower**-severity alert overwrites an active CRITICAL banner (`showAlarm` has no rank guard vs current `alarmState`) — violates "no lower-priority hiding" | C | FIXED |
| I-C2 | S2 | frontend | `showAlarm` fires on **every telemetry packet** where `cam.alert===true` (10 Hz) → re-beep/re-flash storm until backend acks; also duplicate handling with `type:"alert"` packets | C | FIXED |
| I-C3 | S4 | frontend | `renderViewports()` wipes `innerHTML` and re-attaches all `<img>` streams on *any* camera list change → full MJPEG re-handshake for all tiles (C2 from old register) | C | FIXED |
| I-C4 | S3 | honesty | `infoDensityPsm` shows `0.00` people/m² when camera is uncalibrated (backend sends `density_per_sqm: 0.0`) — reads as a measured zero | C | FIXED |
| I-C5 | S3 | frontend | CSV download joins raw cells with commas: camera label or action containing `,`/`"` corrupts the CSV; leading `=`/`+`/`-` in a label = formula injection when opened in Excel | C | FIXED |
| I-C6 | S4 | frontend | `ledgerEventCount` never incremented; ledger header always shows "0 events" (`script.js:1918` only resets it) | C | FIXED |
| I-C7 | S3 | frontend | `adoptBackendCameras` rewrites `state.cameras` from masked source — RTSP creds already stripped (fine) but every adopt triggers full `renderViewports()`; combined with C3 | C | FIXED |
| I-C8 | S4 | frontend | `handleTelemetry` early-`return`s after first adopt, dropping that packet's tile updates (next tick, 100ms, refills — cosmetic) | C | FIXED |
| I-C9 | S3 | frontend | Focus panel `updateFocus` shows headcount/risk from a camera even when `risk_level === "UNKNOWN"` (model loading) — badge says MODEL LOADING but focus panel shows "SAFE" | C | FIXED |
| I-D1 | S1 | launcher | **User commit `13e891c` broke `start.bat`**: browser helper runs synchronously *before* backend start (so health poll always times out or, worse, blocks boot); generated PS1 uses `if $i -ge 90 (` … `)` — invalid PowerShell (needs `{}`) so helper dies instantly; `taskkill /f /im powershell.exe` kills *all* PowerShell on the machine; final cleanup no longer stops the static server (only `pause`); no `taskkill` for `python` child | D | FIXED |
| I-D2 | S2 | security | `serve_static._safe_path` `candidate.startswith(ROOT)` prefix check without separator → sibling dir `ROOT + "evil"` would pass; whitelist in `do_GET` currently saves it, but `_safe_path` is also called with fallback to `index.html` — defense in depth broken | D | FIXED |
| I-D3 | S3 | security | WS Origin check treats *missing* Origin as allowed (needed for non-browser probes) but `ALERT_VISION_ALLOWED_ORIGINS` env prepends `http://` blindly → `https://` origins impossible | D | FIXED |
| I-D4 | S3 | security | MJPEG `/video` + `/frame.jpg` + `/health` unauthenticated on 127.0.0.1 (acceptable for localhost-only, but must be documented as a limitation, not claimed secure) | D | ACCEPTED (documented localhost-only limitation) |
| I-D5 | S4 | security | `MAX_WS_MESSAGES_PER_SEC` silently drops excess messages (no `config_error` feedback) — config save can be lost with no user-visible reason | D | FIXED |
| I-E1 | S2 | perf | `build_packet` → `snapshot()` copies every camera dict every 100ms including `tracks` list; JSON `_sanitize` deep-copies again — fine at 6 cams, needs measurement not assumption | E | OPEN |
| I-E2 | S2 | perf | Each camera = 1 thread + 1 model loader + **separate YOLO instance** (6× yolov8n weights in RAM) — needs measured RSS/CPU at 1/2/4/6 cams | E | OPEN |
| I-E3 | S3 | perf | MJPEG `time.sleep(0.04)` fixed 25fps per client regardless of camera FPS; up to 12 clients × 6 cams unmeasured | E | OPEN |
| I-E4 | S3 | perf | No benchmark harness exists at all — all perf claims would be unmeasured | E | OPEN |
| I-F1 | S2 | tests | No test covers: alert queue priority, hysteresis state machine, hot-swap, reconnect backoff, CSV escaping, WS rate limit, `_validate_source` edge cases | F | OPEN |
| I-F2 | S4 | tests | `requirements-dev.txt` pins `pytest==8.3.3` but venv actually has 9.2.0 (installed via `--target`) — pin drift | F | OPEN |
| I-G1 | S4 | docs | README says "All 5 tests must pass" + "pytest — currently 9 tests" (stale counts), claims "backoff 2s→4s…30s" for *link* watchdog that is actually fixed 2s, LWCC env var claimed but no code path reads `ALERT_VISION_LWCC` | G | OPEN |
| I-G2 | S4 | docs | CHANGELOG Round-2 table has PARTIAL rows (B4/B5/B7/B8, A2/A3/A6, C2/C6) that later commits actually fixed — statuses stale in both directions | G | OPEN |
| I-G3 | S4 | docs | No architecture/benchmark/security/limitation reports existed before this audit | G | OPEN |

## Data-flow trace (camera → dashboard) — verified

1. `start.bat` → `serve_static.py` (:8000) + `alert_vision.py` (WS :8765, MJPEG :8766).
2. Dashboard loads → `localStorage` cameras → WS connect → sends saved config (`sendConfig(true)`), backend `apply_cameras` spawns `CameraPipeline` threads.
3. Worker: open capture (4s CAP_FFMPEG, alternates for 4747) in parallel with YOLO load thread → `UNKNOWN` packets + raw JPEG relay until model ready.
4. Per processed frame: YOLO+ByteTrack → flow → `LiveAnalyzer` → level + hysteresis → optional alert (queue + CSV + history).
5. Every 100ms: `build_packet` → sanitized JSON → per-client asyncio queue → `handlePacket` → tiles/focus/bars/alarm/ledger.
6. MJPEG: `<img src=:8766/video?cam=ID>` with watchdog + exponential retry; re-attach on offline→online.
