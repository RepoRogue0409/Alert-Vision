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
    * CrowdShield analytics (``analytics.py``) score each frame: optical-flow
      turbulence/divergence feeding an EMA-smoothed risk (density / rate of
      change / convergence / turbulence weights) plus a physical density
      cross-check (4 people/m^2 -> HIGH, 6 -> CRITICAL with area calibration).
    * The published risk level uses a time-based debounce with hysteresis
      (escalate hold ~1.5s / de-escalate hold ~4s); alerts fire only on
      confirmed upward transitions and are cooldown-gated per camera.
    * HIGH / CRITICAL transitions append a row to crowdshield_alert_log.csv
      (Timestamp, CameraID, CameraLabel, RiskLevel, RiskScore, DetectionConfidence,
      People, ActionRequired, AlertID) via a background writer thread.

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
import socket
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Queue
from urllib.parse import parse_qs, urlparse

# FFmpeg capture options must be configured before the OpenCV FFMPEG backend is
# imported so RTSP/HTTP streams pick up TCP transport + low-latency flags early.
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    "rtsp_transport;tcp|fflags;nobuffer|probesize;32768|analyzeduration;0",
)
import cv2  # noqa: E402  (must follow OPENCV_FFMPEG_CAPTURE_OPTIONS)
import numpy as np
import torch
from ultralytics import YOLO

from analytics import (
    CRITICAL_AT,
    LiveAnalyzer,
    combine_levels,
    compute_flow_signal,
    confidence_live,
    density_forced_level,
    density_history_vote,
    density_per_sqm,
    estimate_ground_area,
    flow_velocity,
    level_for,
    NOMINAL_SAMPLE_DT,
)

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
SEND_TIMEOUT_SEC = 1.0
MAX_MJPEG_CLIENTS = 12
MAX_WS_MESSAGE_BYTES = 1_000_000
MAX_WS_MESSAGES_PER_SEC = 60

LOG_FILE = "crowdshield_alert_log.csv"
BACKEND_VERSION = "2.1.0"
LOG_HEADERS = [
    "Timestamp",
    "CameraID",
    "CameraLabel",
    "RiskLevel",
    "RiskScore",
    "DetectionConfidence",
    "People",
    "ActionRequired",
    "AlertID",
]
LOG_MAX_BYTES = 5 * 1024 * 1024
ALERT_HISTORY_SIZE = 20
YOLO_MODEL_PATH = "yolov8n.pt"

PERSON_CLASS_ID = 0
PERSON_CONFIDENCE_THRESHOLD = 0.25
TRACKER_CONFIG = "bytetrack.yaml"

DEBOUNCE_WINDOW = 12

RISK_LEVELS = ("SAFE", "MODERATE", "HIGH", "CRITICAL")
RISK_RANK = {level: index for index, level in enumerate(RISK_LEVELS)}

HEADCOUNT_HIGH_THRESHOLD = 6.0
DENSITY_CRITICAL_THRESHOLD = 0.20
TURBULENCE_CRITICAL_THRESHOLD = 3.5
MOTION_THRESHOLD = 1.0

RISK_SCORE_THRESHOLDS = {"moderate": 0.35, "high": 0.55, "critical": 0.75}
DEFAULT_WEIGHTS = {"density": 0.4, "rate_of_change": 0.2, "convergence": 0.2, "turbulence": 0.2}
DEFAULT_ALERT_THRESHOLD = 0.55
DEFAULT_MIN_PEOPLE = 3

# Time-based alert hysteresis (seconds): how long the instant level must hold
# before it becomes the confirmed level (escalate hold / de-escalate hold).
ESCALATE_HOLD_SECONDS = 1.5
DEESCALATE_HOLD_SECONDS = 4.0
ALERT_COOLDOWN_SECONDS = 30.0
# I-A6: after an alert packet has been queued to at least one client, how long
# to wait for an explicit browser receipt (alert_ack) before popping it anyway.
# Distinguishes queue acceptance (backend) from browser receipt (frontend).
ALERT_RECEIPT_GRACE_SECONDS = 10.0
# No WS client ever accepted the alert -> release it after this long; the
# alert_history replay still delivers it to the next client that connects.
ALERT_NO_CLIENT_ABANDON_SECONDS = 60.0
MAX_CSV_WRITE_RETRIES = 3

RECONNECT_INTERVAL = 2.0
RECONNECT_MAX_DELAY = 30.0
SCORE_GAMMA = 1.12

ALLOWED_ORIGINS = (
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://localhost",
    "http://127.0.0.1",
)


def _ensure_log_file(path: str) -> None:
    """Create the log file with the current schema, migrating legacy headers."""
    expected_line = ",".join(LOG_HEADERS)
    if not os.path.exists(path):
        with open(path, "w", newline="") as handle:
            csv.writer(handle).writerow(LOG_HEADERS)
        return
    try:
        needs_migration = False
        legacy_rows = []
        with open(path, "r", newline="") as handle:
            first_line = handle.readline().strip()
            if first_line and first_line.replace(" ", "") != expected_line.replace(" ", ""):
                needs_migration = True
                legacy_rows = [row for row in csv.reader(handle) if row]
        if needs_migration:
            with open(path, "w", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(LOG_HEADERS)
                for row in legacy_rows:
                    values = list(row) + [""] * len(LOG_HEADERS)
                    values = values[: len(LOG_HEADERS)]
                    migrated = [
                        values[0] if len(values) > 0 else "",
                        "",
                        "",
                        values[1] if len(values) > 1 else "",
                        values[2] if len(values) > 2 else "",
                        values[2] if len(values) > 2 else "",
                        "0",
                        values[3] if len(values) > 3 else "",
                        "0",
                    ]
                    writer.writerow(migrated)
    except Exception:
        # Never let log bootstrap fail the backend.
        pass


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


def _optional_float(value):
    """Parse an optional finite float config value (``None`` when absent/invalid)."""
    if value is None or value == "":
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(numeric):
        return None
    return float(numeric)


def _worst_level(levels) -> str:
    worst = "SAFE"
    for level in levels:
        if RISK_RANK.get(level, 0) > RISK_RANK.get(worst, 0):
            worst = level
    return worst


def _validate_source(source) -> str:
    """Return a normalized source string/int or raise ValueError.

    Allowed: int webcam index (>= 0) or stream URLs starting with
    http://, https:// or rtsp://. Everything else (file://, concat:, pipes/,
    plain paths, ...) is rejected.
    """
    if isinstance(source, bool):
        source = int(source)
    if isinstance(source, int):
        if source < 0:
            raise ValueError("webcam index must be >= 0")
        return source
    if isinstance(source, float):
        source = int(source)
    text = str(source if source is not None else "").strip()
    if not text:
        raise ValueError("empty source")
    lowered = text.lower()
    if lowered.startswith(("file://", "concat:", "pipes/", "rtsp/", "http/", "https/", "udp://", "tcp://")):
        raise ValueError(f"unsupported source: {text.split('://')[0]}")
    if not (lowered.startswith("http://") or lowered.startswith("https://") or lowered.startswith("rtsp://")):
        raise ValueError("source must start with http://, https:// or rtsp://")
    if "\x00" in text:
        raise ValueError("source contains invalid characters")
    return text


def _source_display(source) -> str:
    """Human-readable, credential-masked source label."""
    if isinstance(source, bool):
        source = int(source)
    if isinstance(source, int):
        return f"Webcam {source}"
    text = str(source) if source is not None else ""
    if not text:
        return "Unassigned"
    masked = text
    if "://" in masked:
        scheme, rest = masked.split("://", 1)
        if "@" in rest:
            userinfo, hostpart = rest.split("@", 1)
            masked = f"{scheme}:***@{hostpart}"
    return masked


def _sanitize(value):
    """Recursively replace NaN / infinite floats with safe finite numbers."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return float(np.nan_to_num(value, nan=0.0, posinf=1.0, neginf=0.0))
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        return {str(k): _sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Per-camera worker (isolated capture handle + tracker + debounce state)
# ---------------------------------------------------------------------------
class CameraPipeline:
    """One camera channel running in its own daemon worker thread."""

    def __init__(
        self,
        cam_id: str,
        label: str,
        source,
        state: "SharedSystemState",
        area_sqm: float = None,
        height_m: float = None,
        tilt_deg: float = None,
        hfov_deg: float = None,
        vfov_deg: float = None,
        min_people: float = None,
    ) -> None:
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
        self.prev_flow_time = None
        self._last_processed_at = None
        self.latest_frame = None
        self.latest_jpeg = None
        # I-A2/I-A3: `online`/`frame_time` are written by the worker and read
        # by health/snapshot/MJPEG threads -> guard with capture_lock.  The
        # worker never holds capture_lock while calling into shared state that
        # itself takes capture_lock, so no lock ordering hazard exists.
        self.online = False
        self.last_reconnect_attempt = 0.0
        self.reconnect_attempts = 0
        self.frame_time = 0.0

        # STEP 2: connection diagnostics surfaced in every telemetry packet.
        # I-A4: mutated by the worker, read by the broadcast loop -> own lock
        # so a packet never mixes state from one attempt with another's error.
        self._conn_lock = threading.Lock()
        self._conn_state = "loading_model"
        self._conn_error = None
        self._conn_started = time.monotonic()
        self._resolved_source = source
        self._last_open_error = None

        self.seen_ids = set()
        self.risk_history = deque(maxlen=DEBOUNCE_WINDOW)
        # I-B7: density-forced levels are majority-voted so a single noisy
        # detection cannot clear an otherwise sustained high-density state.
        self.density_level_history = deque(maxlen=8)
        self._last_density_level = None
        self.current_risk = "SAFE"
        self.alert_state = "SAFE"
        self.risk_score = 0.0
        self.alert_id = 0
        self.cooldown_until = 0.0
        self._risk_since = time.monotonic()
        self._escalation_since = None
        self._deescalation_since = None
        self.alert_queue: deque = deque(maxlen=ALERT_HISTORY_SIZE)
        self.log_error = False
        self.model_error = None

        self.area_sqm = float(area_sqm) if area_sqm else None
        if self.area_sqm is None and height_m is not None:
            geometry = estimate_ground_area(float(height_m), float(tilt_deg or 0.0), float(hfov_deg or 70.0), float(vfov_deg or 55.0))
            if geometry is not None:
                self.area_sqm = float(geometry[0])
        self.min_people = float(min_people) if min_people is not None else float(DEFAULT_MIN_PEOPLE)
        self.analyzer = LiveAnalyzer(window=50, weights=None, min_people=self.min_people, critical_at=CRITICAL_AT)

        self.packet = self._offline_packet()

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        if self.thread is not None:
            return
        self.thread = threading.Thread(target=self._run, name=f"cam-{self.id}", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        stopped = True
        if self.thread is not None:
            self.thread.join(timeout=5.0)
            # I-A2: if the worker is still alive (stuck in a slow read) do NOT
            # touch the capture handle from this thread - releasing it under a
            # concurrent cap.read() is undefined behaviour. The daemon thread
            # dies with the process; the handle is reclaimed then.
            stopped = not self.thread.is_alive()
            if stopped:
                self.thread = None
        if stopped:
            with self.capture_lock:
                self._source_dirty = False
                if self.cap is not None:
                    with contextlib.suppress(Exception):
                        self.cap.release()
                    self.cap = None
                self.online = False
        else:
            print(
                f":: [Alert Vision] {self.id} worker did not stop within 5s; "
                "capture handle left to the worker thread"
            )

    def set_source(self, source) -> None:
        """Queue a hot-swap; only the reader thread touches the handle."""
        with self.capture_lock:
            current = self._pending_source if self._source_dirty else self.source
            try:
                same = normalize_source(source) == normalize_source(current)
            except Exception:
                same = False
            if same:
                # idempotent hot-swap: ignore no-op source changes
                return
            self._pending_source = source
            self._source_dirty = True
        print(f":: [Alert Vision] {self.id} hot-swap queued -> {_source_display(source)}")

    # -- capture -------------------------------------------------------------
    @staticmethod
    def _open_capture(source, timeout_ms: int = 4000):
        if isinstance(source, str) and not source.strip():
            return None
        if isinstance(source, str) and source.strip().lower().startswith("rtsp://"):
            # Force TCP transport (+ low-latency flags) before the capture backend uses it.
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|fflags;nobuffer|probesize;32768|analyzeduration;0"
        cap = None
        if isinstance(source, int):
            if os.name == "nt":
                cap = cv2.VideoCapture(source, cv2.CAP_DSHOW)
            if cap is None or not cap.isOpened():
                if cap is not None:
                    with contextlib.suppress(Exception):
                        cap.release()
                cap = cv2.VideoCapture(source)
        elif isinstance(source, str):
            # Single FFMPEG attempt with explicit open/read timeouts (STEP 2: fast camera connect).
            params = []
            if hasattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC"):
                params.extend([int(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC), int(timeout_ms)])
            if hasattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC"):
                params.extend([int(cv2.CAP_PROP_READ_TIMEOUT_MSEC), int(timeout_ms)])
            cap = cv2.VideoCapture(source, cv2.CAP_FFMPEG, params) if params else cv2.VideoCapture(source, cv2.CAP_FFMPEG)
            # No second fallback attempt: the default-backend open can hang ~30s on dead hosts.
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

    @staticmethod
    def _candidate_sources(source):
        """Known phone/camera alternate stream paths, primary URL first."""
        if not isinstance(source, str) or "://" not in source:
            return [source]
        parsed = urlparse(source)
        if parsed.scheme not in ("http", "https"):
            return [source]
        path = parsed.path or "/"
        candidates = [source]
        alt_paths = []
        if parsed.port == 4747:
            # DroidCam phone app endpoints
            alt_paths.extend(["/mjpegfeed?640x480", "/mjpegfeed"])
        elif path in ("/video", "/videofeed"):
            # IP Webcam / generic MJPEG endpoints
            alt_paths.append("/videofeed" if path == "/video" else "/video")
        for alt in alt_paths:
            alt_path, _, alt_query = alt.partition("?")
            if alt_path == path and alt_query == (parsed.query or ""):
                continue
            candidates.append(parsed._replace(path=alt_path, query=alt_query).geturl())
        return candidates

    def _open_with_alternates(self, source):
        """Try the primary stream URL, then known alternates with short timeouts."""
        candidates = self._candidate_sources(source)
        for index, candidate in enumerate(candidates):
            with self.capture_lock:
                if self._source_dirty:
                    # A newer source was queued; abandon this stale attempt early.
                    return None
            cap = self._open_capture(candidate, timeout_ms=4000 if index == 0 else 2000)
            if cap is not None:
                with self._conn_lock:
                    # I-A12: every fresh attempt clears the previous source's error.
                    self._last_open_error = None
                    self._resolved_source = candidate
                if candidate != source:
                    print(
                        f":: [Alert Vision] {self.id} alternate stream path worked -> "
                        f"{_source_display(candidate)}"
                    )
                return cap
            if index == 0 and len(candidates) > 1:
                # Only try alternate paths when the host itself answers. If it is
                # unreachable, every alternate would just burn another timeout and
                # delay the readable FAILED state.
                reason = self._source_error_hint(source, timeout=0.6)
                if reason is not None:
                    with self._conn_lock:
                        self._last_open_error = reason
                        self._resolved_source = source
                    return None
        with self._conn_lock:
            self._resolved_source = source
        return None

    def _set_connection(self, state: str, error=None) -> None:
        now = time.monotonic()
        with self._conn_lock:
            if state != self._conn_state:
                self._conn_state = state
                # Keep the attempt clock running into "failed" so elapsed_ms reports
                # how long the failed connect attempt took.
                if state in ("loading_model", "connecting", "connected"):
                    self._conn_started = now
            if state in ("connecting", "connected", "loading_model"):
                # A fresh attempt starts clean; stale errors belong to the last failure.
                self._conn_error = None
            if error:
                self._conn_error = str(error)

    def _connection_payload(self) -> dict:
        # I-A4: one consistent snapshot of {state, error, elapsed, url}.
        with self._conn_lock:
            state = str(self._conn_state)
            error = self._conn_error
            started = self._conn_started
            resolved = self._resolved_source
        elapsed_ms = max(0.0, (time.monotonic() - started) * 1000.0)
        return {
            "state": state,
            "error": error,
            "elapsed_ms": float(round(elapsed_ms, 1)),
            "resolved_url_masked": _source_display(resolved),
        }

    def _connection_error_hint(self) -> str:
        """Best-effort readable reason for a failed stream open."""
        with self._conn_lock:
            last_error = self._last_open_error
        if last_error:
            return last_error
        return self._source_error_hint(self.source) or (
            "stream open failed: wrong path, camera busy (one-client limit), "
            "or unsupported stream"
        )

    @staticmethod
    def _source_error_hint(source, timeout: float = 1.5):
        """Return a readable failure reason, or None when the host answers."""
        if isinstance(source, int):
            return f"webcam index {source} not available"
        if not isinstance(source, str) or "://" not in source:
            return "invalid stream URL"
        parsed = urlparse(source)
        host = parsed.hostname
        if not host:
            return "invalid stream URL"
        if parsed.port:
            port = int(parsed.port)
        elif parsed.scheme == "https":
            port = 443
        elif parsed.scheme == "rtsp":
            port = 554
        else:
            port = 80
        try:
            probe = socket.create_connection((host, port), timeout=timeout)
            probe.close()
            return None
        except socket.timeout:
            return "timeout: host did not respond (camera off / wrong IP / firewall)"
        except OSError as error:
            return f"connection refused/unreachable: {error}"

    def _apply_pending_source(self) -> None:
        with self.capture_lock:
            if not self._source_dirty:
                return
            self._source_dirty = False
            source = self._pending_source
            self._pending_source = None
        # Open outside the lock so a slow stream does not block the whole channel.
        with self._conn_lock:
            self._conn_started = time.monotonic()
        self._set_connection("connecting", None)
        # I-A12: a new source invalidates the previous attempt's error reason.
        with self._conn_lock:
            self._last_open_error = None
        new_cap = self._open_with_alternates(source)
        with self.capture_lock:
            self.source = source
            if self.cap is not None:
                with contextlib.suppress(Exception):
                    self.cap.release()
                self.cap = None
            self.cap = new_cap
            self.online = self.cap is not None
        if self.online:
            self._set_connection("connected", None)
        else:
            self._set_connection("failed", self._connection_error_hint())

        self.frame_counter = 0
        self.prev_small = None
        self.prev_flow_time = None
        self._last_processed_at = None
        self.risk_history.clear()
        self.density_level_history.clear()
        self._last_density_level = None
        with contextlib.suppress(Exception):
            self.analyzer.reset()
        self.current_risk = "SAFE"
        self.alert_state = "SAFE"
        self.risk_score = 0.0
        self._risk_since = time.monotonic()
        self._escalation_since = None
        self._deescalation_since = None
        if self.model is not None:
            # drop stale ByteTrack state so a new stream starts fresh IDs
            with contextlib.suppress(Exception):
                self.model.predictor = None
        status = "ONLINE" if self.online else "OFFLINE"
        print(f":: [Alert Vision] {self.id} hot-swap applied -> {_source_display(self.source)} [{status}]")

    def _try_reconnect(self) -> None:
        if self.stop_event.is_set():
            return
        now = time.time()
        if now - self.last_reconnect_attempt < self._reconnect_delay():
            return
        self.last_reconnect_attempt = now
        with self.capture_lock:
            if self._source_dirty:
                return
            if self.cap is not None and self.cap.isOpened():
                self.online = True
                self.reconnect_attempts = 0
                return
            if self.cap is not None:
                with contextlib.suppress(Exception):
                    self.cap.release()
                self.cap = None
        # Open OUTSIDE the capture lock so a slow stream does not block the whole channel.
        with self._conn_lock:
            self._conn_started = time.monotonic()
        self._set_connection("connecting", None)
        new_cap = self._open_with_alternates(self.source)
        with self.capture_lock:
            if self.stop_event.is_set():
                return
            self.cap = new_cap
            self.online = new_cap is not None and new_cap.isOpened()
            if self.online:
                self.reconnect_attempts = 0
                self.prev_small = None
            else:
                # B4: grow the backoff on every failed attempt (2s -> 4s -> 8s ... 30s).
                self.reconnect_attempts = min(self.reconnect_attempts + 1, 6)
        if self.online:
            self._set_connection("connected", None)
        else:
            self._set_connection("failed", self._connection_error_hint())

    def _reconnect_delay(self) -> float:
        if self.reconnect_attempts <= 0:
            return RECONNECT_INTERVAL
        return float(min(RECONNECT_INTERVAL * (2.0 ** min(self.reconnect_attempts, 5)), RECONNECT_MAX_DELAY))

    def read_frame(self):
        # The handle is only ever released by THIS worker thread (stop() waits
        # for the thread to exit first), so reading outside the lock cannot race
        # a concurrent release (I-A3). Flag writes take the lock because health
        # and snapshot readers observe them.
        with self.capture_lock:
            cap = self.cap
        if cap is None or not cap.isOpened():
            with self.capture_lock:
                self.online = False
            return None
        grabbed, frame = cap.read()
        if not grabbed or frame is None:
            with self.capture_lock:
                self.online = False
                if self.cap is cap:
                    with contextlib.suppress(Exception):
                        cap.release()
                    self.cap = None
            return None
        with self.capture_lock:
            self.online = True
            self.frame_time = time.monotonic()
        return frame

    # -- worker loop ---------------------------------------------------------
    def _load_model(self) -> None:
        """Load YOLO in a background thread so the capture can connect in parallel."""
        while not self.stop_event.is_set() and self.model is None:
            try:
                model_path = os.environ.get("ALERT_VISION_MODEL") or YOLO_MODEL_PATH
                loaded = YOLO(model_path)
                self.model_path = model_path
                self.model = loaded
                self.model_error = None
                print(f":: [Alert Vision] {self.id} model loaded -> {model_path}")
                with self._conn_lock:
                    still_loading = self._conn_state == "loading_model"
                if still_loading:
                    self._set_connection("connecting", None)
                return
            except Exception as error:
                self.model_error = f"model_load_failed: {error}"
                print(
                    f":: [Alert Vision] {self.id} model load failed -> {error} "
                    f"(retrying in 30s)"
                )
                if self.stop_event.wait(30.0):
                    return

    def _run(self) -> None:
        # STEP 2: open the capture immediately; the model loads in a parallel thread
        # so torch/ultralytics startup never delays the stream connection.
        if self.model is None:
            threading.Thread(
                target=self._load_model, name=f"cam-{self.id}-model", daemon=True
            ).start()
        while not self.stop_event.is_set():
            self._apply_pending_source()
            frame = self.read_frame()
            if frame is None:
                with self.frame_lock:
                    self.latest_frame = None
                    self.latest_jpeg = None
                # I-A11: attempt the reconnect FIRST so the offline packet we
                # publish reflects this tick's real connection state/error
                # instead of the previous attempt's leftovers.
                self._try_reconnect()
                self._publish(self._offline_packet())
                time.sleep(0.1)
                continue

            if self.model is None:
                # B2: never run inference without a model. Keep the transport warm
                # and relay the raw frame so the tile shows video while torch loads.
                if self.model_error is None:
                    self.model_error = "model_unavailable"
                    print(f":: [Alert Vision] {self.id} model unavailable -> frames paused")
                self._set_connection("loading_model", self.model_error)
                self.current_risk = "UNKNOWN"
                self._store_raw_frame(frame)
                packet = self._offline_packet()
                packet.update(
                    risk_level="UNKNOWN",
                    risk_score=0.0,
                    alert=False,
                    action="NONE",
                    alert_id=0,
                    online=True,
                    camera_online=True,
                    last_frame_age_ms=0.0,
                )
                self._publish(packet)
                time.sleep(0.05)
                continue

            self._set_connection("connected", None)
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
            fps = self._capture_fps()
            if fps > 0.0:
                frame_period = 1.0 / fps
                elapsed = time.monotonic() - self.frame_time
                if elapsed < frame_period:
                    time.sleep(frame_period - elapsed)
            time.sleep(0.005)

    def _capture_fps(self) -> float:
        with self.capture_lock:
            cap = self.cap
        if cap is None or not cap.isOpened():
            return 0.0
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        return fps if fps > 0.0 else 0.0

    def _store_raw_frame(self, frame) -> None:
        """Relay an un-annotated frame while the model is still loading."""
        with self.frame_lock:
            self.latest_frame = frame
            with contextlib.suppress(Exception):
                ok_jpeg, buffer = cv2.imencode(
                    ".jpg",
                    frame,
                    [int(cv2.IMWRITE_JPEG_QUALITY), int(JPEG_QUALITY)],
                )
                self.latest_jpeg = (
                    buffer.tobytes() if ok_jpeg and buffer is not None else None
                )

    def _publish(self, packet: dict) -> None:
        with self.frame_lock:
            self.packet = packet

    def grab_jpeg(self, quality: int = JPEG_QUALITY):
        with self.frame_lock:
            frame = self.latest_frame
            jpeg = self.latest_jpeg
        if jpeg is not None and quality == JPEG_QUALITY:
            return jpeg
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

        # I-B1/I-B2: measured interval between ANALYSED frames (includes stride).
        processed_at = time.monotonic()
        if self._last_processed_at is None:
            processed_dt = NOMINAL_SAMPLE_DT
        else:
            processed_dt = min(max(processed_at - float(self._last_processed_at), 0.001), 2.0)
        self._last_processed_at = processed_at

        config = self.state.config_snapshot()
        detections = []
        confidences = []
        occupied_area = 0.0

        _conf = float(os.environ.get("ALERT_VISION_CONF") or PERSON_CONFIDENCE_THRESHOLD)
        conf = float(min(0.95, max(0.05, _conf)))
        results = self.model.track(
            frame,
            persist=True,
            tracker=os.environ.get("ALERT_VISION_TRACKER") or TRACKER_CONFIG,
            classes=[PERSON_CLASS_ID],
            conf=conf,
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
                if confidence < conf:
                    continue
                x1, y1, x2, y2 = (float(box[0]), float(box[1]), float(box[2]), float(box[3]))
                box_w = max(0.0, x2 - x1)
                box_h = max(0.0, y2 - y1)
                occupied_area += box_w * box_h
                confidences.append(confidence)
                person_id = int(track_id) if track_id is not None else None
                if person_id is not None:
                    if len(self.seen_ids) > 512:
                        self.seen_ids.clear()
                    self.seen_ids.add(person_id)
                detections.append((x1, y1, x2, y2, confidence, person_id))

        people_now = len(detections)
        annotated = self._annotate(frame, detections)
        frame_area = float(width * height)
        density = float(np.clip(occupied_area / frame_area, 0.0, 1.0)) if frame_area > 0 else 0.0
        avg_confidence = float(np.mean(confidences)) if confidences else 0.0

        weights = config["weights"]
        if people_now <= 0:
            # no people -> suppress motion entirely and hard-reset the analyzer
            self.prev_small = None
            self.prev_flow_time = None
            turbulence = 0.0
            divergence = 0.0
            velocity = 0.0
        else:
            turbulence, divergence, velocity = self._flow_metrics(frame)

        risk_score = self.analyzer.add(
            people_now,
            turbulence,
            divergence,
            w_density=weights.get("density"),
            w_rate_of_change=weights.get("rate_of_change"),
            w_convergence=weights.get("convergence"),
            w_turbulence=weights.get("turbulence"),
            min_people=self.min_people,
            dt=processed_dt,
            area_sqm=self.area_sqm,
        )
        # I-B9: ONE source of truth. validate_config() mirrors alert_threshold
        # into risk_thresholds["high"], so formula HIGH and the alert gate can
        # never disagree.
        formula_level = level_for(
            risk_score,
            threshold=float(config["alert_threshold"]),
            critical_at=float(config["risk_thresholds"].get("critical", CRITICAL_AT)),
        )
        raw_density_level = density_forced_level(people_now, self.area_sqm) if self.area_sqm else None
        # I-B7: escalate immediately; hold a forced level until the majority
        # vote agrees it has cleared (avoids one-frame flapping).
        self.density_level_history.append(raw_density_level or "SAFE")
        voted_clear = density_history_vote(self.density_level_history, agree_needed=4)
        if raw_density_level is not None:
            density_level = raw_density_level
        elif self._last_density_level is not None and voted_clear is None:
            density_level = self._last_density_level
        else:
            density_level = None
        self._last_density_level = density_level
        instant_level = combine_levels(formula_level, density_level)
        density_psm = density_per_sqm(people_now, self.area_sqm) if self.area_sqm else 0.0
        forecast = self.analyzer.forecast_estimate(
            critical_at=float(config["risk_thresholds"].get("critical", CRITICAL_AT))
        )

        self.risk_history.append(instant_level)
        now_mono = time.monotonic()
        instant_rank = RISK_RANK.get(instant_level, RISK_RANK["SAFE"])
        confirmed_rank = RISK_RANK.get(self.current_risk, RISK_RANK["SAFE"])
        if instant_level == self.current_risk:
            self._risk_since = now_mono
            self._escalation_since = None
            self._deescalation_since = None
        elif instant_rank > confirmed_rank:
            if self._escalation_since is None:
                self._escalation_since = now_mono
            if now_mono - self._escalation_since >= ESCALATE_HOLD_SECONDS:
                self.current_risk = instant_level
                self._risk_since = now_mono
                self._escalation_since = None
        elif instant_rank < confirmed_rank:
            if self._deescalation_since is None:
                self._deescalation_since = now_mono
            if now_mono - self._deescalation_since >= DEESCALATE_HOLD_SECONDS:
                self.current_risk = instant_level
                self._risk_since = now_mono
                self._deescalation_since = None
                # F2/A6: on downgrade the alert state advances so a later rise fires again.
                self.alert_state = self.current_risk

        alert_event = None
        alert_fired = False
        alert_level = str(self.current_risk or "SAFE")
        if alert_level in ("HIGH", "CRITICAL"):
            if alert_level != str(self.alert_state or "SAFE"):
                # A new (higher) level than the last alerted level fires immediately.
                # Repeats of the SAME level are governed by the cooldown only.
                should_fire = True
            else:
                should_fire = time.monotonic() >= self.cooldown_until
            if should_fire:
                density_forced = density_level is not None and str(density_level) == alert_level
                level_threshold = float(config.get("alert_threshold") or DEFAULT_ALERT_THRESHOLD)
                if density_forced or float(risk_score) >= level_threshold:
                    action = "DISPATCH" if self.current_risk == "CRITICAL" else "ESCALATE"
                    self.alert_id += 1
                    alert_event = {
                        "alert_id": self.alert_id,
                        "action": action,
                        "risk_level": self.current_risk,
                        "confidence": avg_confidence,
                        "timestamp": time.time() * 1000.0,
                        # I-A6: delivery lifecycle. queued -> (dispatched to N
                        # client queues) -> acked by browser / grace expired.
                        "_queued_at": time.monotonic(),
                        "_dispatched_at": None,
                        "_accepted": 0,
                        "_acked": False,
                    }
                    self.alert_state = self.current_risk
                    self.cooldown_until = time.monotonic() + ALERT_COOLDOWN_SECONDS
                    with self.frame_lock:
                        self.alert_queue.append(alert_event)
                    self.state.log_alert(
                        self.id,
                        self.current_risk,
                        avg_confidence,
                        risk_score=float(risk_score),
                        people=int(people_now),
                        alert_id=int(alert_event["alert_id"]),
                        label=self.label,
                    )
                    self.state.record_alert(self.id, alert_event)
                    alert_fired = True
        self.risk_score = float(risk_score)

        with self.frame_lock:
            self.latest_frame = annotated
            with contextlib.suppress(Exception):
                ok_jpeg, jpeg_buffer = cv2.imencode(
                    ".jpg",
                    annotated,
                    [int(cv2.IMWRITE_JPEG_QUALITY), int(JPEG_QUALITY)],
                )
                self.latest_jpeg = jpeg_buffer.tobytes() if ok_jpeg and jpeg_buffer is not None else None
        now_ms = time.time() * 1000.0
        frame_age_ms = 0.0
        if self.frame_time > 0.0:
            frame_age_ms = max(0.0, (time.monotonic() - self.frame_time) * 1000.0)
        terms = dict(getattr(self.analyzer, "last_terms", {}) or {})
        return {
            "id": self.id,
            "label": self.label,
            "source": _source_display(self.source),
            "source_value_masked": _source_display(self.source),
            "online": True,
            "people_now": int(people_now),
            # I-B8: backward-compatible field retained, but explicitly labelled.
            "unique_ids": int(len(self.seen_ids)),
            "track_ids_seen": int(len(self.seen_ids)),
            "unique_ids_meaning": "cumulative ByteTrack IDs (includes re-identifications; NOT unique people)",
            "tracks": [int(d[5]) for d in detections if d[5] is not None],
            "risk_level": self.current_risk,
            "risk_score": float(risk_score),
            "confidence": float(confidence_live(len(self.analyzer.risk), confidences)),
            "density": float(density),
            "density_per_sqm": float(density_psm),
            "density_calibrated": bool(self.area_sqm),
            "turbulence": float(turbulence),
            "convergence": float(terms.get("convergence", 0.0)),
            "risk_density": float(terms.get("density", 0.0)),
            "risk_rate_of_change": float(terms.get("rate_of_change", 0.0)),
            "risk_convergence": float(terms.get("convergence", 0.0)),
            "risk_turbulence": float(terms.get("turbulence", 0.0)),
            "velocity": float(velocity),
            "risk_level_formula": formula_level,
            "risk_level_density": density_level or "SAFE",
            "forecast_eta_s": float(forecast["eta_s"]) if forecast["eta_s"] is not None else None,
            "forecast_estimate": dict(forecast),
            "area_sqm": float(self.area_sqm) if self.area_sqm else None,
            "min_people": float(self.min_people),
            "timestamp": float(now_ms),
            "last_frame_age_ms": float(frame_age_ms),
            "alert": alert_fired,
            "action": alert_event["action"] if alert_event is not None else "NONE",
            "alert_id": int(alert_event["alert_id"]) if alert_event is not None else 0,
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
        """Return ``(turbulence, divergence, velocity)`` using the CrowdShield signal."""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        small = cv2.resize(gray, (0, 0), fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
        previous = self.prev_small
        if previous is None or previous.shape != small.shape:
            self.prev_small = small.copy()
            self.prev_flow_time = time.monotonic()
            return 0.0, 0.0, 0.0

        now_mono = time.monotonic()
        dt = now_mono - float(self.prev_flow_time) if self.prev_flow_time else 0.0
        self.prev_small = small.copy()
        self.prev_flow_time = now_mono
        flow = cv2.calcOpticalFlowFarneback(previous, small, None, 0.5, 3, 15, 3, 5, 1.2, 0)
        turbulence, divergence = compute_flow_signal(previous, small, dt=dt, flow=flow)
        # I-B3: publish pixels/SECOND, not pixels/frame (FPS/stride comparable).
        velocity = flow_velocity(flow, dt)
        return float(turbulence), float(divergence), float(velocity)

    # -- telemetry snapshots -------------------------------------------------
    def _offline_packet(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "source": _source_display(self.source),
            "source_value_masked": _source_display(self.source),
            "online": False,
            "people_now": 0,
            "unique_ids": int(len(self.seen_ids)),
            "track_ids_seen": int(len(self.seen_ids)),
            "unique_ids_meaning": "cumulative ByteTrack IDs (includes re-identifications; NOT unique people)",
            "tracks": [],
            "risk_level": "SAFE",
            "risk_score": 0.0,
            "confidence": 0.0,
            "density": 0.0,
            "density_per_sqm": 0.0,
            "density_calibrated": bool(self.area_sqm),
            "turbulence": 0.0,
            "convergence": 0.0,
            "risk_density": 0.0,
            "risk_rate_of_change": 0.0,
            "risk_convergence": 0.0,
            "risk_turbulence": 0.0,
            "velocity": 0.0,
            "risk_level_formula": "SAFE",
            "risk_level_density": "SAFE",
            "forecast_eta_s": None,
            "forecast_estimate": None,
            "area_sqm": float(self.area_sqm) if self.area_sqm else None,
            "min_people": float(self.min_people),
            "timestamp": float(time.time() * 1000.0),
            "last_frame_age_ms": 0.0,
            "alert": False,
            "action": "NONE",
            "camera_online": False,
            "log_error": bool(self.state.log_error) if self.state is not None else False,
            "model_error": self.model_error,
            "connection": self._connection_payload(),
        }

    def acknowledge_alert(self, alert_id=None) -> bool:
        """Pop the front alert as BROWSER-RECEIPTED (explicit alert_ack).

        Returns True when an alert was consumed.  ``alert_id`` (when given)
        must match the front alert - stale acks never eat a newer alert.
        """
        with self.frame_lock:
            if not self.alert_queue:
                return False
            event = self.alert_queue[0]
            if alert_id is not None and int(event.get("alert_id") or 0) != int(alert_id):
                return False
            event["_acked"] = True
            self.alert_queue.popleft()
        return True

    def front_alert_needs_dispatch(self) -> bool:
        """True when the front alert has not yet been queued to any client."""
        with self.frame_lock:
            if not self.alert_queue:
                return False
            return self.alert_queue[0].get("_dispatched_at") is None

    def mark_alert_dispatched(self, accepted: int) -> None:
        """Record QUEUE ACCEPTANCE: the alert packet entered N client queues."""
        with self.frame_lock:
            if not self.alert_queue:
                return
            event = self.alert_queue[0]
            if event.get("_dispatched_at") is None:
                event["_dispatched_at"] = time.monotonic()
                event["_accepted"] = max(0, int(accepted))

    def sweep_alert_queue(self) -> None:
        """Release front alerts whose receipt grace expired (I-A6).

        * browser acked            -> already popped by acknowledge_alert()
        * queued to clients, no
          browser receipt in 10s   -> pop (queue acceptance only)
        * never dispatched (no
          clients) for 60s         -> pop; history replay still covers it
        """
        now = time.monotonic()
        with self.frame_lock:
            while self.alert_queue:
                event = self.alert_queue[0]
                if event.get("_acked"):
                    self.alert_queue.popleft()
                    continue
                dispatched = event.get("_dispatched_at")
                if dispatched is not None and now - dispatched >= ALERT_RECEIPT_GRACE_SECONDS:
                    print(
                        f":: [Alert Vision] {self.id} alert {event.get('alert_id')} "
                        f"queue-accepted (clients={event.get('_accepted')}) but no browser "
                        f"receipt in {ALERT_RECEIPT_GRACE_SECONDS:.0f}s -> releasing"
                    )
                    self.alert_queue.popleft()
                    continue
                if dispatched is None and now - float(event.get("_queued_at") or now) >= ALERT_NO_CLIENT_ABANDON_SECONDS:
                    print(
                        f":: [Alert Vision] {self.id} alert {event.get('alert_id')} "
                        "never dispatchable (no WS clients) -> releasing; "
                        "history replay still covers it"
                    )
                    self.alert_queue.popleft()
                    continue
                break

    def snapshot(self) -> dict:
        with self.frame_lock:
            packet = dict(self.packet)
            event = None
            if self.alert_queue:
                event = self.alert_queue[0]
            frame_age_ms = 0.0
            if self.online and self.frame_time > 0.0:
                frame_age_ms = max(0.0, (time.monotonic() - self.frame_time) * 1000.0)
        if event is not None:
            packet["alert"] = True
            packet["action"] = event["action"]
            packet["risk_level"] = event["risk_level"]
            packet["confidence"] = float(event["confidence"])
            packet["alert_id"] = int(event.get("alert_id") or 0)
        else:
            packet["alert"] = False
            packet["action"] = "NONE"
            packet["alert_id"] = 0
        packet["label"] = self.label
        packet["source"] = _source_display(self.source)
        packet["source_value_masked"] = _source_display(self.source)
        packet["online"] = bool(self.online)
        packet["camera_online"] = bool(self.online)
        packet["last_frame_age_ms"] = float(frame_age_ms)
        packet["log_error"] = bool(self.state.log_error) if self.state is not None else False
        packet["model_error"] = self.model_error
        packet["connection"] = self._connection_payload()
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
        self._retry_queue: Queue = Queue(maxsize=500)
        self._writer_thread = threading.Thread(target=self._log_writer, name="crowdshield-log-writer", daemon=True)
        self._writer_thread.start()
        self.alert_history: deque = deque(maxlen=ALERT_HISTORY_SIZE)
        self.log_error = False
        _ensure_log_file(log_file)
        if specs:
            self.apply_cameras(specs, initial=True)

    # -- logging -------------------------------------------------------------
    def _log_writer(self) -> None:
        while True:
            item = self._retry_queue.get()
            if item is None:
                break
            # I-A8: rows carry a retry counter so a persistently failing write
            # (disk full, permissions) gives up after MAX_CSV_WRITE_RETRIES
            # instead of spinning forever, and surfaces log_error to the UI.
            if isinstance(item, tuple):
                row, retries = item
            else:
                row, retries = item, 0
            try:
                if os.path.exists(self.log_file) and os.path.getsize(self.log_file) > LOG_MAX_BYTES:
                    with contextlib.suppress(Exception):
                        os.replace(self.log_file, self.log_file + ".1")
                with open(self.log_file, "a", newline="") as handle:
                    csv.writer(handle).writerow(row)
                self.log_error = False
            except Exception as error:
                if retries >= MAX_CSV_WRITE_RETRIES:
                    self.log_error = True
                    print(
                        f":: [Alert Vision] CSV write failed {retries + 1}x, row dropped "
                        f"({type(error).__name__}): {error}"
                    )
                    continue
                with contextlib.suppress(Exception):
                    self._retry_queue.put((row, retries + 1))
                time.sleep(0.5)
        return

    def record_alert(self, cam_id: str, event: dict) -> None:
        with self.lock:
            self.alert_history.append(
                {
                    "alert_id": int(event.get("alert_id") or 0),
                    "camera_id": str(cam_id),
                    "camera_label": str(event.get("camera_label") or cam_id),
                    "risk_level": str(event.get("risk_level") or "HIGH"),
                    "action": str(event.get("action") or "ESCALATE"),
                    "confidence": float(event.get("confidence") or 0.0),
                    "timestamp": float(event.get("timestamp") or time.time() * 1000.0),
                }
            )

    # -- configuration -------------------------------------------------------
    def config_snapshot(self) -> dict:
        with self.lock:
            return {
                "weights": dict(self.weights),
                "alert_threshold": float(self.alert_threshold),
                "risk_thresholds": dict(self.risk_thresholds),
                "stride": int(self.stride),
            }

    def validate_config(self, payload: dict) -> dict:
        """Validate + clamp the runtime risk configuration (B9 / B10)."""
        weights = dict(self.weights)
        raw_weights = payload.get("weights")
        if isinstance(raw_weights, dict):
            for key, value in raw_weights.items():
                if key in weights:
                    try:
                        numeric = float(np.nan_to_num(float(value), nan=0.0, posinf=1.0, neginf=0.0))
                        weights[key] = float(min(1.0, max(0.0, numeric)))
                    except (TypeError, ValueError):
                        weights[key] = float(self.weights.get(key, DEFAULT_WEIGHTS[key]))

        alert_threshold = self.alert_threshold
        raw_threshold = payload.get("alert_threshold")
        if raw_threshold is not None:
            try:
                alert_threshold = float(np.nan_to_num(float(raw_threshold), nan=0.0, posinf=1.0, neginf=0.0))
                alert_threshold = float(min(1.0, max(0.0, alert_threshold)))
            except (TypeError, ValueError):
                raise ValueError("alert_threshold must be a number between 0 and 1")

        risk_thresholds = dict(self.risk_thresholds)
        raw_thresholds = payload.get("risk_thresholds")
        if isinstance(raw_thresholds, dict):
            for key in ("moderate", "high", "critical"):
                value = raw_thresholds.get(key, RISK_SCORE_THRESHOLDS.get(key))
                try:
                    risk_thresholds[key] = float(np.nan_to_num(float(value), nan=0.0, posinf=1.0, neginf=0.0))
                    risk_thresholds[key] = float(min(1.0, max(0.0, risk_thresholds[key])))
                except (TypeError, ValueError):
                    risk_thresholds[key] = float(RISK_SCORE_THRESHOLDS.get(key))
            moderate = risk_thresholds["moderate"]
            high = risk_thresholds["high"]
            critical = risk_thresholds["critical"]
            high = min(max(high, moderate), critical)
            critical = min(max(critical, high), 1.0)
            moderate = min(max(moderate, 0.0), high)
            risk_thresholds["moderate"] = moderate
            risk_thresholds["high"] = high
            risk_thresholds["critical"] = critical
            if not (moderate < high < critical):
                risk_thresholds = dict(RISK_SCORE_THRESHOLDS)

        # I-B9: ONE source of truth for HIGH. The UI slider (alert_threshold)
        # is authoritative when supplied; otherwise risk_thresholds.high is.
        # Mirroring prevents formula-HIGH and the alert gate from diverging.
        if raw_threshold is not None:
            alert_threshold = min(
                max(alert_threshold, risk_thresholds["moderate"] + 1e-4),
                risk_thresholds["critical"] - 1e-4,
            )
            risk_thresholds["high"] = float(alert_threshold)
        else:
            alert_threshold = float(risk_thresholds["high"])

        stride = self.stride
        raw_stride = payload.get("stride")
        if raw_stride is not None:
            try:
                stride = max(1, min(10, int(raw_stride)))
            except (TypeError, ValueError):
                raise ValueError("stride must be an integer between 1 and 10")

        return {
            "weights": weights,
            "alert_threshold": float(alert_threshold),
            "risk_thresholds": risk_thresholds,
            "stride": int(stride),
        }

    def update_config(self, payload: dict) -> None:
        validated = self.validate_config(payload)
        stride_changed = None
        with self.lock:
            old_stride = int(self.stride)
            self.weights = dict(validated["weights"])
            self.alert_threshold = float(validated["alert_threshold"])
            self.risk_thresholds = dict(validated["risk_thresholds"])
            self.stride = int(validated["stride"])
            # I-A10: compare the NEW stride against the OLD one (captured
            # before assignment) - the previous code compared after writing,
            # so real changes never logged.
            if payload.get("stride") is not None and int(self.stride) != old_stride:
                stride_changed = int(self.stride)
        if stride_changed is not None:
            print(f":: [Alert Vision] Inference stride -> {stride_changed}")

        cameras = payload.get("cameras")
        if isinstance(cameras, list):
            self.apply_cameras(cameras)

    # -- camera registry -----------------------------------------------------
    def apply_cameras(self, specs, initial: bool = False) -> None:
        normalized = []
        seen = set()
        seen_sources = set()
        for index, spec in enumerate(specs[:MAX_CAMERAS]):
            if not isinstance(spec, dict):
                continue
            cam_id = str(spec.get("id") or f"cam{index + 1}").strip() or f"cam{index + 1}"
            if cam_id in seen:
                continue
            source = normalize_source(spec.get("source"))
            try:
                _validate_source(source)
            except Exception:
                continue
            source_key = str(source)
            if source_key in seen_sources:
                raise ValueError(f"duplicate camera source: {_source_display(source)}")
            seen_sources.add(source_key)
            seen.add(cam_id)
            normalized.append(
                {
                    "id": cam_id,
                    "label": str(spec.get("label") or f"CAM {index + 1}").strip() or f"CAM {index + 1}",
                    "source": source,
                    "area_sqm": _optional_float(spec.get("area_sqm")),
                    "height_m": _optional_float(spec.get("height_m")),
                    "tilt_deg": _optional_float(spec.get("tilt_deg")),
                    "hfov": _optional_float(spec.get("hfov")),
                    "vfov": _optional_float(spec.get("vfov")),
                    "min_people": _optional_float(spec.get("min_people")),
                }
            )
        if not normalized:
            return

        removed = []
        with self.lock:
            existing = dict(self.cameras)
            wanted: dict[str, CameraPipeline] = {}
            new_pipelines = []
            for spec in normalized:
                pipeline = existing.get(spec["id"])
                if pipeline is None:
                    pipeline = CameraPipeline(
                        spec["id"],
                        spec["label"],
                        spec["source"],
                        self,
                        area_sqm=spec.get("area_sqm"),
                        height_m=spec.get("height_m"),
                        tilt_deg=spec.get("tilt_deg"),
                        hfov_deg=spec.get("hfov"),
                        vfov_deg=spec.get("vfov"),
                        min_people=spec.get("min_people"),
                    )
                    new_pipelines.append(pipeline)
                else:
                    pipeline.label = spec["label"]
                    if spec.get("area_sqm") is not None:
                        pipeline.area_sqm = float(spec["area_sqm"])
                    elif spec.get("height_m") is not None:
                        geometry = estimate_ground_area(
                            float(spec["height_m"]),
                            float(spec.get("tilt_deg") or 0.0),
                            float(spec.get("hfov") or 70.0),
                            float(spec.get("vfov") or 55.0),
                        )
                        pipeline.area_sqm = float(geometry[0]) if geometry is not None else pipeline.area_sqm
                    if spec.get("min_people") is not None:
                        pipeline.min_people = float(spec["min_people"])
                        pipeline.analyzer.min_people = pipeline.min_people
                    if normalize_source(pipeline.source) != normalize_source(spec["source"]):
                        pipeline.set_source(spec["source"])
                wanted[spec["id"]] = pipeline
            # I-A1: collect removals under the lock but STOP them OUTSIDE it -
            # stop() joins the worker for up to 5s, which would otherwise stall
            # every telemetry snapshot waiting on state.lock.
            removed = [
                (cam_id, pipeline)
                for cam_id, pipeline in existing.items()
                if cam_id not in wanted
            ]
            self.cameras = wanted

        for cam_id, pipeline in removed:
            pipeline.stop()
            print(f":: [Alert Vision] {cam_id} channel removed")

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
        if cam_id is not None and str(cam_id) != "":
            chosen = next((pipe for pipe in pipelines if pipe.id == str(cam_id)), None)
            if chosen is None:
                # unknown / removed camera -> never fall back to another channel
                return None
        else:
            chosen = pipelines[0]
        return chosen.grab_jpeg()

    # -- persistent ledger ---------------------------------------------------
    def log_alert(self, cam_id: str, risk_level: str, confidence: float, risk_score: float = 0.0, people: int = 0, alert_id: int = 0, label: str = "") -> None:
        action = "DISPATCH" if risk_level == "CRITICAL" else "ESCALATE"
        row = [
            time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            str(cam_id),
            str(label or cam_id),
            str(risk_level),
            f"{float(risk_score or 0.0):.4f}",
            f"{float(confidence or 0.0):.2f}",
            str(int(people or 0)),
            action,
            str(int(alert_id or 0)),
        ]
        with self._log_lock:
            try:
                self._retry_queue.put_nowait(row)
            except Exception:
                # B7: never re-acquire the non-reentrant _log_lock here (deadlock).
                self.log_error = True
        print(
            f":: [Alert Vision] THREAT LOGGED :: {row[0]} | {cam_id} | {risk_level} | "
            f"CONF {float(confidence or 0.0):.2f} | ACTION {action}"
        )

    def shutdown(self) -> None:
        with contextlib.suppress(Exception):
            self._retry_queue.put_nowait(None)
        for pipeline in self.camera_list():
            pipeline.stop()
        with self.lock:
            self.cameras = {}


# ---------------------------------------------------------------------------
# WebSocket fan-out
# ---------------------------------------------------------------------------
class _ClientSession:
    def __init__(self, connection) -> None:
        self.connection = connection
        # I-A5: alerts ride a separate, priority queue so a slow telemetry
        # drain can never block or drop an emergency alert.
        self.alert_queue: asyncio.Queue = asyncio.Queue(maxsize=16)
        self.incoming_queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        self.task: asyncio.Task = None


class WebSocketBroadcaster:
    def __init__(self) -> None:
        self.clients: dict = {}
        self._lock = asyncio.Lock()

    def _get_or_create_session(self, connection) -> _ClientSession:
        session = self.clients.get(connection)
        if session is None:
            session = _ClientSession(connection)
            self.clients[connection] = session
        return session

    async def register(self, connection, history_message: str = None) -> None:
        async with self._lock:
            session = self._get_or_create_session(connection)
            self.clients[connection] = session
        session.task = asyncio.create_task(self._session_loop(session))
        if history_message is not None:
            session.incoming_queue.put_nowait(history_message)

    async def unregister(self, connection) -> None:
        async with self._lock:
            session = self.clients.pop(connection, None)
        if session is not None and session.task is not None:
            session.task.cancel()
            # gather(return_exceptions=True) reaps the cancelled task without
            # letting CancelledError (a BaseException) escape into the caller.
            await asyncio.gather(session.task, return_exceptions=True)

    def active_count(self) -> int:
        return len(self.clients)

    async def send_history(self, connection, alerts) -> None:
        message = json.dumps({"type": "history", "alerts": list(alerts)})
        await self.send_raw(connection, message)

    async def send_raw(self, connection, message: str) -> None:
        async with self._lock:
            session = self.clients.get(connection)
        if session is None:
            return
        # I-A9: never raise QueueFull into the handler - drop instead.
        if session.incoming_queue.full():
            print(":: [Alert Vision] dropping non-alert message for slow client")
            return
        try:
            session.incoming_queue.put_nowait(message)
        except asyncio.QueueFull:
            print(":: [Alert Vision] dropping non-alert message for slow client")

    async def broadcast(self, packet: dict) -> int:
        """Fan out one packet; returns how many client queues ACCEPTED it.

        Queue acceptance is a backend-side fact (the message entered the
        client's asyncio queue) - it is NOT browser receipt (I-A6).
        """
        async with self._lock:
            sessions = tuple(self.clients.values())
        if not sessions:
            return 0
        try:
            message = json.dumps(_sanitize(packet), allow_nan=False)
        except Exception:
            # never let a non-serializable packet kill the fan-out
            return 0
        # I-A5: emergency alerts go to the priority queue; telemetry to the
        # regular queue. An alert is only dropped if the *alert* queue itself
        # is full (16 deep), never because telemetry backed up.
        is_alert = str(packet.get("type") or "") == "alert"
        accepted = 0
        for session in sessions:
            queue = session.alert_queue if is_alert else session.incoming_queue
            if queue.full():
                # drop slow-client traffic instead of blocking the others
                continue
            try:
                queue.put_nowait(message)
                accepted += 1
            except asyncio.QueueFull:
                continue
        return accepted

    async def _session_loop(self, session: _ClientSession) -> None:
        try:
            while True:
                message = None
                # I-A5: drain alerts first - emergency traffic outranks telemetry.
                try:
                    message = session.alert_queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                if message is None:
                    try:
                        message = await asyncio.wait_for(
                            session.incoming_queue.get(), timeout=SEND_TIMEOUT_SEC
                        )
                    except asyncio.TimeoutError:
                        continue
                    except asyncio.CancelledError:
                        raise
                    # An alert may have arrived while we waited for telemetry.
                    try:
                        priority = session.alert_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        priority = None
                    if priority is not None:
                        # send the alert first, requeue the telemetry behind it
                        if session.incoming_queue.full():
                            pass  # telemetry is droppable; alert is not
                        else:
                            try:
                                session.incoming_queue.put_nowait(message)
                            except asyncio.QueueFull:
                                pass
                        message = priority
                await asyncio.wait_for(self._send(session.connection, message), timeout=SEND_TIMEOUT_SEC)
        except Exception:
            # B8: a timed-out send means the client is dead -> close it, don't leak the socket.
            with contextlib.suppress(Exception):
                await session.connection.close()
        finally:
            async with self._lock:
                if self.clients.get(session.connection) is session:
                    del self.clients[session.connection]

    @staticmethod
    async def _send(connection, message: str) -> None:
        await connection.send(message)


# ---------------------------------------------------------------------------
# MJPEG relay
# ---------------------------------------------------------------------------
class FrameRelayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, state: SharedSystemState, broadcaster=None) -> None:
        super().__init__(address, FrameRelayHandler)
        self.state = state
        self.broadcaster = broadcaster
        self.mjpeg_clients = 0
        self._mjpeg_lock = threading.Lock()


class FrameRelayHandler(BaseHTTPRequestHandler):
    server_version = "AlertVision/2.0"

    def do_GET(self) -> None:  # noqa: N802 - http server API
        parsed = urlparse(self.path)
        path = parsed.path
        cam_id = (parse_qs(parsed.query).get("cam") or [None])[0]
        if path == "/health":
            self._send_health()
            return
        if path in ("/frame.jpg", "/snapshot.jpg"):
            self._send_snapshot(cam_id)
            return
        if path != "/video":
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"not found")
            return
        with self.server._mjpeg_lock:  # noqa: SLF001 - server-owned counter
            if self.server.mjpeg_clients >= MAX_MJPEG_CLIENTS:
                self.send_response(503)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(b"too many clients")
                return
            self.server.mjpeg_clients += 1
        try:
            self._send_mjpeg_stream(cam_id)
        finally:
            with contextlib.suppress(Exception):
                with self.server._mjpeg_lock:  # noqa: SLF001
                    self.server.mjpeg_clients = max(0, self.server.mjpeg_clients - 1)

    def _send_health(self) -> None:
        cameras = self.server.state.camera_list()
        online = sum(1 for pipe in cameras if pipe.online)
        payload = json.dumps(
            {
                "status": "ok",
                "version": BACKEND_VERSION,
                "cameras": len(cameras),
                "cameras_online": int(online),
                "model_loaded": any(pipe.model is not None for pipe in cameras),
                "ws_clients": (
                    self.server.broadcaster.active_count()
                    if getattr(self.server, "broadcaster", None) is not None
                    else 0
                ),
                "mjpeg_clients": int(getattr(self.server, "mjpeg_clients", 0)),
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.wfile.write(payload)

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
                    # offline / removed / shutting down -> stop the stream
                    return
                self.wfile.write(
                    b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(jpeg_bytes)).encode("latin-1")
                    + b"\r\n\r\n"
                )
                self.wfile.write(jpeg_bytes)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
                time.sleep(0.04)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
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

    @staticmethod
    def _client_origin(connection) -> str:
        try:
            request = getattr(connection, "request", None)
            headers = getattr(request, "headers", None)
            if not headers:
                return None
            origin = None
            try:
                origin = headers.get("origin")
            except Exception:
                origin = None
            if not origin:
                try:
                    origin = headers.get("Origin")
                except Exception:
                    origin = None
            return str(origin) if origin else None
        except Exception:
            return None

    def _origin_allowed(self, origin) -> bool:
        if origin is None:
            return True
        if origin in ALLOWED_ORIGINS:
            return True
        configured = os.environ.get("ALERT_VISION_ALLOWED_ORIGINS") or ""
        allowed = {"http://localhost:8000", "http://127.0.0.1:8000"}
        for entry in configured.split(","):
            entry = entry.strip()
            if entry:
                allowed.add(f"http://{entry}")
        return origin in allowed

    async def _handle_connection(self, connection, path=None) -> None:
        # B8: check the Origin header BEFORE registering the client.
        if not self._origin_allowed(self._client_origin(connection)):
            with contextlib.suppress(Exception):
                await connection.close()
            return
        history_message = None
        if self.state.alert_history:
            history_message = json.dumps({"type": "history", "alerts": list(self.state.alert_history)})
        await self.broadcaster.register(connection, history_message=history_message)
        try:
            async with connection:
                msg_count = 0
                window_start = time.monotonic()
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
                    if len(text) > MAX_WS_MESSAGE_BYTES:
                        continue
                    now_mono = time.monotonic()
                    if now_mono - window_start > 1.0:
                        msg_count = 0
                        window_start = now_mono
                    msg_count += 1
                    if msg_count > MAX_WS_MESSAGES_PER_SEC:
                        continue
                    try:
                        payload = json.loads(text)
                    except Exception:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    message_type = str(payload.get("type") or "")
                    if message_type == "alert_ack":
                        # I-A6: explicit BROWSER RECEIPT of an alert.
                        ack_id = payload.get("alert_id")
                        try:
                            ack_id = int(ack_id)
                        except (TypeError, ValueError):
                            ack_id = 0
                        if ack_id > 0:
                            for pipeline in self.state.camera_list():
                                if pipeline.acknowledge_alert(ack_id):
                                    print(
                                        f":: [Alert Vision] alert {ack_id} browser-receipt "
                                        f"confirmed by client ({pipeline.id})"
                                    )
                                    break
                        continue
                    if message_type == "config":
                        loop = asyncio.get_running_loop()
                        try:
                            validated = self.state.validate_config(payload)
                            await loop.run_in_executor(None, self.state.update_config, payload)
                        except Exception as error:
                            await self.broadcaster.send_raw(
                                connection, json.dumps({"type": "config_error", "error": str(error)})
                            )
                            continue
                        await self.broadcaster.send_raw(
                            connection, json.dumps({"type": "config_ack", "config": validated})
                        )
                    elif message_type == "test_source":
                        # STEP 2/B9: probe a source off the event loop, 5s cap.
                        loop = asyncio.get_running_loop()
                        request_id = payload.get("id")
                        source = payload.get("source")
                        try:
                            source = normalize_source(source)
                            if isinstance(source, str):
                                _validate_source(source)
                        except Exception as error:
                            await self.broadcaster.send_raw(
                                connection,
                                json.dumps(
                                    {
                                        "type": "test_result",
                                        "id": request_id,
                                        "ok": False,
                                        "error": str(error),
                                        "width": 0,
                                        "height": 0,
                                        "fps": 0.0,
                                        "elapsed_ms": 0.0,
                                    }
                                ),
                            )
                            continue
                        try:
                            result = await asyncio.wait_for(
                                loop.run_in_executor(None, self._test_source_sync, source),
                                timeout=5.0,
                            )
                        except asyncio.TimeoutError:
                            result = {
                                "ok": False,
                                "error": "timed out after 5s",
                                "width": 0,
                                "height": 0,
                                "fps": 0.0,
                                "elapsed_ms": 5000.0,
                            }
                        result["type"] = "test_result"
                        result["id"] = request_id
                        await self.broadcaster.send_raw(connection, json.dumps(_sanitize(result)))
                    elif message_type == "get_state":
                        await self.broadcaster.send_raw(
                            connection,
                            json.dumps(
                                _sanitize(
                                    {
                                        "type": "state",
                                        "config": self.state.config_snapshot(),
                                        "cameras": [pipe.snapshot() for pipe in self.state.camera_list()],
                                    }
                                )
                            ),
                        )
        except Exception as error:
            # I-A7: log the fault instead of swallowing it wholesale; the
            # connection is cleaned up in finally either way.
            print(
                f":: [Alert Vision] WS handler error "
                f"({type(error).__name__}): {error}"
            )
        finally:
            await self.broadcaster.unregister(connection)

    @staticmethod
    def _test_source_sync(source) -> dict:
        """Open a source once and report resolution/fps/timing (runs in a worker thread)."""
        started = time.monotonic()
        cap = None
        error = None
        width = height = 0
        fps = 0.0
        try:
            cap = CameraPipeline._open_capture(source, timeout_ms=4000)
            if cap is None:
                error = CameraPipeline._source_error_hint(source) or (
                    "stream open failed: wrong path, camera busy (one-client limit), "
                    "or unsupported stream"
                )
            else:
                ok, frame = cap.read()
                if not ok or frame is None:
                    error = "stream opened but no frame arrived (wrong path or camera busy)"
                else:
                    height, width = frame.shape[:2]
                    with contextlib.suppress(Exception):
                        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            if cap is not None:
                with contextlib.suppress(Exception):
                    cap.release()
        return {
            "ok": error is None,
            "error": error,
            "width": int(width),
            "height": int(height),
            "fps": float(round(fps, 2)),
            "elapsed_ms": float(round((time.monotonic() - started) * 1000.0, 1)),
        }

    def start_frame_relay(self):
        relay_server = FrameRelayServer((HOST, VIDEO_PORT), self.state, self.broadcaster)
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
                    await self._broadcast_tick()
                    await asyncio.sleep(BROADCAST_INTERVAL)
        finally:
            self.state.shutdown()
            # B11: no OpenCV GUI windows exist in this headless backend.
            if relay_server is not None:
                relay_server.shutdown()
                relay_server.server_close()
            if relay_thread is not None:
                relay_thread.join(timeout=5)
            print(":: [Alert Vision] Backend shutdown complete")

    async def _broadcast_tick(self) -> dict:
        """One telemetry tick: build the full packet, broadcast it, then flush alerts.

        F1/A1: the full telemetry packet is broadcast to all connected clients every
        ``BROADCAST_INTERVAL`` even when no alert fired, so the dashboard always shows
        live state (risk, people, scores, source, etc.).

        I-A5/I-A6: emergency alert packets ride the priority queue and are
        dispatched exactly once per alert; ``broadcast()``'s return value is
        recorded as QUEUE ACCEPTANCE (per-client queue depth), which is
        explicitly not browser receipt - receipt arrives as an ``alert_ack``
        message or expires after ``ALERT_RECEIPT_GRACE_SECONDS``.
        """
        packet = self.state.build_packet()

        if self.broadcaster.active_count() > 0:
            await self.broadcaster.broadcast(packet)

        for cam in packet.get("cameras") or []:
            if not (isinstance(cam, dict) and bool(cam.get("alert"))):
                continue
            pipeline = self.state.cameras.get(str(cam.get("id") or ""))
            if pipeline is None or not pipeline.front_alert_needs_dispatch():
                continue  # already dispatched (or released) - never re-send
            accepted = await self.broadcaster.broadcast(
                {
                    "type": "alert",
                    "alert_id": int(cam.get("alert_id") or 0),
                    "camera_id": str(cam.get("id") or ""),
                    "camera_label": str(cam.get("label") or ""),
                    "risk_level": str(cam.get("risk_level") or "HIGH"),
                    "action": str(cam.get("action") or "ESCALATE"),
                    "confidence": float(cam.get("confidence") or 0.0),
                    "timestamp": float(cam.get("timestamp") or time.time() * 1000.0),
                }
            )
            pipeline.mark_alert_dispatched(accepted)
            if accepted == 0:
                print(
                    f":: [Alert Vision] alert {cam.get('alert_id')} for "
                    f"{cam.get('id')} queued: no WS clients (will replay via history)"
                )
            else:
                print(
                    f":: [Alert Vision] alert {cam.get('alert_id')} for "
                    f"{cam.get('id')} queue-accepted by {accepted} client(s); "
                    "awaiting alert_ack (browser receipt)"
                )

        # Release alerts whose receipt grace expired (queue-accept only).
        for pipeline in list(self.state.cameras.values()):
            pipeline.sweep_alert_queue()
        return packet


def _initial_specs():
    raw = (
        os.environ.get("ALERT_VISION_SOURCES")
        or os.environ.get("ALERT_VISION_SOURCE")
        or "0"
    )
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    if not parts:
        parts = ["0"]
    specs = []
    for index, part in enumerate(parts[:MAX_CAMERAS]):
        source = normalize_source(part)
        try:
            _validate_source(source)
        except Exception:
            source = 0
        specs.append(
            {"id": f"cam{index + 1}", "label": f"CAM {index + 1}", "source": source}
        )
    if not specs:
        specs = [{"id": "cam1", "label": "CAM 1", "source": 0}]
    return specs


def _port_free(port: int) -> bool:
    import socket

    with contextlib.suppress(Exception):
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", int(port)))
        sock.close()
        return True
    return False


def main() -> None:
    torch.set_num_threads(max(1, min(4, os.cpu_count() or 2)))
    try:
        stride = int(os.environ.get("ALERT_VISION_STRIDE") or DEFAULT_STRIDE)
    except (TypeError, ValueError):
        stride = DEFAULT_STRIDE
    stride = max(1, min(10, stride))
    # B3: check ports BEFORE creating the backend / starting any camera pipeline.
    if not _port_free(WEBSOCKET_PORT) or not _port_free(VIDEO_PORT):
        print(
            f":: [Alert Vision] Port busy - another instance may already be running "
            f"(ws://{HOST}:{WEBSOCKET_PORT} / http://{HOST}:{VIDEO_PORT})"
        )
        return
    backend = AlertVisionBackend(_initial_specs(), stride=stride, log_file=LOG_FILE)
    try:
        asyncio.run(backend.run())
    except KeyboardInterrupt:
        print(":: [Alert Vision] Shutdown signal received")


if __name__ == "__main__":
    main()
