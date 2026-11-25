"""Camera connectivity CLI for Alert Vision.

Usage:

    python check_camera.py http://192.168.1.106:4747/video
    python check_camera.py 0                      # local webcam index

Prints: resolved URL, open time, first-frame time, resolution, FPS, and the
likely cause if the stream cannot be opened. Tries known phone-camera
alternate paths (/video -> /mjpegfeed?640x480 -> /mjpegfeed, /video -> /videofeed).
"""

import os
import sys
import time
from urllib.parse import urlparse

# Must be set before importing cv2 so the FFMPEG backend picks it up.
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    "rtsp_transport;tcp|fflags;nobuffer|probesize;32768|analyzeduration;0",
)
import cv2  # noqa: E402


def normalize(source: str):
    text = str(source).strip()
    if text.isdigit():
        return int(text)
    if "://" not in text:
        raise SystemExit(f":: invalid source: {text!r} (use http://, https://, rtsp:// or a webcam index)")
    return text


def candidates(source):
    """Primary URL plus known phone-camera alternate stream paths."""
    if not isinstance(source, str):
        return [source]
    parsed = urlparse(source)
    if parsed.scheme not in ("http", "https"):
        return [source]
    path = parsed.path or "/"
    result = [source]
    alternates = []
    if parsed.port == 4747:
        alternates.extend(["/mjpegfeed?640x480", "/mjpegfeed"])
    elif path in ("/video", "/videofeed"):
        alternates.append("/videofeed" if path == "/video" else "/video")
    for alt in alternates:
        alt_path, _, alt_query = alt.partition("?")
        if alt_path == path and alt_query == (parsed.query or ""):
            continue
        result.append(parsed._replace(path=alt_path, query=alt_query).geturl())
    return result


def open_capture(source, timeout_ms=4000):
    if isinstance(source, int):
        cap = cv2.VideoCapture(source, cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY)
        if cap is not None and cap.isOpened():
            return cap
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass
        return None
    params = []
    if hasattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC"):
        params.extend([int(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC), int(timeout_ms)])
    if hasattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC"):
        params.extend([int(cv2.CAP_PROP_READ_TIMEOUT_MSEC), int(timeout_ms)])
    try:
        cap = cv2.VideoCapture(source, cv2.CAP_FFMPEG, params) if params else cv2.VideoCapture(source, cv2.CAP_FFMPEG)
    except Exception:
        return None
    if cap is not None and cap.isOpened():
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        return cap
    if cap is not None:
        try:
            cap.release()
        except Exception:
            pass
    return None


def error_hint(source):
    if isinstance(source, int):
        return f"webcam index {source} not found"
    parsed = urlparse(source)
    host = parsed.hostname
    if not host:
        return "invalid URL"
    port = parsed.port or (443 if parsed.scheme == "https" else 554 if parsed.scheme == "rtsp" else 80)
    import socket

    try:
        probe = socket.create_connection((host, port), timeout=1.5)
        probe.close()
        return (
            "host/port reachable but stream open failed: wrong path, camera busy "
            "(one-client limit), or unsupported stream"
        )
    except socket.timeout:
        return "TIMEOUT: host did not respond (camera off / wrong IP / firewall)"
    except OSError as exc:
        return f"CONNECTION REFUSED/UNREACHABLE: {exc}"


def probe(source):
    address = normalize(source)
    print(f":: source              {source}")
    print(f":: negotiated type     {'webcam index' if isinstance(address, int) else address.split('://', 1)[0].upper()}")
    for index, candidate in enumerate(candidates(address)):
        timeout_ms = 4000 if index == 0 else 2000
        label = "primary" if index == 0 else f"alternate {index}"
        opened_at = time.monotonic()
        cap = open_capture(candidate, timeout_ms=timeout_ms)
        open_ms = (time.monotonic() - opened_at) * 1000.0
        if cap is None:
            print(f":: {label:<10} FAILED  open={open_ms:6.0f}ms  url={candidate}")
            print(f"::            reason: {error_hint(candidate)}")
            continue
        try:
            read_at = time.monotonic()
            ok, frame = cap.read()
            first_frame_ms = (time.monotonic() - read_at) * 1000.0
            if not ok or frame is None:
                print(f":: {label:<10} FAILED  open={open_ms:6.0f}ms  first_frame={first_frame_ms:6.0f}ms")
                print("::            reason: stream opened but no frame arrived (wrong path or camera busy)")
                continue
            height, width = frame.shape[:2]
            fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
            print(f":: {label:<10} OK      open={open_ms:6.0f}ms  first_frame={first_frame_ms:6.0f}ms")
            print(f"::            url        {candidate}")
            print(f"::            resolution {width}x{height}  fps={fps:.1f}")
            return 0
        finally:
            try:
                cap.release()
            except Exception:
                pass
    print(":: RESULT: camera could not be opened with any known path.")
    return 1


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    try:
        return probe(argv[1])
    except SystemExit as exc:
        print(exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
