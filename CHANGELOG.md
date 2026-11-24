# Alert Vision — Change Log

## Phase 5: L1 launcher hardening + requirements + tests + CHANGELOG (current)

### Added
- `requirements.txt` — pinned core runtime dependencies (websockets, opencv-python, numpy). No heavy deps; LWCC optional, off by default, lazy-imported.
- `requirements-optional.txt` — optional extensions (lwcc, streamlit, ultralytics, torch, altair, pandas).
- `.gitignore` — expanded to cover `__pycache__/`, `*.pt` (model weights), `crowdshield_alert_log.csv`, `.env`, `requirements-optional.txt`, `Thumbs.db`.
- `tests/` directory with `test_core_logic.py` — synthetic-video smoke test that imports the real `analytics` module and exercises `compute_flow_signal`, `level_for`, `density_forced_level`, `LiveAnalyzer`, `combine_levels`. All tests pass with `from analytics import ...`.
- `tests/test_core_logic.py` — 5 tests: flow signal tuple, level_for formula, density forced thresholds, LiveAnalyzer init, combine_levels never-downgrade.

### Changed
- `start.bat` — L1 launcher hardening: explicit `python -u` unbuffered output, working‑directory guard, environment validation, single‑instance guard, improved error messages, pause on exit.
- `alert_vision.py` — ensured `compute_flow_signal`, `LiveAnalyzer.last_terms`, `density_forced_level`, `combine_levels`, `forecast_eta_s`, `risk_level_formula`, `risk_level_density`, `area_sqm`, `min_people`, `last_frame_age_ms`, `log_error`, `model_error` are all present in the telemetry packet output; removed `nest_asyncio`; plain `asyncio.run` used.
- `analytics.py` — pure CrowdShield risk logic (no Streamlit, no cv2-UI deps); `cv2` imported lazily inside `compute_flow_signal`; `LiveAnalyzer.last_terms` and `LiveAnalyzer.reset()` exposed; all analytics items UI‑free.
- `script.js` — Phase 4 fixes: F1 NO SIGNAL overlay with exponential backoff retry; F2 link stale/lost detection (EMA latency, tick watchdog); F3 alarm banner with ETA and ACK; F4 config ack/error from backend; F5 per-camera density/rate/convergence/turbulence bars + density-per-sqm + formula/density gate + ETA info panel; F6 ledger header + clear button; F7 info-card grid layout; F8 accessibility (focus-visible, skip-link, reduced-motion media query).
- `index.html` — Phase 4 CSS: `.no-signal` overlay, `.alarm-banner` with pulse animation, `.info-row` bar breakdown, `.link-status.stale`, `.ledger-clear`, `.skip-link`, alarm cam-tile class, keyboard focus styles.
- `script.js` — Phase 3 fixes: phone/CCTV presets (DroidCam 4747, IP Webcam 8080, Generic MJPEG 80, Generic RTSP 554/stream1, Hikvision, Dahua, Tapo, Custom); live URL generation; Enter/Connect/blur connect with ≥800ms debounce; IPv4/hostname + port 1–65535 validation incl. Tailscale 100.64.0.0/10 and private LAN auto-hint; auto-split pasted full URL; RTSP username/password URL-encoded; status chip IDLE/CONNECTING/CONNECTED/FAILED + same‑WiFi/Tailscale hint; localStorage persistence (camera config, not password) + auto-reconnect; duplicate-source rejection; preset-matching placeholders.

### Fixed bugs
- B1–B17 (Phase 1): time-based hysteresis alert state machine, per-camera alert queue + `alert` broadcast + history replay, background CSV writer thread, offline NO SIGNAL/`last_frame_age_ms`, MJPEG hardening/`/health`/12-client cap/shared JPEG, source allowlist + credential masking + WS origin/size/rate limits, source hot-swap outside lock + config validation + dup-source rejection, single threshold model, log migration/rotation/history, per-client queue+timeout+`allow_nan=False`, model retry/env, removed `nest_asyncio`, port-busy check, graceful shutdown.
- F1–F8 (Phase 4): NO SIGNAL overlay instead of dead frame, real latency from server timestamps (not fake constant), alarm banner with pulse and ETA, link stale/lost states with visual distinction, info signal bars (density/rate/convergence/turbulence), density-per-sqm + formula/density gate + ETA in info panel, ledger header with clear, accessibility improvements (focus-visible, skip-link, reduced-motion).

### Pending
- Phase 5: full README "Connect your phone" section + troubleshooting; final CHANGELOG audit.