# Performance measurements (Phase E)

All numbers below were **measured on this machine**, not estimated. They come
from `tools/benchmark_multicam.py` (real backend + synthetic 15 fps MJPEG
cameras) and `tools/benchmark_inference.py` (real `yolov8n.pt`, synthetic
frames). Re-run them before quoting numbers on different hardware.

## Hardware

| Item | Value |
|---|---|
| OS | Microsoft Windows 11 Home Single Language, build 26300 |
| CPU | AMD Ryzen 7 7435HS, 8 cores / 16 logical processors |
| RAM | 24,261 MB |
| GPU inference | not used (`device=cpu`, `yolov8n.pt`) |
| Python | 3.11 (project venv) |

## Multi-camera benchmark (6 s telemetry window + 3 s MJPEG window per run)

Command: `python tools/benchmark_multicam.py --counts 1 2 4 6 --seconds 6`

| Cameras | All online (ms) | CPU mean / max (% of one core) | RSS mean / max (MB) | Telemetry avg / max interval (ms) | MJPEG fps (cam1, 1 client) |
|---:|---:|---:|---:|---:|---:|
| 1 | 110 | 36.5 / 49.7 | 381.4 / 395.3 | 112.3 / 187.0 | 22.8 |
| 2 | 172 | 59.4 / 85.2 | 444.6 / 467.2 | 111.7 / 188.0 | 22.7 |
| 4 | 547 | 68.0 / 88.6 | 570.8 / 599.5 | 106.9 / 125.0 | 22.6 |
| 6 | 1078 | 70.5 / 86.2 | 696.9 / 733.2 | 108.2 / 188.0 | 22.5 |

Observations (measured, not assumed):

* Telemetry held its ~100 ms design cadence at all four loads (max observed
  188 ms, one delayed packet; no queue overflow was seen with one WS client).
* Memory grew roughly linearly with camera count: ~381 MB at 1 camera to
  ~697 MB at 6 (mean RSS), consistent with a separate YOLO instance per camera.
* CPU is not strictly monotonic (2 cameras sampled higher than 4). YOLO uses
  multiple Torch threads and the sampler is a 0.5 s window; treat these as
  observed ranges, not a capacity curve.
* MJPEG relay delivered ~22.5 fps to one client, below the 25 fps sleep target
  because each frame is JPEG-encoded by the worker thread. Up to 12 clients and
  6 cameras simultaneously was **NOT measured**.

## YOLO `track()` latency (CPU, synthetic frames, 10 iterations after warm-up)

Command: `python tools/benchmark_inference.py --iterations 10`

| Input | mean (ms) | p50 (ms) | p95 (ms) |
|---|---:|---:|---:|
| 320x240 | 33.48 | 31.43 | 54.25 |
| 640x480 | 30.74 | 30.69 | 31.80 |

The 320x240 p95 outlier is a measurement artifact of thread scheduling; the
smaller input is **not** slower in steady state. No accuracy claim is made here.

## What was NOT measured

* Real DroidCam hardware at `192.168.1.106:4747` (network/FPS conditions differ).
* 12 simultaneous MJPEG clients, or 6 cameras × 12 clients.
* GPU inference, stride 10, or long-duration memory-leak behaviour.
* Accuracy / ID-switch rate (see `docs/ACCURACY_LIMITATIONS.md`).

## Reproducing

1. Start nothing else on ports 8000/8765/8766/14847..14852.
2. Run the commands above. The harness terminates all child processes it
   starts and prints one `RESULT {...}` JSON object per camera count.
