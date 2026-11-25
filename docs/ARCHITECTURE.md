# Alert Vision — Architecture Map (Phase 0 audit)

> Produced by a full read-through of every project file. Verified against code,
> not against previous documentation claims.

## 1. Process / thread topology

```
start.bat
├── python serve_static.py        (threading HTTP, 127.0.0.1:8000, whitelist)
│     serves: / , /index.html , /script.js
├── powershell browser helper     (polls :8766/health → opens browser)   [see D-issue-1]
└── python -u alert_vision.py      (backend)
      ├── main(): port-free check → AlertVisionBackend(specs)
      ├── SharedSystemState
      │     ├── writer thread "crowdshield-log-writer"  (Queue 500 → CSV append)
      │     └── camera registry: dict[str, CameraPipeline]  (lock = RLock)
      ├── CameraPipeline × N (max 6), each daemon thread "cam-<id>"
      │     ├── capture handle (cv2.VideoCapture, CAP_FFMPEG, 4s open / 4s read timeout)
      │     ├── model loader thread "cam-<id>-model" (YOLO load, 30s retry)
      │     ├── own LiveAnalyzer + ByteTrack (model.track persist=True)
      │     └own latest_frame / latest_jpeg / packet (frame_lock)
      ├── AlertVisionBackend.run()  (asyncio event loop)
      │     ├── broadcast tick @100ms: build_packet → broadcast → flush alerts
      │     └── websocket server 127.0.0.1:8765 (websockets.asyncio.server.serve)
      └── FrameRelayServer (ThreadingHTTPServer, 127.0.0.1:8766)
            ├── /video?cam=ID   MJPEG (max 12 clients, 25fps sleep 0.04)
            ├── /frame.jpg      single JPEG
            └── /health         JSON {status,version,cameras,ws_clients,mjpeg_clients}
```

## 2. Data flow (camera → dashboard)

```
cv2.VideoCapture.read()                      [cam worker thread]
  → (stride gate: frame_counter % stride)
  → YOLO track() + ByteTrack                 [same thread, model loaded in parallel]
  → detections → people_now, occupied-area density
  → Farneback optical flow → turbulence/divergence (median-compensated for shake)
  → LiveAnalyzer.add() → EMA risk 0..1
  → level_for(alert_threshold, critical) = formula level
  → density_forced_level(area_sqm)          (4/m² HIGH, 6/m² CRITICAL)
  → combine_levels (never downgrade) → instant level
  → time hysteresis (escalate 1.5s / de-escalate 4s) → confirmed level
  → alert gate (new level OR 30s cooldown) + (density_forced OR score≥threshold)
        ├→ alert_queue.append (deque 20)
        ├→ state.log_alert → CSV writer queue
        └→ state.record_alert → alert_history (deque 20, replayed on WS connect)
  → annotated JPEG (frame_lock) + packet (frame_lock)
→ snapshot() per tick → build_packet() → json.dumps(_sanitize) → per-client asyncio.Queue(64)
→ script.js handlePacket → updateTile/updateFocus/showAlarm/ledger
MJPEG: grab_jpeg() → multipart stream → <img> per tile
```

## 3. Shared mutable state inventory (lock coverage)

| State | Writers | Readers | Lock |
|---|---|---|---|
| `SharedSystemState.weights/thresholds/stride` | executor (config msg) | cam threads (`config_snapshot`) | `state.lock` (RLock) |
| `SharedSystemState.cameras` | executor (`apply_cameras`) | broadcast loop, HTTP health | `state.lock` |
| `SharedSystemState.alert_history` | cam threads | WS connect replay | `state.lock` |
| CSV rows | cam threads | writer thread | `_log_lock` + Queue |
| `CameraPipeline.cap` | worker (`_apply_pending_source`, `read_frame`, `_try_reconnect`), **`stop()` from other threads** | worker, `_capture_fps` | `capture_lock` — **but `cap.read()` runs outside the lock (I-A3)** |
| `CameraPipeline.latest_frame/jpeg/packet` | worker | MJPEG threads, broadcast loop | `frame_lock` |
| `CameraPipeline.online/frame_time/reconnect_attempts` | worker, **`stop()`** | health, snapshot | **none (I-A2)** |
| `_conn_state/_conn_error/_conn_started/_resolved_source` | worker | broadcast loop (`_connection_payload`) | **none — torn reads possible (I-A4)** |
| `CameraPipeline.model/model_error` | model thread | worker, health | none (atomic ref, benign) |
| WS `clients` dict | event loop only | event loop | `asyncio.Lock` (ok) |
| per-client `incoming_queue` | event loop | event loop | none needed (ok) |

## 4. External contracts (must stay backward compatible)

* WS JSON: additive fields only — `telemetry` (type/timestamp/cameras[]/…), `alert`,
  `history`, `config_ack`, `config_error`, `test_result`, `state`.
* MJPEG: `/video?cam=`, `/frame.jpg?cam=`, `/health` on :8766.
* CSV schema: `Timestamp,CameraID,CameraLabel,RiskLevel,RiskScore,DetectionConfidence,People,ActionRequired,AlertID`.
* Frontend ↔ backend config: `{type:"config", stride, weights{4}, alert_threshold, cameras?}`.
* Max 6 cameras; ports 8765/8766/8000; `yolov8n.pt` read-only.

## 5. Verified-vs-claims check (README/CHANGELOG vs code)

| Claim | Verdict |
|---|---|
| Credentials never logged/broadcast, masked `***@` | **TRUE** (`_source_display`, telemetry `source_value_masked`, test A5 passes; localStorage excludes password) |
| One CAP_FFMPEG attempt, 4s timeouts, no hanging fallback | **TRUE** (`_open_capture`) |
| Reconnect backoff 2→4→8…30 reset on success | **TRUE** (`_reconnect_delay`, attempts capped 6 → 30s) |
| Model loads in parallel; raw frames relay while loading (`UNKNOWN`) | **TRUE** (`_run` + `_load_model` thread) |
| DroidCam `/video → /mjpegfeed?640x480 → /mjpegfeed` alternates | **TRUE** (`_candidate_sources`, port 4747) |
| `test_source` → `test_result`, ≤5s, off event loop | **TRUE** (executor + `wait_for(5.0)`) |
| Emergency alerts outrank telemetry | **FALSE — alerts share the telemetry queue and are dropped when full (I-A5)** |
| Queue acceptance ≠ browser receipt distinguished | **FALSE — `acknowledge_alert` fires on queue acceptance only, undocumented (I-A6)** |
| Density never presented as physical measurement | **PARTIAL — `density_per_sqm` shows `0.00` when uncalibrated instead of "not calibrated" (I-C4)** |
| Time-to-Critical honest | **PARTIAL — linear extrapolation labelled "ETA" without caveat (I-B6)** |
| No broad exception swallowing | **FALSE — `_handle_connection` has bare `except Exception: pass` (I-A7)** |
| start.bat browser helper opens browser, stops cleanly | **FALSE — user commit `13e891c` runs the helper synchronously *before* the backend boots and the generated PS1 is syntactically invalid; final cleanup no longer stops the static server (I-D1)** |
| Frontend event counter ("0 events") | **FALSE — `ledgerEventCount` is never incremented (I-C6)** |
