# Alert Vision: A Reliable Multi-Camera Crowd-Safety Telemetry Architecture

**Draft research paper — v1.0, 25 November 2026**  
**Author:** [Author name], [Affiliation]  
**Artifact:** Alert Vision, Windows + Python 3.11, commit `93b7481`

> This paper describes the system as implemented and verified. All numbers come from
> the project's automated tests and benchmark logs. No accuracy or capacity claim is
> made beyond the recorded measurements.

## Abstract

Small public-event operators often need a low-cost way to observe crowd risk across
several cameras without deploying a full computer-vision platform. We present Alert
Vision, a localhost-only multi-camera telemetry engine that combines threaded video
capture, YOLOv8 person detection with ByteTrack, optical-flow-derived heuristic
signals, a WebSocket dashboard, and an MJPEG relay. The engineering contribution is
not a new detector but a set of reliability properties: bounded connection attempts,
non-blocking model loading, priority-ordered emergency alerts, an explicit distinction
between *queue acceptance* and *browser receipt*, and a launcher that fails closed
instead of booting a partially installed stack. The system was audited in eight phases;
64 automated tests pass. On a synthetic camera, first connectivity was observed at
2641 ms and an unreachable source produced a readable failure at 4641 ms. The paper
documents the architecture, the failure-mode analysis, and the measured behaviour,
while explicitly noting what was not validated (detection accuracy, real-phone QA,
and twelve simultaneous MJPEG clients).

**Keywords:** crowd safety, multi-camera systems, WebSocket telemetry, MJPEG,
reliability engineering, real-time analytics.

---

## 1. Introduction

Crowd-safety dashboards are frequently built as demonstrations: a detector, a web
page, and a happy-path video stream. In operation, however, the dominant problems are
failures of *plumbing*: a camera that never connects, a slow browser client that
causes an alert to be dropped, a launcher that starts the web server even though
`pip install` failed, or a log writer that retries forever without telling the user.

Alert Vision was therefore engineered backwards from failure modes. The project
keeps a simple CrowdShield-derived risk model in a pure module (`analytics.py`) and
concentrates concurrency and delivery correctness in the backend (`alert_vision.py`).
This paper reports the resulting architecture and the evidence collected during a
full code audit and remediation cycle.

The research questions are:

* **RQ1.** How can a six-camera pipeline keep connection diagnostics consistent
  without blocking telemetry?
* **RQ2.** How can emergency alerts be guaranteed priority over routine telemetry
  while preserving an honest delivery semantic?
* **RQ3.** What are the measured resource costs of the design on commodity CPU
  hardware?

## 2. System model

### 2.1 Process and thread topology

The stack consists of three processes:

1. `serve_static.py` — a stdlib `ThreadingHTTPServer` bound to `127.0.0.1:8000`,
   serving a three-file whitelist (`/`, `/index.html`, `/script.js`).
2. `alert_vision.py` — the backend, containing:
   * an `asyncio` event loop on `127.0.0.1:8765` for WebSocket clients;
   * a `ThreadingHTTPServer` on `127.0.0.1:8766` for MJPEG, a single frame endpoint,
     and `/health`;
   * up to six `CameraPipeline` worker threads;
   * one model-loader thread per camera;
   * a CSV writer thread with bounded retries.
3. `start.bat` — a launcher that starts the static server, launches a hidden
   PowerShell health-polling helper, and then runs the backend in the foreground.

Each camera owns its `cv2.VideoCapture`, YOLO instance, ByteTrack state, optical-flow
buffers, and latest-frame/JPEG snapshot. The only cross-camera shared state is the
configuration registry, alert history, and the CSV writer queue.

### 2.2 Data flow

```text
capture.read()
  → stride gate
  → YOLO track (person class) + ByteTrack
  → occupancy / calibrated density
  → Farneback flow → turbulence, divergence, velocity
  → LiveAnalyzer → EMA risk
  → formula level + density-forced level → hysteresis → confirmed level
  → alert gate → priority queue + CSV + replay history
  → 100 ms telemetry packet → per-client asyncio queues → dashboard
```

MJPEG is a parallel path: the worker encodes the latest annotated frame, and the relay
thread serves it to `<img>` elements with a watchdog and bounded retry backoff.

## 3. Reliability design

### 3.1 Connection diagnostics (RQ1)

Earlier implementations allowed the worker thread to mutate connection state while the
broadcast loop read it without synchronisation. We introduced a dedicated `_conn_lock`
so that `connection = {state, error, elapsed_ms, resolved_url_masked}` is published as
one consistent snapshot. The capture handle is released only by the worker itself;
`stop()` joins the worker before touching the handle, and skips release if the join
times out. This avoids undefined behaviour from releasing a capture handle during a
concurrent `read()`.

Stream opening uses one `CAP_FFMPEG` attempt with 4000 ms timeouts. For DroidCam URLs
on port 4747, alternates (`/video`, `/mjpegfeed?640x480`, `/mjpegfeed`) are tried only
if a 600 ms TCP probe shows the host is alive; otherwise the pipeline transitions to
`FAILED` quickly with a readable reason. Reconnect delay grows 2 → 4 → 8 → 16 → 30 s
and resets on success.

### 3.2 Alert priority and honest delivery (RQ2)

The original broadcaster used one 64-slot queue per client for both telemetry and
alerts. A slow browser could therefore fill the queue and cause an emergency alert to
be dropped. The revised design uses two queues per client:

* `incoming_queue` (64 slots) for routine telemetry;
* `alert_queue` (16 slots) for `type:"alert"` messages.

The session loop drains `alert_queue` first. Telemetry is droppable; an alert is only
dropped if the *alert* queue itself is full.

Delivery semantics are explicit:

| Stage | Meaning | Code event |
|---|---|---|
| queued | alert created in the camera pipeline | `_queued_at` |
| queue-accepted | entered at least one client queue | `_dispatched_at`, `_accepted` |
| browser receipt | dashboard actually displayed the alert | `alert_ack` from `script.js` |
| released | receipt absent for 10 s, or no client for 60 s | `sweep_alert_queue()` |

The frontend deliberately sends `alert_ack` only from `showAlarm()`, not when a packet
arrives. Thus queue acceptance is never reported as browser receipt.

### 3.3 Launcher and security

The user-supplied `start.bat` revision ran a synchronous PowerShell health poller
*before* the backend started, generated invalid PowerShell, and killed every
`powershell.exe` on the machine during cleanup. The corrected launcher:

* checks `%ERRORLEVEL%` after venv creation, pip install, and a combined import test;
* aborts before booting the backend if any dependency step fails;
* starts the browser helper with `start /b` so the backend boots concurrently;
* polls `/health` once per second for at most 90 s and calls `Start-Process` on success;
* records helper and static-server PIDs in temp files and terminates only those PIDs.

Additional controls: `commonpath`-based path traversal rejection in the static server,
full-scheme support in the WebSocket Origin allow-list, and a `config_error` reply
when the 60 msg/s rate limit is exceeded. The endpoints remain unauthenticated but are
bound to loopback; this is documented, not claimed as secure against local code.

## 4. Verification methodology

Verification used four layers:

1. **Unit / pure logic** — `pytest` over `analytics.py` and backend helpers.
2. **Source-contract tests** — because Node.js is unavailable, `script.js` invariants
   are asserted from source (priority queue, dedupe, CSV escaping, incremental render).
3. **Integration** — a real backend subprocess, synthetic MJPEG camera, and real
   WebSocket client.
4. **Stress** — telemetry queue filled to capacity while three alert packets are sent;
   all three must be delivered first.

## 5. Results

| Metric | Result |
|---|---|
| Automated tests | 64 passed, 0 failed |
| First camera CONNECTED (synthetic) | 2641 ms (target <3000 ms) |
| Unreachable source FAILED | 4641 ms with readable reason (target <6000 ms) |
| Alert priority under full telemetry backlog | 3/3 alerts delivered first |
| Launcher end-to-end | health OK, `/` served 200, browser opened, PID-scoped cleanup |
| Tracked `.pyc` files | 0 |

These results answer RQ1 (consistent, non-blocking diagnostics) and RQ2 (priority with
explicit receipt semantics) within the limits of the test harness. RQ3 is addressed in
the companion performance paper.

## 6. Threats to validity

* The integration camera is synthetic (`tools/fake_mjpeg.py`), not a physical phone.
* An already-open dashboard tab re-pushes its saved camera config on WebSocket
  reconnect; this is expected UX but can override `ALERT_VISION_SOURCE` in tests.
* Browser-only behaviours (audible alarm, ACK button, visual tile re-render) were not
  manually QA'd in this run.
* No token authentication or TLS is present; the design assumes loopback-only use.

## 7. Conclusion

Alert Vision demonstrates that reliability properties — priority queues, explicit
delivery states, bounded retries, fail-closed startup, and PID-scoped cleanup — can be
added to a modest Python dashboard without changing its external JSON contracts. The
implementation is supported by 64 passing tests and measured connection timing.
Future work should validate physical camera behaviour, add optional authentication for
non-loopback deployments, and evaluate detection accuracy on labelled crowd video.

## References

See `references.bib` for background sources on crowd dynamics, Farneback flow, YOLOv8,
ByteTrack, and the WebSocket protocol.
