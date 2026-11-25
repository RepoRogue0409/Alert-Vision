"""Connect-timing verification for Alert Vision (STEP 5).

Proves the two numbers the dashboard promises:

    python tools/verify_connect.py

  1. CONNECTED < 3s  - boots the backend against tools/fake_mjpeg.py and times
     the first /health response reporting cameras_online >= 1.
  2. FAILED  < 6s    - opens an unreachable stream in-process through
     CameraPipeline and times the transition to connection.state == "failed",
     printing the readable reason the dashboard will show.

The FAILED case runs in-process because a dashboard tab that is already open
re-applies its saved camera config on every reconnect, which would race an
end-to-end test. The pipeline transition is exactly what drives the chip.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    "rtsp_transport;tcp|fflags;nobuffer|probesize;32768|analyzeduration:0",
)

FAKE_SOURCE = "http://127.0.0.1:4747/video"
DEAD_SOURCE = "http://192.0.2.10:4747/video"  # TEST-NET-1: never answers
HEALTH_URL = "http://127.0.0.1:8766/health"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)


def _health():
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=1.0) as response:
            return json.load(response)
    except Exception:
        return None


def _port_free(port):
    import socket

    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def check_connected(timeout=15.0):
    for port in (8765, 8766):
        if not _port_free(port):
            print(f":: FAILED  port {port} is busy - stop the running backend first")
            return 1
    env = dict(os.environ, ALERT_VISION_SOURCE=FAKE_SOURCE)
    log = tempfile.NamedTemporaryFile(
        "w+", suffix=".log", delete=False, encoding="utf-8", errors="replace"
    )
    started = time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, "-u", "alert_vision.py"],
        env=env,
        cwd=REPO_ROOT,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = started + timeout
        while time.monotonic() < deadline:
            health = _health()
            if health and int(health.get("cameras_online") or 0) >= 1:
                elapsed = (time.monotonic() - started) * 1000.0
                verdict = "PASS" if elapsed < 3000 else "FAIL"
                print(
                    f":: CONNECTED  {elapsed:6.0f}ms  ({verdict} < 3000ms)  "
                    f"health={json.dumps(health)}"
                )
                return 0 if elapsed < 3000 else 1
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        print(f":: FAILED  backend never reported an online camera within {timeout:.0f}s")
        return 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.flush()
        log.seek(0)
        tail = log.read().strip().splitlines()[-6:]
        log.close()
        for line in tail:
            print("   |", line)


def check_failed(source=DEAD_SOURCE):
    import alert_vision

    pipeline = alert_vision.CameraPipeline("probe", "PROBE", source, state=None)
    started = time.monotonic()
    pipeline._apply_pending_source()
    elapsed = (time.monotonic() - started) * 1000.0
    connection = pipeline._connection_payload()
    verdict = "PASS" if connection["state"] == "failed" and elapsed < 6000 else "FAIL"
    print(
        f":: FAILED     {elapsed:6.0f}ms  ({verdict} < 6000ms)  "
        f"state={connection['state']}  reason={connection['error']!r}"
    )
    if pipeline.cap is not None:
        pipeline.cap.release()
    return 0 if verdict == "PASS" else 1


def main():
    status = check_connected()
    status |= check_failed()
    return status


if __name__ == "__main__":
    raise SystemExit(main())
