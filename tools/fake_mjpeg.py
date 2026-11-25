"""Synthetic MJPEG server for local camera-connect tests (STEP 4).

Serves a DroidCam-compatible MJPEG stream on:

    http://127.0.0.1:4747/video
    http://127.0.0.1:4747/mjpegfeed
    http://127.0.0.1:4747/mjpegfeed?640x480

Run:

    python tools/fake_mjpeg.py            # port 4747
    python tools/fake_mjpeg.py --port 8080
"""

import argparse
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np

BOUNDARY = "frame"
ALLOWED_PATHS = ("/video", "/videofeed", "/mjpegfeed")
JPEG_QUALITY = 80
FPS = 15


def _render(index: int) -> bytes:
    img = np.zeros((480, 640, 3), dtype=np.uint8)
    img[:] = (18, 26, 34)
    cv2.putText(
        img,
        "DroidCam SIM",
        (30, 60),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.2,
        (0, 242, 254),
        2,
        cv2.LINE_AA,
    )
    x = 60 + int((index * 7) % 520)
    cv2.circle(img, (x, 250), 42, (0, 255, 140), -1)
    cv2.putText(
        img,
        f"frame {index}",
        (30, 440),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (200, 220, 240),
        1,
        cv2.LINE_AA,
    )
    ok, buffer = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
    return buffer.tobytes() if ok else b""


class FakeCameraHandler(BaseHTTPRequestHandler):
    server_version = "FakeDroidCam/1.0"

    def do_GET(self) -> None:  # noqa: N802 - http server API
        path = self.path.split("?", 1)[0]
        if path not in ALLOWED_PATHS:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        index = 0
        try:
            while True:
                data = _render(index)
                index += 1
                self.wfile.write(
                    b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(data)).encode("latin-1")
                    + b"\r\n\r\n"
                )
                self.wfile.write(data)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
                time.sleep(1.0 / FPS)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return

    def log_message(self, format, *args) -> None:  # silence access logs
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="Synthetic MJPEG camera for Alert Vision tests")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4747)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), FakeCameraHandler)
    server.daemon_threads = True
    print(
        f":: [FakeDroidCam] serving {FPS}fps MJPEG on "
        f"http://{args.host}:{args.port}/video (also /mjpegfeed)",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
