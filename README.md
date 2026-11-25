# Alert Vision — Multi-Camera Crowd Safety Dashboard

**Alert Vision** is a multi-camera crowd-safety telemetry engine for Windows + Python 3.11.
It combines a threaded camera shell, WebSocket dashboard, and MJPEG stream with tested
CrowdShield analytics for risk scoring, forecasting, and alerting.

## Quick start

1. **Install deps**

   ```powershell
   pip install -r requirements.txt
   # Optional: pip install -r requirements-optional.txt  # LWCC only
   ```

2. **Start the stack**

   ```powershell
   .\start.bat
   ```

   This launches:
   * an HTTP server on port **8000** (dashboard at `http://localhost:8000`)
   * the Alert Vision backend on websocket **8765** and MJPEG **8766**

3. **Connect a phone / CCTV camera**

   Open the dashboard at `http://localhost:8000`. Click **"+ ADD CAMERA CHANNEL"**.

   Choose a preset or enter the camera details manually:

   | Preset               | Scheme | Default port | Default path          |
   |----------------------|--------|--------------|-----------------------|
   | DroidCam (phone)     | http   | **4747**     | `/video`              |
   | IP Webcam (phone)    | http   | **8080**     | `/video`              |
   | Generic MJPEG        | http   | **80**       | `/video`              |
   | Generic RTSP         | rtsp   | **554**      | `/stream1`            |
   | Hikvision            | rtsp   | **554**      | `/Streaming/Channels/101` |
   | Dahua                | rtsp   | **554**      | `/cam/realmonitor?channel=1&subtype=0` |
   | TP-Link Tapo         | rtsp   | **554**      | `/stream1`            |
   | Custom URL           | http   | **80**       | `/`                   |

   - **IP/Host**: enter the phone's IP address (must be on the same Wi‑Fi, or a Tailscale `100.x.x.x` IP).
   - **Port**: 1–65535. Tailscale IPs work remotely if both PC and phone are on the same tailnet.
   - **Path**: stream path (for RTSP, e.g. `/stream1`).
   - **Username / Password**: only for RTSP streams; credentials are **never** logged or broadcast; they are masked in URL display and removed from `localStorage`.

   Click **CONNECT / APPLY**. The status chip will show `CONNECTING` → `CONNECTED` (or `FAILED` if no frames).

   - Pasted full URLs (e.g. `http://192.168.1.50:8080/video`) are auto‑split into scheme/host/path/port fields.
   - Duplicate sources (same stream on multiple channels) are rejected.
   - Maximum **6** camera channels.

## Troubleshooting

| Symptom                              | Likely cause                              | Fix                                                      |
|--------------------------------------|-------------------------------------------|----------------------------------------------------------|
| NO SIGNAL / NO SYNC                  | Phone not on same Wi‑Fi; wrong port/path  | Verify phone and PC are on same LAN, or use a Tailscale IP (`100.x.x.x`). Check the URL path. |
| LINK LOST — RECONNECTING             | Backend WS connection dropped             | The watchdog will auto‑reconnect with exponential backoff (2s → 4s → 8s … 30s). |
| LINK STALE — NO TELEMETRY            | No camera frames received >3s ago         | Check camera feed; ensure the phone is streaming, not in standby. |
| ALARM BANNER not appearing           | Risk level stays SAFE/MODERATE            | This is normal when density is below the HIGH (4/m²) threshold. |
| CONFIG REJECTED — CHECK INPUT        | Invalid weight or threshold value         | Values must be between 0 and 1. Use the sliders.         |
| Duplicate source error               | Two channels using the same stream        | Remove one channel or change the source URL.             |
| Password shown in URL or logs        | Credential leak                          | Passwords are never persisted or sent in WS messages; URL masking removes `user:pass` → `***`. |
| Camera status stays IDLE             | Backend hasn't adopted the camera yet     | Wait a few seconds; the backend auto‑adopts on first telemetry packet. |

## Configuration

* Risk weights (density / rate_of_change / convergence / turbulence) and the alert threshold
  can be adjusted via the UI or sent from the backend as a `config` JSON message.
* Camera configurations (preset, host, port, path, username) are persisted in
  `localStorage` under key `alertVision.cameraConfig.v1` — passwords are **excluded**.
* On reconnect, the saved config is restored (without passwords); the UI will mark
  non‑webcam cameras as `CONNECTING` until the backend confirms they are online.

## Development

* `tests/test_core_logic.py` — synthetic-video smoke test; runs `from analytics import ...`
  against the real module. All 5 tests must pass before commit.
* `tests/test_phase_a_fixes.py`, `tests/test_phase_b_fixes.py` — Round-2 smoke tests for
  telemetry broadcast, criticality, credential masking, apply_cameras.
* `python -m py_compile alert_vision.py analytics.py` — must succeed.
* `pytest` — currently 9 tests; more may be added in `tests/`.
* Do **not** modify `yolov8n.pt`; it is a read‑only model weight.
* LWCC (`Lightweight Crowd Counting` library) is optional, off by default, and lazy‑imported;
  enable with `ALERT_VISION_LWCC=1`. It must never crash the process if missing.

## Environment variables

| Var | Purpose |
|-----|---------|
| `ALERT_VISION_SOURCE` / `ALERT_VISION_SOURCES` | Comma-separated camera sources (e.g. `0,http://192.168.1.50:8080/video`). |
| `ALERT_VISION_STRIDE` | Inference stride (default 1, clamped 1–10). |
| `ALERT_VISION_CONF` | Detection confidence (default 0.25, clamped 0.05–0.95). |
| `ALERT_VISION_TRACKER` | ByteTrack tracker override. |
| `ALERT_VISION_MODEL` | YOLO model path (default `yolov8n.pt`). |
| `ALERT_VISION_LWCC` | `1` enables optional LWCC counting (lazy import, never crashes). |
| `ALERT_VISION_ALLOWED_ORIGINS` | Comma-separated allowed browser origins. |

## Calibration guide

Per-camera density calibration:
* `area_sqm` — known crowd area in square metres (people/m² density).
* OR `height_m` + `tilt_deg` + `hfov` / `vfov` — camera geometry, from which ground area is estimated.
* OR `min_people` — capacity hint (default 3).

Density gates: 4 people/m² → HIGH, 6 people/m² → CRITICAL (density‑forced levels).

## License

MIT — see the original CrowdShield reference for attribution.