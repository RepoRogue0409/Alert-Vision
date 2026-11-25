# Final verification report (Phase G)

Generated after Phases A–G. Every row distinguishes implementation from proof.
No accuracy or capacity claim is made without a measurement recorded in
`docs/PERFORMANCE.md` or `docs/ACCURACY_LIMITATIONS.md`.

## 1. Test counts (exact)

| Command | Result |
|---|---|
| `python -m py_compile alert_vision.py analytics.py check_camera.py serve_static.py` | **PASS** (no output, exit 0) |
| `python -m py_compile tools/benchmark_multicam.py tools/benchmark_inference.py` | **PASS** |
| `python -m pytest -q` | **64 passed, 0 failed** (last run: 20.46 s) |
| `python tools/verify_connect.py` (synthetic camera) | **CONNECTED 2140 ms (<3000 PASS)**, **FAILED 4703 ms (<6000 PASS, reason: timeout: host did not respond)** |
| `pip install --dry-run -r requirements-dev.txt` in a clean venv | **PASS** — resolves `pytest-9.2.0` (range `>=8.3.3,<10`) |
| Generated browser-helper PowerShell parse check | **PASS** (`Parser::ParseFile`, 0 errors) |
| `python tools/benchmark_multicam.py --counts 1 2 4 6 --seconds 6` | **4/4 runs ok**, cameras_online matched requested count |
| `python tools/benchmark_inference.py --iterations 10` | **PASS** (latency only, no accuracy claim) |

## 2. Hardware used for measurements

Windows 11 Home Single Language (build 26300), Lenovo 83JC,
AMD Ryzen 7 7435HS (8 cores / 16 logical), 24,261 MB RAM, CPU inference,
`yolov8n.pt` 6,549,796 bytes (read-only).

## 3. Status table

| ID / feature | Status | Evidence / caveat |
|---|---|---|
| I-A1…I-A12 backend concurrency + WS reliability | **IMPLEMENTED / AUTOMATED TEST PASSED** | 12 tests in `tests/test_phase_a_backend.py`; full suite 64 passed |
| Alert priority over telemetry | **IMPLEMENTED / AUTOMATED TEST PASSED** | `test_A5_*`, `test_F3_stress_*` (priority queue drained first under full telemetry backlog) |
| Queue acceptance vs browser receipt | **IMPLEMENTED / AUTOMATED TEST PASSED** | `test_A6_*`; `alert_ack` sent only when the alert is displayed |
| I-B1…I-B9 analytics honesty | **IMPLEMENTED / AUTOMATED TEST PASSED** | `tests/test_phase_b_analytics.py` (10 synthetic scenarios) |
| I-B10 detection accuracy / ID switches | **NOT VERIFIED** | No labelled dataset; see `docs/ACCURACY_LIMITATIONS.md` |
| I-C1…I-C9 frontend contracts | **IMPLEMENTED / AUTOMATED TEST PASSED (source contract)** | `tests/test_phase_c_frontend.py`; no JS runtime on this machine, so browser interaction QA is separate below |
| I-D1 start.bat launcher | **MANUALLY VERIFIED + AUTOMATED TEST PASSED** | Ran `start.bat`: health OK, `/` served 200 with correct title, Chrome window opened automatically; on backend kill, ports 8000/8765/8766 freed and helper/static PID files removed; `test_D1_*` guards background helper + PID cleanup |
| I-D2 path traversal | **IMPLEMENTED / AUTOMATED TEST PASSED** | `commonpath` test with sibling `root_evil` |
| I-D3 / I-D5 WS origin + rate-limit feedback | **IMPLEMENTED / AUTOMATED TEST PASSED** | `test_D3_*`, `test_D5_*` |
| I-D4 unauthenticated loopback endpoints | **ACCEPTED LIMITATION** | Documented in `docs/SECURITY.md`; not exposed beyond 127.0.0.1 |
| I-E1/I-E2/I-E4 performance harness | **IMPLEMENTED / MEASURED** | `docs/PERFORMANCE.md` with 1/2/4/6 camera table |
| I-E3 MJPEG 12-client load | **PARTIAL — NOT VERIFIED** | 1 client measured ~22.5 fps; 12-client case not measured |
| I-F1/I-F2 coverage | **IMPLEMENTED / AUTOMATED TEST PASSED** | 64 tests; integration boots real backend + WS; dev range dry-run verified |
| I-G1/G2/G3 documentation | **IMPLEMENTED** | README, CHANGELOG, ARCHITECTURE, ACCURACY_LIMITATIONS, SECURITY, PERFORMANCE, this file |
| Live phone (DroidCam 192.168.1.106:4747) end-to-end | **NOT VERIFIED in this audit** | Synthetic camera used; saved dashboard config can override `ALERT_VISION_SOURCE` on WS reconnect (expected UX) |
| Alarm banner beep / ACK button visual behaviour | **NOT VERIFIED** | Needs a human at the dashboard; source-contract tests only |
| Cross-camera identity | **NOT CLAIMED** | `unique_ids` explicitly labelled cumulative ByteTrack IDs, not unique people |
| LWCC optional counting | **NOT WIRED / NOT VERIFIED** | No code path reads `ALERT_VISION_LWCC`; documented as reserved |
| Blocking items | **BLOCKED: none** | — |

## 4. File-by-file change summary

| File | Change |
|---|---|
| `alert_vision.py` | connection locking, worker-only capture release, alert priority queue + receipt lifecycle, bounded CSV retries, single HIGH threshold, density vote, honest forecast payload, time-based flow/velocity, full-scheme origins, rate-limit feedback |
| `analytics.py` | time-normalized EMA/rate, motion-masked + zoom-compensated divergence, `flow_velocity`, calibrated density term, `forecast_estimate` with caveat/r² |
| `script.js` | severity-ranked alert queue, dedupe, display-time `alert_ack`, incremental viewport sync, CSV quoting/formula guard, ledger counter, UNKNOWN honesty, density-calibrated label, ETA estimate wording |
| `index.html` | ETA label changed to "Linear ETA (estimate)" |
| `start.bat` | full rewrite: 7 ordered checks, background hidden browser helper, PID-scoped cleanup, no broad `taskkill` |
| `serve_static.py` | `commonpath` traversal guard, optional PID file for scoped shutdown |
| `requirements-dev.txt` | `pytest>=8.3.3,<10` (dry-run verified) |
| `tests/` | 9 → 64 tests across 11 files (unit, analytics, frontend contract, security, performance, integration, stress) |
| `tools/` | `fake_mjpeg.py`, `ws_probe.py`, `verify_connect.py`, `benchmark_inference.py`, `benchmark_multicam.py` |
| `check_camera.py` | CLI source checker (compiles; used in verification flow) |
| `docs/` | `ARCHITECTURE.md`, `ISSUE_REGISTER.md`, `ACCURACY_LIMITATIONS.md`, `SECURITY.md`, `PERFORMANCE.md`, `VERIFICATION.md` |
| `README.md`, `CHANGELOG.md` | corrected counts, env-var truth, honest supersession of stale statuses |

## 5. Outstanding manual QA checklist (human at dashboard)

1. Open `http://127.0.0.1:8000/` in a browser; confirm tiles render and Test connection reports FAILED reason for a wrong IP.
2. Force HIGH then CRITICAL; confirm a lower-priority alert does not replace the banner and that `+N queued` appears.
3. Click ACK; confirm banner clears and next queued alert surfaces.
4. Remove/add a camera; confirm only the changed tile's `<img>` src changes.
5. Export CSV with a camera label containing `,` and `=Cmd`; confirm quoting and no formula execution in Excel.
6. Unplug/replug a real DroidCam; confirm `CONNECTING` (never FAILED while connecting) → `CONNECTED`.
