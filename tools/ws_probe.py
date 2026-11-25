"""WebSocket probe for Alert Vision.

Connects to ws://127.0.0.1:8765 and prints what a real client receives.

    python tools/ws_probe.py                 # 5 seconds
    python tools/ws_probe.py --seconds 8
    python tools/ws_probe.py --test http://127.0.0.1:4747/video   # probe a source
    python tools/ws_probe.py --set http://127.0.0.1:4747/video --seconds 6
                                             # apply cam1 source, report time-to-connected
"""

import argparse
import asyncio
import json
import time

import websockets

URI = "ws://127.0.0.1:8765"


def _camera_summary(cam: dict) -> dict:
    connection = cam.get("connection") or {}
    return {
        "id": cam.get("id"),
        "label": cam.get("label"),
        "online": cam.get("online"),
        "conn": connection.get("state"),
        "conn_err": connection.get("error"),
        "conn_ms": connection.get("elapsed_ms"),
        "resolved": connection.get("resolved_url_masked"),
        "frame_age_ms": cam.get("last_frame_age_ms"),
        "people": cam.get("people_now"),
        "risk": cam.get("risk_level"),
        "score": cam.get("risk_score"),
        "model_error": cam.get("model_error"),
        "alert": cam.get("alert"),
        "alert_id": cam.get("alert_id"),
    }


async def run(seconds: float, test_source: str = None, set_source: str = None) -> None:
    async with websockets.connect(URI, open_timeout=10) as websocket:
        print("CONNECTED", URI, flush=True)
        started_at = None
        connected_at = None
        if set_source:
            await websocket.send(
                json.dumps(
                    {
                        "type": "config",
                        "cameras": [{"id": "cam1", "label": "CAM 1", "source": set_source}],
                        "stride": 1,
                    }
                )
            )
            started_at = time.monotonic()
            print(f"CONFIG SENT cam1 -> {set_source}", flush=True)
        if test_source:
            await websocket.send(
                json.dumps({"type": "test_source", "id": "probe-1", "source": test_source})
            )
            print(f"TEST_SOURCE SENT {test_source}", flush=True)
        start = time.monotonic()
        telemetry_msgs = 0
        last_telemetry_at = None
        intervals = []
        while time.monotonic() - start < seconds:
            try:
                message = await asyncio.wait_for(websocket.recv(), timeout=2.0)
            except asyncio.TimeoutError:
                print("(no message for 2s)", flush=True)
                break
            packet = json.loads(message)
            ptype = packet.get("type")
            if ptype == "telemetry":
                telemetry_msgs += 1
                now = time.monotonic()
                if last_telemetry_at is not None:
                    intervals.append((now - last_telemetry_at) * 1000.0)
                last_telemetry_at = now
                cameras = packet.get("cameras") or []
                if connected_at is None and started_at is not None:
                    for cam in cameras:
                        if cam.get("online") and (cam.get("connection") or {}).get("state") == "connected":
                            connected_at = now
                            print(f"CONNECTED_IN {(connected_at - started_at) * 1000.0:.0f}ms", flush=True)
                            break
                print(
                    "TELEMETRY",
                    json.dumps(
                        {
                            "cameras": len(cameras),
                            "camera_online": packet.get("camera_online"),
                            "people_now": packet.get("people_now"),
                            "risk": packet.get("risk_level"),
                            "alert": packet.get("alert"),
                            "detail": [_camera_summary(cam) for cam in cameras],
                        }
                    ),
                    flush=True,
                )
            elif ptype == "alert":
                print("ALERT", json.dumps(packet), flush=True)
            elif ptype in ("test_result", "config_ack", "config_error", "state", "history"):
                print(ptype.upper(), json.dumps(packet)[:600], flush=True)
            else:
                print("OTHER", ptype, json.dumps(packet)[:300], flush=True)
        if intervals:
            average = sum(intervals) / len(intervals)
            print(
                f"SUMMARY telemetry={telemetry_msgs} avg_interval={average:.0f}ms "
                f"min={min(intervals):.0f}ms max={max(intervals):.0f}ms",
                flush=True,
            )
        else:
            print(f"SUMMARY telemetry={telemetry_msgs}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Alert Vision WebSocket probe")
    parser.add_argument("--seconds", type=float, default=5.0)
    parser.add_argument("--test", default=None, help="optional source URL to test_source")
    parser.add_argument("--set", default=None, help="optional source URL to apply to cam1")
    args = parser.parse_args()
    asyncio.run(run(args.seconds, args.test, args.set))


if __name__ == "__main__":
    main()
