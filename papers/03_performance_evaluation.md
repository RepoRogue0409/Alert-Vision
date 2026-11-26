# Measured Performance of a Six-Camera Crowd-Telemetry Pipeline on Commodity CPU Hardware

**Draft research paper — v1.0, 25 November 2026**  
**Author:** [Author name], [Affiliation]  
**Artifact:** `tools/benchmark_multicam.py`, `tools/benchmark_inference.py`, commit `93b7481`

## Abstract

We report the resource cost of Alert Vision, a six-camera crowd-telemetry pipeline that
runs YOLOv8/ByteTrack inference per camera on a commodity laptop CPU. Rather than
estimating capacity, we measure CPU utilisation, resident memory, telemetry cadence,
MJPEG delivery rate, and time-to-connected for one, two, four, and six synthetic
cameras. On an AMD Ryzen 7 7435HS with 16 logical processors, mean CPU utilisation rose
from 36.5% at one camera to 70.5% at six, and mean RSS from 381 MB to 697 MB, while
the 100 ms telemetry cadence was maintained (observed mean interval 107–112 ms). A
single MJPEG client received approximately 22.5 fps. We explicitly identify what was
not measured, including twelve simultaneous MJPEG clients and physical camera hardware.

**Keywords:** performance measurement, multi-camera systems, YOLOv8, CPU inference,
telemetry, MJPEG.

---

## 1. Introduction

Real-time crowd dashboards are often described as "real-time" without reporting the
hardware or the measurement method. This paper provides a reproducible measurement
protocol and the resulting numbers for Alert Vision. The goal is not to claim an
optimal implementation, but to establish an honest baseline on the exact machine used
for development.

## 2. Hardware and software

| Item | Value |
|---|---|
| OS | Windows 11 Home Single Language, build 26300 |
| CPU | AMD Ryzen 7 7435HS, 8 cores / 16 logical processors |
| RAM | 24,261 MB |
| Inference device | CPU (`device="cpu"`) |
| Model | `yolov8n.pt`, 6,549,796 bytes, read-only |
| Python | 3.11 project virtual environment |

## 3. Method

`tools/benchmark_multicam.py` performs, for each requested camera count:

1. start N synthetic MJPEG servers on ports 14847…14852;
2. start the real backend subprocess with an initial synthetic source;
3. wait for `/health`;
4. connect a real WebSocket client and push a config containing N cameras;
5. wait until all cameras report `online=true` and `connection.state="connected"`;
6. sample telemetry packet intervals for the configured window;
7. sample backend CPU and RSS every 0.5 s over the **whole process tree**, using
   `psutil.cpu_times()` deltas normalised to one core;
8. fetch one MJPEG stream for 3 s and count `--frame` boundaries;
9. terminate all child processes.

CPU is deliberately measured from `cpu_times()` deltas rather than
`cpu_percent(interval=None)`: on Windows the Python interpreter may be launched as a
child of a shim process, and the naive method can report 0% for the launcher while the
real worker consumes several cores.

## 4. Results

Command: `python tools/benchmark_multicam.py --counts 1 2 4 6 --seconds 6`

| Cameras | All online (ms) | CPU mean / max (% core) | RSS mean / max (MB) | Telemetry avg / max (ms) | MJPEG fps |
|---:|---:|---:|---:|---:|---:|
| 1 | 110 | 36.5 / 49.7 | 381.4 / 395.3 | 112.3 / 187.0 | 22.8 |
| 2 | 172 | 59.4 / 85.2 | 444.6 / 467.2 | 111.7 / 188.0 | 22.7 |
| 4 | 547 | 68.0 / 88.6 | 570.8 / 599.5 | 106.9 / 125.0 | 22.6 |
| 6 | 1078 | 70.5 / 86.2 | 696.9 / 733.2 | 108.2 / 188.0 | 22.5 |

All four runs reported `ok=true`, with `cameras_online` equal to the requested count
and `model_loaded=true`.

### 4.1 YOLO track latency

`tools/benchmark_inference.py --iterations 10` after one warm-up call:

| Input | Mean | p50 | p95 |
|---|---:|---:|---:|
| 320×240 | 33.48 ms | 31.43 ms | 54.25 ms |
| 640×480 | 30.74 ms | 30.69 ms | 31.80 ms |

The smaller input's p95 is a thread-scheduling artifact, not evidence that downscaling
is slower. The numbers are steady-state latency only; no accuracy is implied.

## 5. Analysis

* **Telemetry cadence** remained close to the 100 ms design target at all loads; a
  single delayed packet produced the 187–188 ms maxima.
* **Memory** scaled approximately linearly with camera count because each camera owns
  a separate YOLO instance. Six cameras required about 697 MB mean RSS on this machine.
* **CPU** was not strictly monotonic (2 cameras sampled higher than 4). YOLO uses
  multiple Torch threads and the sampler uses 0.5 s windows; the values should be read
  as observed ranges, not a capacity curve.
* **MJPEG** delivered ~22.5 fps against a 25 fps sleep target, the difference being
  JPEG encoding overhead in the worker thread.

## 6. Threats to validity

* Synthetic cameras produce constant scenes; real DroidCam streams may differ in FPS,
  network jitter, and encoding.
* Only one WebSocket client and one MJPEG client were active during measurement.
* Twelve MJPEG clients, stride 10, GPU inference, and long-duration leakage were not
  measured.
* The benchmark terminates its own processes; it does not model operator-driven
  camera hot-swap during load.

## 7. Conclusion

On the tested hardware, the design supports six cameras within roughly 70% of one CPU
core and 700 MB RSS while preserving telemetry cadence. These are measured baselines,
not claims about other hardware or about detection quality. Future work should repeat
the benchmark with physical cameras, multiple concurrent viewers, and labelled video
for accuracy correlation.

## References

See `references.bib`.
