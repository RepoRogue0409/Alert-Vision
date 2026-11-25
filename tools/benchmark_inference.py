"""Latency-only YOLO preprocessing benchmark (Phase B/E).

    python tools/benchmark_inference.py [--iterations 20]

Measures wall-clock `model.track()` latency for synthetic frames at the widths
the backend actually produces (<= MAX_FRAME_WIDTH = 640).  It deliberately
makes NO accuracy claim: there are no labelled people here.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    "rtsp_transport;tcp|fflags;nobuffer|probesize;32768|analyzeduration;0",
)
import cv2  # noqa: E402
import numpy as np  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from ultralytics import YOLO  # noqa: E402

from alert_vision import MAX_FRAME_WIDTH, PERSON_CLASS_ID, TRACKER_CONFIG  # noqa: E402


def _percentile(values, pct):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round((len(ordered) - 1) * pct)))]


def benchmark(width, height, iterations, model):
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:] = (22, 30, 38)
    cv2.rectangle(frame, (width // 4, height // 4), (width * 3 // 4, height * 3 // 4), (180, 120, 90), -1)
    # One warm-up call excludes one-time Torch/OpenCV initialization from the
    # reported steady-state latency.
    model.track(frame, persist=True, tracker=TRACKER_CONFIG, classes=[PERSON_CLASS_ID],
                conf=0.25, device="cpu", verbose=False)
    times = []
    for _ in range(iterations):
        started = time.perf_counter()
        model.track(
            frame,
            persist=True,
            tracker=TRACKER_CONFIG,
            classes=[PERSON_CLASS_ID],
            conf=0.25,
            device="cpu",
            verbose=False,
        )
        times.append((time.perf_counter() - started) * 1000.0)
    return {
        "width": width,
        "height": height,
        "iterations": iterations,
        "mean_ms": round(float(np.mean(times)), 2),
        "p50_ms": round(_percentile(times, 0.50), 2),
        "p95_ms": round(_percentile(times, 0.95), 2),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=20)
    args = parser.parse_args()
    model = YOLO(os.environ.get("ALERT_VISION_MODEL") or "yolov8n.pt")
    results = [benchmark(320, 240, args.iterations, model), benchmark(MAX_FRAME_WIDTH, 480, args.iterations, model)]
    print(":: YOLO track() latency on synthetic frames (CPU, no accuracy claim)")
    for item in results:
        print("   {width}x{height}: mean={mean_ms}ms p50={p50_ms}ms p95={p95_ms}ms (n={iterations})".format(**item))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
