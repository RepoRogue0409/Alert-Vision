# Security posture (Phase D)

## Verified controls

| Control | Evidence |
|---|---|
| All services bind to `127.0.0.1` only | `HOST = "127.0.0.1"` in `alert_vision.py` and `serve_static.py` |
| Camera credentials never logged or broadcast | `_source_display()` masks `user:pass` as `***@`; regression test A5 (old suite) |
| Static server serves a strict whitelist (`/`, `/index.html`, `/script.js`) | `serve_static.py:do_GET` |
| Path traversal / sibling-prefix escape rejected by whole-component `commonpath` | `tests/test_phase_d_security.py::test_D2_*` |
| WS Origin checked before client registration; bare hosts get `http://`, full schemes preserved | `tests/test_phase_d_security.py::test_D3_*` |
| Excess WS message rate returns a `config_error` instead of silently dropping | `tests/test_phase_d_security.py::test_D5_*` |
| Launcher aborts on pip/import failure and stops only its own child processes by PID | `tests/test_phase_d_security.py::test_D1_*` |

## Explicit limitations (NOT secured)

* **The MJPEG relay (`/video`, `/frame.jpg`) and `/health` are unauthenticated.**
  They are safe only because the listener is bound to loopback. Anyone who can
  run code as the local user can already read the stream; do not expose port
  8766 through a tunnel or reverse proxy without adding authentication.
* **The WebSocket server has no token authentication.** It relies on loopback
  binding plus an Origin allow-list. A local process can connect with no Origin
  header; this is intentional for diagnostic probes (`tools/ws_probe.py`).
* **No TLS.** All traffic is plain HTTP/WS on loopback. Do not bind to `0.0.0.0`.
* **Browser localStorage holds camera host/path but never passwords**
  (`script.js:savePersisted` strips `password`). It is readable by any script
  running on the dashboard origin, so keep the origin limited to localhost.
* **CSV export is plain text.** Formula-injection quoting is applied by the
  frontend (`csvCell`), and the backend uses `csv.writer`; still treat the file
  as untrusted input when opening in a spreadsheet.

## What was NOT changed

* No public WebSocket/MJPEG/CSV field was renamed. Additive fields only:
  `connection`, `model_error`, `log_error`, `track_ids_seen`, `forecast_estimate`,
  `density_calibrated`, `unique_ids_meaning`.
* `yolov8n.pt` remains read-only; LWCC remains optional and lazily imported.
