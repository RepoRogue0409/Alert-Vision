"""
Alert Vision - Multi-Camera Crowd Safety & Stampede Prevention Platform
======================================================================

Production backend architecture:

    SharedSystemState   - dynamic camera registry + global risk configuration
    CameraPipeline      - one independent worker thread per camera channel
                          (own cv2.VideoCapture handle, own YOLOv8 tracker)
    WebSocketBroadcaster- async fan-out of unified multi-camera JSON packets
    FrameRelayServer    - stdlib ThreadingHTTPServer MJPEG relay on :8766
    AlertVisionBackend  - async orchestration (websocket + aggregation loop)

Runtime contract:
    * Any number of camera channels (webcam index / RTSP / HTTP) run
      concurrently, each in its own worker thread with an isolated
      cv2.VideoCapture resource and a private YOLOv8 tracker instance.
    * Frames wider than 640px are downscaled before inference.
    * A selectable inference stride skips frames per channel.
    * Detection uses model.track(persist=True) with ByteTrack, so every
      human box receives a persistent integer ID rendered as
      'Person ID: X' on the relayed frame.
    * Farneback optical flow provides turbulence / convergence / velocity.
    * A lone detection (people_now <= 1) is hard-clamped to SAFE with
      turbulence/convergence/velocity suppressed to zero.
    * A rolling 12-frame debounce requires 8 consistent votes before the
      published risk level changes (ghost spike elimination).
    * HIGH / CRITICAL transitions append a row to crowdshield_alert_log.csv
      (Timestamp, CameraID, RiskLevel, ConfidenceScore, ActionRequired).

Endpoints:
    ws://127.0.0.1:8765            multi-camera telemetry + config channel
    http://127.0.0.1:8766/video?cam=cam1     MJPEG annotated stream
    http://127.0.0.1:8766/frame.jpg?cam=cam1 single JPEG snapshot

Run:
    python alert_vision.py
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import json
import os
import threading
import time
from collections import Counter, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np
import nest_asyncio
import torch
from ultralytics import YOLO

try:
    from websockets.asyncio.server import serve as websocket_server
except ImportError:  # pragma: no cover - legacy package layout fallback
    from websockets.legacy.server import serve as websocket_server

HOST = "127.0.0.1"
WEBSOCKET_PORT = 8765
VIDEO_PORT = 8766
MAX_FRAME_WIDTH = 640
MAX_CAMERAS = 6
DEFAULT_STRIDE = 1
BROADCAST_INTERVAL = 0.1
JPEG_QUALITY = 72

LOG_FILE = "crowshield_alert_log.csv"
LOG_HEADERS = ["Timestamp", "CameraID", "RiskLevel", "ConfidenceScore", "ActionRequired"]
YOLO_MODEL_PATH = "yolov8n.pt"

PERSON_CLASS_ID = 0
PERSON_CONFIDENCE_THRESHOLD = 0.25
TRACKER_CONFIG = "bytetrack.yaml"

DEBOUNCE_WINDOW = 12
DEBOUNCE_MIN_VOTES = 8

RISK_LEVELS = ("SAFE", "MODERATE", "HIGH", "CRITICAL")
RISK_RANK = {level: index for index, level in enumerate(RISK_LEVELS)}

HEADCOUNT_HIGH_THRESHOLD = 6.0
DENSITY_CRITICAL_THRESHOLD = 0.20
TURBULENCE_CRITICAL_THRESHOLD = 3.5
MOTION_THRESHOLD = 1.0

RISK_SCORE_THRESHOLDS = {"moderate": 0.35, "high": 0.55, "critical": 0.75}
DEFAULT_WEIGHTS = {"density": 0.4, "rate_of_change": 0.2, "convergence": 0.2, "turbulence": 0.2}
DEFAULT_ALERT_THRESHOLD = 0.55

RECONNECT_INTERVAL = 2.0
SCORE_GAMMA = 1.12


def _ensure_log_file(path: str) -> None:
    if not os.path.exists(path):
        with open(path, "w", newline="") as handle:
            csv.writer(handle).writerow(LOG_HEADERS)


def _select_inference_device() -> str:
    if torch.cuda.is_available():
        return "cuda:0"
    return "cpu"


def normalize_source(value):
    """Normalize a camera source into an int index or a stream URL string."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value if value is not None else "").strip()
    if text.isdigit():
        return int(text)
    return text


def _worst_level(levels) -> str:
    worst = "SAFE"
    for level in levels:
        if RISK_RANK.get(level, 0) > RISK_RANK.get(worst, 0):
            worst = level
    return worst


def _source_display(source) -> str:
    if isinstance(source, int):
        return f"Webcam {source}"
    return str(source) if source else "Unassigned"


# ---------------------------------------------------------------------------
# Per-camera worker (isolated capture handle + tracker + debounce state)
# ---------------------------------------------------------------------------
class CameraPipeline:
    """One camera channel running in its own daemon worker thread."""

    def __init__(self, cam_id: str, label: str, source, state: "SharedSystemState") -> None:
        self.id = cam_id
        self.label = label
        self.source = source
        self.state = state

        self.capture_lock = threading.Lock()
        self.frame_lock = threading.Lock()
        self._pending_source = source
        self._source_dirty = True
        self.stop_event = threading.Event()
        self.thread = None

        self.cap = None
        self.model = None
        self.device = _select_inference_device()

        self.frame_counter = 0
        self.prev_small = None
        self.latest_frame = None
        self.online = False
        self.last_reconnect_attempt = 0.0

        self.seen_ids = set()
        self.risk_history = deque(maxlen=DEBOUNCE_WINDOW)
        self.current_risk = "SAFE"
        self.alert_state = "SAFE"
        self.risk_score = 0.0
        self._pending_alert = None

        self.packet = self._offline_packet()

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        if self.thread is not None:
            return
        self.thread = threading.Thread(target=self._run, name=f"cam-{self.id}", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=5.0)
            self.thread = None
        with self.capture_lock:
            self._source_dirty = False
            if self.cap is not None:
                with contextlib.suppress(Exception):
                    self.cap.release()
                self.cap = None
        self.online = False

    def set_source(self, source) -> None:
        """Queue a hot-swap; only the reader thread touches the handle."""
        with self.capture_lock:
            self._pending_source = source
            self._source_dirty = True
        print(f":: [Alert Vision] {self.id} hot-swap queued -> {_source_display(source)}")

    # -- capture -------------------------------------------------------------
    @staticmethod
    def _open_capture(source):
        if isinstance(source, str) and not source.strip():
            return None
        cap = None
        if isinstance(source, int) and os.name == "nt":
            cap = cv2.VideoCapture(source, cv2.CAP_DSHOW)
            if cap is None or not cap.isOpened():
                if cap is not None:
                    cap.release()
                cap = cv2.VideoCapture(source)
        elif isinstance(source, str):
            params = []
            if hasattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC"):
                params.extend([int(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC), 5000])
            if hasattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC"):
                params.extend([int(cv2.CAP_PROP_READ_TIMEOUT_MSEC), 5000])
            try:
                cap = cv2.VideoCapture(source, cv2.CAP_FFMPEG, params) if params else cv2.VideoCapture(source)
            except Exception:
                cap = cv2.VideoCapture(source)
            if cap is None or not cap.isOpened():
                if cap is not None:
                    with contextlib.suppress(Exception):
                        cap.release()
                cap = cv2.VideoCapture(source)
        else:
            cap = cv2.VideoCapture(source)
        if cap is not None and cap.isOpened():
            with contextlib.suppress(Exception):
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            return cap
        if cap is not None:
            with contextlib.suppress(Exception):
                cap.release()
        return None

    def _apply_pending_source(self) -> None:
        with self.capture_lock:
            if not self._source_dirty:
                return
            self._source_dirty = False
            source = self._pending_source
            self._pending_source = None
            if self.cap is not None:
                with contextlib.suppress(Exception):
                    self.cap.release()
                self.cap = None
            self.source = source
            self.cap = self._open_capture(source)
            self.online = self.cap is not None

        self.frame_counter = 0
        self.prev_small = None
        self.risk_history.clear()
        self.current_risk = "SAFE"
        self.alert_state = "SAFE"
        self.risk_score = 0.0
        self._pending_alert = None
        if self.model is not None:
            # drop stale ByteTrack state so a new stream starts fresh IDs
            with contextlib.suppress(Exception):
                self.model.predictor = None
        status = "ONLINE" if self.online else "OFFLINE"
        print(f":: [Alert Vision] {self.id} hot-swap applied -> {_source_display(self.source)} [{status}]")

    def _try_reconnect(self) -> None:
        now = time.time()
        if now - self.last_reconnect_attempt < RECONNECT_INTERVAL:
            return
        self.last_reconnect_attempt = now
        with self.capture_lock:
            if self._source_dirty:
                return
            if self.cap is not None and self.cap.isOpened():
                self.online = True
                return
            if self.cap is not None:
                with contextlib.suppress(Exception):
                    self.cap.release()
                self.cap = None
            self.cap = self._open_capture(self.source)
            self.online = self.cap is not None
            if self.online:
                self.prev_small = None

    def read_frame(self):
        with self.capture_lock:
            cap = self.cap
        if cap is None or not cap.isOpened():
            self.online = False
            return None
        grabbed, frame = cap.read()
        if not grabbed or frame is None:
            self.online = False
            with self.capture_lock:
                if self.cap is cap:
                    with contextlib.suppress(Exception):
                        cap.release()
                    self.cap = None
            return None
        self.online = True
        return frame

    # -- worker loop ---------------------------------------------------------
    def _run(self) -> None:
        if self.model is None:
            self.model = YOLO(YOLO_MODEL_PATH)
        while not self.stop_event.is_set():
            self._apply_pending_source()
            frame = self.read_frame()
            if frame is None:
                self._publish(self._offline_packet())
                self._try_reconnect()
                time.sleep(0.1)
                continue

            self.frame_counter += 1
            stride = max(1, int(self.state.config_snapshot()["stride"]))
            if self.frame_counter % stride != 0:
                continue

            try:
                packet = self.process_frame(frame)
            except Exception as error:  # keep the channel alive on transient faults
                print(f":: [Alert Vision] {self.id} frame fault (recovered): {error}")
                continue
            if packet is not None:
                self._publish(packet)
            time.sleep(0.005)

    def _publish(self, packet: dict) -> None:
        with self.frame_lock:
            self.packet = packet

    def grab_jpeg(self, quality: int = JPEG_QUALITY):
        with self.frame_lock:
            frame = self.latest_frame
        if frame is None:
            return None
        ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
        if not ok or buffer is None:
            return None
        return buffer.tobytes()

    # -- analytics -----------------------------------------------------------
    def process_frame(self, frame):
        height, width = frame.shape[:2]
        if width > MAX_FRAME_WIDTH:
            scale = MAX_FRAME_WIDTH / float(width)
            frame = cv2.resize(
                frame,
                (int(width * scale), int(height * scale)),
                interpolation=cv2.INTER_AREA,
            )
            height, width = frame.shape[:2]

        config = self.state.config_snapshot()
        detections = []
        confidences = []
        occupied_area = 0.0

        results = self.model.track(
            frame,
            persist=True,
            tracker=TRACKER_CONFIG,
            classes=[PERSON_CLASS_ID],
            conf=PERSON_CONFIDENCE_THRESHOLD,
            device=self.device,
            verbose=False,
        )
        boxes = results[0].boxes if results else None
        if boxes is not None and len(boxes) > 0:
            coords = boxes.xyxy.cpu().tolist()
            scores = boxes.conf.cpu().tolist()
            track_ids = boxes.id.cpu().tolist() if boxes.id is not None else [None] * len(scores)
            for box, confidence, track_id in zip(coords, scores, track_ids):
                confidence = float(confidence)
                if confidence < PERSON_CONFIDENCE_THRESHOLD:
                    continue
                x1, y1, x2, y2 = (float(box[0]), float(box[1]), float(box[2]), float(box[3]))
                box_w = max(0.0, x2 - x1)
                box_h = max(0.0, y2 - y1)
                occupied_area += box_w * box_h
                confidences.append(confidence)
                person_id = int(track_id) if track_id is not None else None
                if person_id is not None:
                    self.seen_ids.add(person_id)
                detections.append((x1, y1, x2, y2, confidence, person_id))

        people_now = len(detections)
        annotated = self._annotate(frame, detections)
        frame_area = float(width * height)
        density = float(np.clip(occupied_area / frame_area, 0.0, 1.0)) if frame_area > 0 else 0.0
        avg_confidence = float(np.mean(confidences)) if confidences else 0.0

        if people_now <= 1:
            # lone-person guard: suppress all motion vectors, hard SAFE
            self.prev_small = None
            turbulence = 0.0
            convergence = 0.0
            velocity = 0.0
            risk_score = 0.0
            instant_level = "SAFE"
        else:
            turbulence, convergence, velocity = self._flow_metrics(frame)
            risk_score, instant_level = self._score(
                people_now, density, turbulence, convergence, velocity, config
            )

        self.risk_history.append(instant_level)
        confirmed = self.current_risk
        if len(self.risk_history) >= DEBOUNCE_MIN_VOTES:
            candidate, votes = Counter(self.risk_history).most_common(1)[0]
            if votes >= DEBOUNCE_MIN_VOTES:
                confirmed = candidate
        if people_now <= 1:
            confirmed = "SAFE"

        alert_event = None
        if (
            confirmed in ("HIGH", "CRITICAL")
            and self.alert_state != confirmed
            and risk_score >= float(config["alert_threshold"])
            and people_now >= 2
        ):
            action = "DISPATCH" if confirmed == "CRITICAL" else "ESCALATE"
            alert_event = {
                "action": action,
                "risk_level": confirmed,
                "confidence": avg_confidence,
                "timestamp": time.time() * 1000.0,
            }
            self.state.log_alert(self.id, confirmed, avg_confidence)
        self.alert_state = confirmed
        self.current_risk = confirmed
        self.risk_score = float(risk_score)

        with self.frame_lock:
            self.latest_frame = annotated
        if alert_event is not None:
            self._pending_alert = alert_event

        return {
            "id": self.id,
            "label": self.label,
            "source": _source_display(self.source),
            "source_value": self.source,
            "online": True,
            "people_now": int(people_now),
            "unique_ids": int(len(self.seen_ids)),
            "tracks": [int(d[5]) for d in detections if d[5] is not None],
            "risk_level": confirmed,
            "risk_score": float(risk_score),
            "confidence": float(avg_confidence),
            "density": float(density),
            "turbulence": float(turbulence),
            "convergence": float(convergence),
            "velocity": float(velocity),
            "timestamp": float(time.time() * 1000.0),
            "alert": False,
            "action": "NONE",
            "camera_online": True,
        }

    def _annotate(self, frame, detections):
        annotated = frame.copy()
        for x1, y1, x2, y2, confidence, person_id in detections:
            top_left = (int(x1), int(y1))
            bottom_right = (int(x2), int(y2))
            cv2.rectangle(annotated, top_left, bottom_right, (0, 255, 140), 2)
            text = f"Person ID: {person_id}" if person_id is not None else f"Person {confidence:.2f}"
            (text_w, text_h), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
            label_top = max(0, int(y1) - text_h - 8)
            cv2.rectangle(
                annotated,
                (int(x1), label_top),
                (int(x1) + text_w + 10, label_top + text_h + 8),
                (0, 255, 140),
                -1,
            )
            cv2.putText(
                annotated,
                text,
                (int(x1) + 5, label_top + text_h + 3),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                (8, 20, 18),
                1,
                cv2.LINE_AA,
            )
        return annotated

    def _flow_metrics(self, frame):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        small = cv2.resize(gray, (0, 0), fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
        previous = self.prev_small
        if previous is None or previous.shape != small.shape:
            self.prev_small = small.copy()
            return 0.0, 0.0, 0.0

        flow = cv2.calcOpticalFlowFarneback(
            previous,
            small,
            None,
            pyr_scale=0.5,
            levels=5,
            winsize=15,
            iterations=3,
            poly_n=5,
            poly_sigma=1.1,
            flags=0,
        )
        self.prev_small = small.copy()
        fx = flow[:, :, 0].astype(np.float32)
        fy = flow[:, :, 1].astype(np.float32)
        magnitude = np.hypot(fx, fy)

        mean_magnitude = float(np.mean(magnitude)) if magnitude.size else 0.0
        std_magnitude = float(np.std(magnitude)) if magnitude.size else 0.0
        turbulence = float(std_magnitude / (abs(mean_magnitude) + 1e-6))
        velocity = float(np.hypot(float(np.mean(fx)), float(np.mean(fy))))

        flow_h, flow_w = fx.shape
        yy, xx = np.mgrid[0:flow_h, 0:flow_w]
        cx, cy = (flow_w - 1) / 2.0, (flow_h - 1) / 2.0
        dx = cx - xx
        dy = cy - yy
        radial_norm = np.hypot(dx, dy) + 1e-6
        radial = fx * (dx / radial_norm) + fy * (dy / radial_norm)
        convergence = float(np.clip(float(np.mean(radial)) / (mean_magnitude + 1e-6), 0.0, 1.0))
        return turbulence, convergence, velocity

    def _score(self, people_now, density, turbulence, convergence, velocity, config):
        headcount_index = float(min(people_now / HEADCOUNT_HIGH_THRESHOLD, 1.0))
        density_index = float(min(density / (DENSITY_CRITICAL_THRESHOLD + 1e-6), 1.0))
        combined_density = float(np.clip(0.6 * density_index + 0.4 * headcount_index, 0.0, 1.0))
        rate_score = float(min(velocity / (MOTION_THRESHOLD + 1e-6), 1.0))
        turbulence_score = float(min(turbulence / (TURBULENCE_CRITICAL_THRESHOLD + 1e-6), 1.0))
        convergence_score = float(np.clip(convergence, 0.0, 1.0))

        weights = config["weights"]
        weight_density = float(weights.get("density", DEFAULT_WEIGHTS["density"]))
        weight_rate = float(weights.get("rate_of_change", DEFAULT_WEIGHTS["rate_of_change"]))
        weight_convergence = float(weights.get("convergence", DEFAULT_WEIGHTS["convergence"]))
        weight_turbulence = float(weights.get("turbulence", DEFAULT_WEIGHTS["turbulence"]))
        weight_total = weight_density + weight_rate + weight_convergence + weight_turbulence
        if weight_total <= 1e-6:
            weight_density, weight_rate = DEFAULT_WEIGHTS["density"], DEFAULT_WEIGHTS["rate_of_change"]
            weight_convergence, weight_turbulence = DEFAULT_WEIGHTS["convergence"], DEFAULT_WEIGHTS["turbulence"]
            weight_total = sum(DEFAULT_WEIGHTS.values())

        raw = (
            weight_density * combined_density
            + weight_rate * rate_score
            + weight_convergence * convergence_score
            + weight_turbulence * turbulence_score
        ) / weight_total

        # non-linear normalization clamp keeps mid-band scores stable
        score = float(np.clip(raw, 0.0, 1.0))
        score = float(np.clip(score ** SCORE_GAMMA, 0.0, 1.0))

        thresholds = config["risk_thresholds"]
        if score >= thresholds["critical"]:
            level = "CRITICAL"
        elif score >= thresholds["high"]:
            level = "HIGH"
        elif score >= thresholds["moderate"]:
            level = "MODERATE"
        else:
            level = "SAFE"
        return score, level

    # -- telemetry snapshots -------------------------------------------------
    def _offline_packet(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "source": _source_display(self.source),
            "source_value": self.source,
            "online": False,
            "people_now": 0,
            "unique_ids": int(len(self.seen_ids)),
            "tracks": [],
            "risk_level": "SAFE",
            "risk_score": 0.0,
            "confidence": 0.0,
            "density": 0.0,
            "turbulence": 0.0,
            "convergence": 0.0,
            "velocity": 0.0,
            "timestamp": float(time.time() * 1000.0),
            "alert": False,
            "action": "NONE",
            "camera_online": False,
        }

    def snapshot(self) -> dict:
        with self.frame_lock:
            packet = dict(self.packet)
        event = self._pending_alert
        if event is not None:
            self._pending_alert = None
            packet["alert"] = True
            packet["action"] = event["action"]
            packet["risk_level"] = event["risk_level"]
            packet["confidence"] = float(event["confidence"])
        else:
            packet["alert"] = False
            packet["action"] = "NONE"
        packet["label"] = self.label
        packet["source"] = _source_display(self.source)
        packet["online"] = bool(self.online)
        packet["camera_online"] = bool(self.online)
        if not self.online:
            packet["people_now"] = 0
            packet["risk_level"] = "SAFE"
            packet["risk_score"] = 0.0
            packet["alert"] = False
            packet["action"] = "NONE"
        return packet


# ---------------------------------------------------------------------------
# Shared dynamic camera registry + global configuration
# ---------------------------------------------------------------------------
class SharedSystemState:
    def __init__(self, specs, stride: int = DEFAULT_STRIDE, log_file: str = LOG_FILE) -> None:
        self.lock = threading.RLock()
        self.cameras: dict[str, CameraPipeline] = {}
        self.stride = max(1, int(stride))
        self.weights = dict(DEFAULT_WEIGHTS)
        self.alert_threshold = float(DEFAULT_ALERT_THRESHOLD)
        self.risk_thresholds = dict(RISK_SCORE_THRESHOLDS)
        self.log_file = log_file
        self._log_lock = threading.Lock()
        _ensure_log_file(log_file)
        if specs:
            self.apply_cameras(specs, initial=True)

    # -- configuration -------------------------------------------------------
    def config_snapshot(self) -> dict:
        with self.lock:
            return {
                "weights": dict(self.weights),
                "alert_threshold": float(self.alert_threshold),
                "risk_thresholds": dict(self.risk_thresholds),
                "stride": int(self.stride),
            }

    def update_config(self, payload: dict) -> None:
        stride_changed = None
        with self.lock:
            weights = payload.get("weights")
            if isinstance(weights, dict):
                for key, value in weights.items():
                    if key in self.weights:
                        with contextlib.suppress(TypeError, ValueError):
                            self.weights[key] = float(value)
            threshold = payload.get("alert_threshold")
            if threshold is not None:
                with contextlib.suppress(TypeError, ValueError):
                    self.alert_threshold = float(np.clip(float(threshold), 0.0, 1.0))
            risk_thresholds = payload.get("risk_thresholds")
            if isinstance(risk_thresholds, dict):
                for key, value in risk_thresholds.items():
                    normalized = str(key).lower()
                    if normalized in self.risk_thresholds:
                        with contextlib.suppress(TypeError, ValueError):
                            self.risk_thresholds[normalized] = float(value)
            stride = payload.get("stride")
            if stride is not None:
                with contextlib.suppress(TypeError, ValueError):
                    requested = max(1, int(stride))
                    if requested != self.stride:
                        self.stride = requested
                        stride_changed = requested

        if stride_changed is not None:
            print(f":: [Alert Vision] Inference stride -> {stride_changed}")

        cameras = payload.get("cameras")
        if isinstance(cameras, list):
            self.apply_cameras(cameras)

    # -- camera registry -----------------------------------------------------
    def apply_cameras(self, specs, initial: bool = False) -> None:
        normalized = []
        seen = set()
        for index, spec in enumerate(specs[:MAX_CAMERAS]):
            if not isinstance(spec, dict):
                continue
            cam_id = str(spec.get("id") or f"cam{index + 1}").strip() or f"cam{index + 1}"
            if cam_id in seen:
                continue
            seen.add(cam_id)
            normalized.append(
                {
                    "id": cam_id,
                    "label": str(spec.get("label") or f"CAM {index + 1}").strip() or f"CAM {index + 1}",
                    "source": normalize_source(spec.get("source")),
                }
            )
        if not normalized:
            return

        with self.lock:
            existing = dict(self.cameras)

        wanted: dict[str, CameraPipeline] = {}
        new_pipelines = []
        for spec in normalized:
            pipeline = existing.get(spec["id"])
            if pipeline is None:
                pipeline = CameraPipeline(spec["id"], spec["label"], spec["source"], self)
                new_pipelines.append(pipeline)
            else:
                pipeline.label = spec["label"]
                if pipeline.source != spec["source"]:
                    pipeline.set_source(spec["source"])
            wanted[spec["id"]] = pipeline

        for cam_id, pipeline in existing.items():
            if cam_id not in wanted:
                pipeline.stop()
                print(f":: [Alert Vision] {cam_id} channel removed")

        with self.lock:
            self.cameras = wanted

        for pipeline in new_pipelines:
            pipeline.start()
            print(f":: [Alert Vision] {pipeline.id} channel online -> {_source_display(pipeline.source)}")

        if not initial:
            print(f":: [Alert Vision] Active camera channels: {', '.join(sorted(wanted))}")

    def camera_list(self):
        with self.lock:
            return [self.cameras[key] for key in sorted(self.cameras)]

    # -- telemetry -----------------------------------------------------------
    def build_packet(self) -> dict:
        pipelines = self.camera_list()
        cameras = [pipeline.snapshot() for pipeline in pipelines]
        online = [cam for cam in cameras if cam["online"]]
        aggregate_people = sum(int(cam["people_now"]) for cam in online)
        aggregate_level = _worst_level([cam["risk_level"] for cam in online])
        aggregate_score = max([float(cam["risk_score"]) for cam in online], default=0.0)
        return {
            "type": "telemetry",
            "timestamp": float(time.time() * 1000.0),
            "cameras": cameras,
            "camera_count": len(cameras),
            "camera_online": bool(online),
            "people_now": int(aggregate_people),
            "risk_level": aggregate_level if online else "SAFE",
            "risk_score": float(aggregate_score) if online else 0.0,
            "alert": any(bool(cam["alert"]) for cam in cameras),
        }

    def get_frame(self, cam_id=None):
        pipelines = self.camera_list()
        if not pipelines:
            return None
        chosen = None
        if cam_id:
            chosen = next((pipe for pipe in pipelines if pipe.id == cam_id), None)
        if chosen is None:
            chosen = next((pipe for pipe in pipelines if pipe.online), pipelines[0])
        return chosen.grab_jpeg()

    # -- persistent ledger ---------------------------------------------------
    def log_alert(self, cam_id: str, risk_level: str, confidence: float) -> None:
        action = "DISPATCH" if risk_level == "CRITICAL" else "ESCALATE"
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        row = [timestamp, cam_id, risk_level, f"{float(confidence):.2f}", action]
        with self._log_lock:
            with open(self.log_file, "a", newline="") as handle:
                csv.writer(handle).writerow(row)
        print(
            f":: [Alert Vision] THREAT LOGGED :: {timestamp} | {cam_id} | {risk_level} | "
            f"CONF {float(confidence):.2f} | ACTION {action}"
        )

    def shutdown(self) -> None:
        for pipeline in self.camera_list():
            pipeline.stop()
        with self.lock:
            self.cameras = {}


# ---------------------------------------------------------------------------
# WebSocket fan-out
# ---------------------------------------------------------------------------
class WebSocketBroadcaster:
    def __init__(self) -> None:
        self.clients = set()
        self._lock = asyncio.Lock()

    async def register(self, connection) -> None:
        async with self._lock:
            self.clients.add(connection)

    async def unregister(self, connection) -> None:
        async with self._lock:
            self.clients.discard(connection)

    def active_count(self) -> int:
        return len(self.clients)

    async def broadcast(self, packet: dict) -> None:
        clients = tuple(self.clients)
        if not clients:
            return
        message = json.dumps(packet)
        results = await asyncio.gather(
            *(self._send(client, message) for client in clients),
            return_exceptions=True,
        )
        for client, result in zip(clients, results):
            if isinstance(result, BaseException):
                self.clients.discard(client)

    @staticmethod
    async def _send(client, message: str) -> None:
        await client.send(message)


# ---------------------------------------------------------------------------
# MJPEG relay
# ---------------------------------------------------------------------------
class FrameRelayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, state: SharedSystemState) -> None:
        super().__init__(address, FrameRelayHandler)
        self.state = state


class FrameRelayHandler(BaseHTTPRequestHandler):
    server_version = "AlertVision/2.0"

    def do_GET(self) -> None:  # noqa: N802 - http server API
        parsed = urlparse(self.path)
        path = parsed.path
        cam_id = (parse_qs(parsed.query).get("cam") or [None])[0]
        if path in ("/frame.jpg", "/snapshot.jpg"):
            self._send_snapshot(cam_id)
            return
        if path != "/video":
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"not found")
            return
        self._send_mjpeg_stream(cam_id)

    def _send_snapshot(self, cam_id) -> None:
        jpeg_bytes = self.server.state.get_frame(cam_id)
        if jpeg_bytes is None:
            self.send_response(503)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b"frame not ready")
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(jpeg_bytes)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.wfile.write(jpeg_bytes)

    def _send_mjpeg_stream(self, cam_id) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            while True:
                jpeg_bytes = self.server.state.get_frame(cam_id)
                if jpeg_bytes is None:
                    time.sleep(0.05)
                    continue
                self.wfile.write(
                    b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(jpeg_bytes)).encode("latin-1")
                    + b"\r\n\r\n"
                )
                self.wfile.write(jpeg_bytes)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
                time.sleep(0.04)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except Exception:
            return

    def log_message(self, format, *args) -> None:  # silence http logs
        return


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
class AlertVisionBackend:
    def __init__(self, specs, stride: int = DEFAULT_STRIDE, log_file: str = LOG_FILE) -> None:
        self.state = SharedSystemState(specs, stride=stride, log_file=log_file)
        self.broadcaster = WebSocketBroadcaster()

    async def _handle_connection(self, connection, path=None) -> None:
        await self.broadcaster.register(connection)
        try:
            async with connection:
                while True:
                    try:
                        message = await connection.recv()
                    except Exception:
                        break
                    if message is None:
                        break
                    if isinstance(message, bytes):
                        text = message.decode("utf-8", "replace")
                    elif isinstance(message, str):
                        text = message
                    else:
                        continue
                    try:
                        payload = json.loads(text)
                    except Exception:
                        continue
                    if isinstance(payload, dict) and payload.get("type") == "config":
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(None, self.state.update_config, payload)
        except Exception:
            pass
        finally:
            await self.broadcaster.unregister(connection)

    def start_frame_relay(self):
        relay_server = FrameRelayServer((HOST, VIDEO_PORT), self.state)
        relay_thread = threading.Thread(
            target=relay_server.serve_forever,
            kwargs={"poll_interval": 0.5},
            daemon=True,
            name="mjpeg-relay",
        )
        relay_thread.start()
        return relay_server, relay_thread

    async def run(self) -> None:
        relay_server, relay_thread = self.start_frame_relay()
        try:
            async with websocket_server(self._handle_connection, HOST, WEBSOCKET_PORT):
                print(
                    f":: [Alert Vision] WebSocket telemetry online on ws://{HOST}:{WEBSOCKET_PORT} "
                    f"| MJPEG relay on http://{HOST}:{VIDEO_PORT}/video?cam=<id>"
                )
                while True:
                    packet = self.state.build_packet()
                    await self.broadcaster.broadcast(packet)
                    await asyncio.sleep(BROADCAST_INTERVAL)
        finally:
            self.state.shutdown()
            cv2.destroyAllWindows()
            if relay_server is not None:
                relay_server.shutdown()
                relay_server.server_close()
            if relay_thread is not None:
                relay_thread.join(timeout=5)
            print(":: [Alert Vision] Backend shutdown complete")


def _initial_specs():
    raw = (
        os.environ.get("ALERT_VISION_SOURCES")
        or os.environ.get("ALERT_VISION_SOURCE")
        or "0"
    )
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    if not parts:
        parts = ["0"]
    return [
        {"id": f"cam{index + 1}", "label": f"CAM {index + 1}", "source": normalize_source(part)}
        for index, part in enumerate(parts[:MAX_CAMERAS])
    ]


def main() -> None:
    nest_asyncio.apply()
    stride = int(os.environ.get("ALERT_VISION_STRIDE", DEFAULT_STRIDE) or DEFAULT_STRIDE)
    backend = AlertVisionBackend(_initial_specs(), stride=stride, log_file=LOG_FILE)
    try:
        asyncio.run(backend.run())
    except KeyboardInterrupt:
        print(":: [Alert Vision] Shutdown signal received")


if __name__ == "__main__":
    main()
