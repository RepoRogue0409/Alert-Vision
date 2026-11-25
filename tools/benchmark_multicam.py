"""Real multi-camera performance benchmark (Phase E).

    python tools/benchmark_multicam.py --counts 1 2 4 6 --seconds 8

Starts N synthetic MJPEG cameras and the REAL backend, pushes a config through
the REAL WebSocket, waits for all cameras online, then samples:

  * backend CPU % and RSS (psutil, measured on this machine)
  * telemetry packet interval (from real WS packets)
  * /health cameras_online / model_loaded / mjpeg_clients

Accuracy is NOT measured here. Hardware is reported by docs/PERFORMANCE.md.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

import psutil
import websockets

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

WS_URI = "ws://127.0.0.1:8765"
HEALTH_URL = "http://127.0.0.1:8766/health"
BASE_PORT = 14847


def _port_free(port):
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _wait_port(port, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.1)
    return False


def _mjpeg_fps(cam_id, seconds=3.0):
    """Count real MJPEG frame boundaries delivered to one relay client."""
    url = f"http://127.0.0.1:8766/video?cam={cam_id}"
    boundary = b"--frame"
    try:
        with urllib.request.urlopen(url, timeout=3.0) as response:
            started = time.monotonic()
            chunk = response.read(65536)
            count = 0
            while chunk and time.monotonic() - started < seconds:
                count += chunk.count(boundary)
                chunk = response.read(65536)
            elapsed = max(time.monotonic() - started, 1e-3)
            return round(max(0, count - 1) / elapsed, 1)  # subtract stream header
    except Exception:
        return None


def _health():
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=1.0) as response:
            return json.load(response)
    except Exception:
        return None


def _alive(member):
    try:
        return member.is_running() and member.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def _terminate(proc):
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


async def _measure_one(count, seconds, base_port):
    ports = [base_port + i for i in range(count)]
    for port in (8765, 8766, 8000):
        if not _port_free(port):
            raise RuntimeError(f"port {port} busy - stop the running backend first")
    for port in ports:
        if not _port_free(port):
            raise RuntimeError(f"fake camera port {port} busy")

    fakes = []
    backend = None
    logs = []
    result = {"cameras": count, "ok": False}
    try:
        for port in ports:
            log = tempfile.NamedTemporaryFile("w+", suffix=".log", delete=False, encoding="utf-8")
            logs.append(log)
            fakes.append(subprocess.Popen(
                [sys.executable, os.path.join(REPO_ROOT, "tools", "fake_mjpeg.py"), "--port", str(port)],
                cwd=REPO_ROOT, stdout=log, stderr=subprocess.STDOUT,
            ))
        if not all(_wait_port(port) for port in ports):
            raise RuntimeError("synthetic camera failed to start")

        sources = [f"http://127.0.0.1:{port}/video" for port in ports]
        env = dict(os.environ, ALERT_VISION_SOURCE=sources[0])
        backend_log = tempfile.NamedTemporaryFile("w+", suffix=".log", delete=False, encoding="utf-8")
        logs.append(backend_log)
        backend = subprocess.Popen(
            [sys.executable, "-u", "alert_vision.py"],
            env=env, cwd=REPO_ROOT, stdout=backend_log, stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline and backend.poll() is None:
            if _health() is not None:
                break
            await asyncio.sleep(0.1)
        if backend.poll() is not None:
            raise RuntimeError("backend exited during boot")

        # Windows can launch the real interpreter as a child of a shim PID;
        # measure the whole process tree, not just the launcher.
        def tree():
            root = psutil.Process(backend.pid)
            try:
                return [root] + root.children(recursive=True)
            except psutil.Error:
                return [root]

        def cpu_snapshot(members):
            totals = {}
            for member in members:
                if not _alive(member):
                    continue
                try:
                    times = member.cpu_times()
                    totals[member.pid] = float(times.user) + float(times.system)
                except psutil.Error:
                    pass
            return totals

        async with websockets.connect(WS_URI, open_timeout=10) as ws:
            config = {
                "type": "config",
                "stride": 1,
                "cameras": [
                    {"id": f"cam{i + 1}", "label": f"CAM {i + 1}", "source": src}
                    for i, src in enumerate(sources)
                ],
            }
            await ws.send(json.dumps(config))
            online_at = None
            boot_at = time.monotonic()
            while time.monotonic() - boot_at < 40.0:
                message = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                if message.get("type") != "telemetry":
                    continue
                cameras = message.get("cameras") or []
                if len(cameras) >= count and all(
                    cam.get("online") and (cam.get("connection") or {}).get("state") == "connected"
                    for cam in cameras[:count]
                ):
                    online_at = (time.monotonic() - boot_at) * 1000.0
                    break
            if online_at is None:
                raise RuntimeError("not all synthetic cameras reached CONNECTED")
            result["all_online_ms"] = round(online_at, 1)

            intervals = []
            cpu = []
            rss = []
            last = None
            last_sample = time.monotonic()
            cpu_before = cpu_snapshot(tree())
            cpu_count = psutil.cpu_count() or 1
            sample_start = last_sample
            while time.monotonic() - sample_start < seconds:
                # Drain packets at the real 10 Hz cadence; a fixed sleep here
                # would measure our polling loop, not the backend.
                try:
                    message = json.loads(await asyncio.wait_for(ws.recv(), timeout=0.2))
                except asyncio.TimeoutError:
                    message = None
                if message and message.get("type") == "telemetry":
                    now = time.monotonic()
                    if last is not None:
                        intervals.append((now - last) * 1000.0)
                    last = now
                now = time.monotonic()
                if now - last_sample >= 0.5:
                    members = tree()
                    cpu_now = cpu_snapshot(members)
                    elapsed = max(now - last_sample, 1e-3)
                    # CPU seconds consumed by the whole tree over this window,
                    # normalized to one core (100% = one fully busy core).
                    used = sum(
                        max(0.0, value - cpu_before.get(pid, 0.0))
                        for pid, value in cpu_now.items()
                    )
                    cpu.append(100.0 * used / elapsed / cpu_count)
                    rss.append(sum(
                        m.memory_info().rss for m in members if _alive(m)
                    ) / (1024.0 * 1024.0))
                    cpu_before = cpu_now
                    last_sample = now

            mjpeg_fps = await asyncio.to_thread(_mjpeg_fps, "cam1", 3.0)
            health = _health() or {}
            result.update({
                "mjpeg_fps_cam1": mjpeg_fps,
                "ok": True,
                "sample_seconds": round(time.monotonic() - sample_start, 1),
                "telemetry_packets": len(intervals) + (1 if last else 0),
                "telemetry_avg_interval_ms": round(sum(intervals) / len(intervals), 1) if intervals else None,
                "telemetry_max_interval_ms": round(max(intervals), 1) if intervals else None,
                "cpu_percent_mean": round(sum(cpu) / len(cpu), 1) if cpu else None,
                "cpu_percent_max": round(max(cpu), 1) if cpu else None,
                "rss_mb_mean": round(sum(rss) / len(rss), 1) if rss else None,
                "rss_mb_max": round(max(rss), 1) if rss else None,
                "cameras_online": health.get("cameras_online"),
                "model_loaded": health.get("model_loaded"),
                "ws_clients": health.get("ws_clients"),
                "mjpeg_clients": health.get("mjpeg_clients"),
            })
    finally:
        _terminate(backend)
        for fake in fakes:
            _terminate(fake)
        for log in logs:
            try:
                log.flush()
                name = log.name
                log.close()
                with open(name, "r", encoding="utf-8", errors="replace") as handle:
                    tail = handle.read().strip().splitlines()[-4:]
                if not result.get("ok"):
                    for line in tail:
                        print("   |", line)
            except OSError:
                pass
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--counts", type=int, nargs="+", default=[1, 2, 4, 6])
    parser.add_argument("--seconds", type=float, default=8.0)
    parser.add_argument("--base-port", type=int, default=BASE_PORT)
    args = parser.parse_args()
    print(":: REAL multi-camera benchmark (synthetic streams, real backend)")
    status = 0
    for count in args.counts:
        try:
            result = asyncio.run(_measure_one(count, args.seconds, args.base_port))
        except Exception as error:
            result = {"cameras": count, "ok": False, "error": f"{type(error).__name__}: {error}"}
            status = 1
        print("RESULT " + json.dumps(result, sort_keys=True), flush=True)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
