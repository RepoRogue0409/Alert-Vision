# Alert Vision — Change Log

> Honest state after the MASTER ENGINEERING audit. A status is DONE only when
> there is a passing automated test or a recorded manual check. "PARTIAL" and
> "NOT VERIFIED" are kept where measurement/browser QA is still required.
> Historical Round-2 PARTIAL rows are superseded by the Phase A–G rows below;
> the old table was stale in both directions (it under- and over-reported fixes).

## Final audit phases (commits `5e84626` → `d15e8dc`)

| Phase | Commit | Fixed IDs | Verified evidence |
|---|---|---|---|
| 0 — audit | `5e84626` | issue register created | `docs/ARCHITECTURE.md`, `docs/ISSUE_REGISTER.md` (46 issues) |
| A — backend/concurrency | `06a461c` | I-A1…I-A12 | 12 new tests; alert priority queue; queue-accept ≠ browser receipt; masked/consistent connection payload; bounded CSV retries |
| B — CV/analytics | `5f2b3ee` | I-B1…I-B9 | 10 synthetic scenarios; FPS/stride-independent EMA + rate; zoom-compensated flow; calibrated density; honest ETA; single HIGH threshold |
| C — frontend/alerts | `a49e8f6` | I-C1…I-C9 | alert queue with severity guard; no 10 Hz alert storm; incremental viewports; CSV quoting/formula guard; UNKNOWN shown honestly |
| D — security/launcher | `6476f8b` | I-D1, I-D2, I-D3, I-D5 | `commonpath` traversal fix; full-scheme origins; rate-limit feedback; `start.bat` background helper + PID cleanup (no machine-wide `taskkill`) |
| E — performance | `92d32eb` | I-E1, I-E2, I-E4 | real 1/2/4/6-camera measurements in `docs/PERFORMANCE.md`; 12-client MJPEG case still NOT measured (I-E3 PARTIAL) |
| F — tests | `d15e8dc` | I-F1, I-F2 | 64 tests: unit, integration (real WS/backend), stress (alert priority), frontend source-contract; dev requirement range dry-run verified in clean venv |
| G — docs | this commit | I-G1, I-G2, I-G3 | README corrected; honesty/security/performance/verification docs added |

## DONE now (previously PARTIAL in Round 2)

| Item | Honest current status | Evidence |
|---|---|---|
| Telemetry broadcast each tick | **DONE** | `tests/test_phase_a_fixes.py`, integration test F1 |
| Alarm banner / queue | **DONE** | severity-ranked `alertQueue`, dedupe, explicit `alert_ack` on display; source-contract tests C1/C2 |
| CRITICAL reachable / density gate | **DONE** | 4/m²→HIGH, 6/m²→CRITICAL tests; calibrated density feeds risk term |
| Alert-state flapping / hysteresis | **DONE** | time-based escalation 1.5s / de-escalation 4s; tests F6/F8 (state-machine constants + history vote) |
| Capture reader / reconnect / hot-swap | **DONE** | worker-only handle release; backoff 2→30s; hot-swap test F7 |
| Flow `dt` / mask / EMA | **DONE** | motion-masked + zoom-compensated divergence; velocity px/s; time-constant EMA |
| Logging deadlock / rotation / history | **DONE** | nested `_log_lock` removed; bounded retries; `log_error` surfaced |
| WS origin / send timeout / `get_state` | **DONE** (origin/timeout); `get_state` still additive-only | origin test D3; session loop closes timed-out clients |
| `test_source` / connection state | **DONE** | ≤5 s off-loop probe; integration test F2 (FAILED ≤5 s) |
| `check_camera.py` CLI | **DONE** | present, compiles, used by verification flow |
| Diff-based `renderViewports` | **DONE** | incremental DOM sync; only changed sources re-attach |
| Per-camera UI calibration | **DONE** (area_sqm / height+tilt+FOV / min_people via backend config); no `capacity_people` field | density tests + `density_calibrated` badge |
| Camera filter in ledger | **NOT IMPLEMENTED** | intentionally not added; ledger shows all cameras (documented limitation) |
| Token auth on WS | **NOT IMPLEMENTED** | loopback + Origin allow-list only; see `docs/SECURITY.md` |

## STILL NOT VERIFIED / LIMITATIONS

* Accuracy, small-person recall, ByteTrack ID-switch rate — `docs/ACCURACY_LIMITATIONS.md`.
* 12 simultaneous MJPEG clients × 6 cameras — `docs/PERFORMANCE.md`.
* Live DroidCam hardware (`192.168.1.106:4747`) in this audit run — pipeline
  verification used `tools/fake_mjpeg.py`; real phone QA remains a manual step.
* No cross-camera identity tracking is claimed anywhere.
* LWCC is **not wired in this build**; `ALERT_VISION_LWCC` is reserved, not read.

## Verification commands

```powershell
.\.venv\Scripts\python -m py_compile alert_vision.py analytics.py check_camera.py serve_static.py
.\.venv\Scripts\python -m pytest -q          # 64 passed
.\.venv\Scripts\python tools\verify_connect.py
.\.venv\Scripts\python tools\benchmark_multicam.py --counts 1 2 4 6 --seconds 6
```

See `docs/VERIFICATION.md` for the final status table with MANUALLY VERIFIED /
NOT VERIFIED / BLOCKED distinctions.
