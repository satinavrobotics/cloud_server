#!/usr/bin/env python3
"""
The one script every robot -- and every operator -- launches. No per-device
config or identity file: this device's Tailscale hostname and IP are
resolved automatically at startup via `tailscale status --json` (every
robot and every operator laptop is already a tailnet member), so the same
script and command line run unmodified anywhere.

Two roles, same script:
  --role robot     (default) publish real camera video, or a synthetic
                    color-cycle frame if no camera's available (auto-
                    detected). Gets a publish-only token.
  --role operator  viewer: connects, subscribes to whatever's being
                    published, logs track/participant events. Its token can
                    also send data (teleop cmd_vel, RPC) but can't publish
                    tracks -- an operator's laptop can watch and drive the
                    fleet, it can't inject video into a room.

Unlike tests/performance/livekit/'s synthetic load tool, this uses the real
LiveKit Python SDK.

Unlike a self-minting test client, this never holds the LiveKit API secret
-- it fetches a short-lived, role-scoped token from the livekit-sfu-tokens
service (packages/services/livekit_sfu_tokens, :8008) using its
Tailscale-resolved identity. Only that service ever holds the real API secret.

Usage:
    pip install -r requirements.txt
    python robot_client.py                                    # role=robot, token-server at $TOKEN_SERVER_URL
    python robot_client.py --token-server http://100.85.3.47:8008
    python robot_client.py --role operator --room admin@satinavrobotics.com

Rooms are per user email, the same convention the ROS bridge and sati-client
use; --room (or $LIVEKIT_ROOM) defaults to admin@satinavrobotics.com.
"""

from __future__ import annotations

import argparse
import asyncio
import colorsys
import json
import logging
import os
import subprocess
import time
from signal import SIGINT, SIGTERM
from urllib import error, request

import numpy as np
from livekit import rtc

try:
    import cv2
except ImportError:
    cv2 = None  # synthetic mode still works without opencv installed


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--token-server", default=os.getenv("TOKEN_SERVER_URL", "http://100.85.3.47:8008"))
    p.add_argument("--role", choices=["robot", "operator"], default="robot")
    p.add_argument("--room", default=os.getenv("LIVEKIT_ROOM", "admin@satinavrobotics.com"),
                   help="room name; one room per user email")
    p.add_argument("--camera-index", type=int, default=0)
    p.add_argument("--force-synthetic", action="store_true", help="skip camera even if one is available")
    p.add_argument("--width", type=int, default=1280)
    p.add_argument("--height", type=int, default=720)
    p.add_argument("--fps", type=float, default=30.0)
    return p.parse_args()


def resolve_identity() -> tuple[str, str]:
    """(identity, tailscale_ip), resolved from this device's own Tailscale
    status -- the same mechanism every robot already uses for ACL scoping,
    so there's nothing extra to configure per-robot."""
    try:
        out = subprocess.run(
            ["tailscale", "status", "--json"], capture_output=True, text=True, timeout=5, check=True
        )
        self_info = json.loads(out.stdout)["Self"]
        return self_info["HostName"], self_info["TailscaleIPs"][0]
    except Exception as e:
        raise SystemExit(f"could not resolve Tailscale identity (is tailscaled running and logged in?): {e}")


def fetch_token(token_server: str, identity: str, room: str, role: str) -> dict:
    """One attempt; raises on failure. `role` controls what the token can
    actually do server-side (robot: publish tracks + data, no subscribe;
    operator: subscribe + data, no tracks) -- not just a label. Blocking, so
    callers run it in a thread (see connect_and_publish)."""
    body = json.dumps({"participantName": identity, "roomName": room, "role": role}).encode()
    req = request.Request(
        f"{token_server}/api/createToken", data=body, headers={"Content-Type": "application/json"}
    )
    with request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


async def fetch_token_with_retry(token_server: str, identity: str, room: str, role: str) -> dict:
    """Retries with backoff -- token-server being briefly unreachable over a
    4G link shouldn't crash a robot that's meant to run unattended. Async, so
    SIGTERM/SIGINT are still handled while waiting."""
    attempt = 1
    while True:
        try:
            return await asyncio.to_thread(fetch_token, token_server, identity, room, role)
        except error.HTTPError as e:
            if e.code < 500:  # the server understood and refused: retrying can't help
                raise SystemExit(f"token-server rejected the request: HTTP {e.code} {e.read().decode(errors='replace')}")
            wait = min(30, 2**attempt)
            logging.warning("token-server error (HTTP %s), retrying in %ss", e.code, wait)
            await asyncio.sleep(wait)
            attempt += 1
        except (error.URLError, OSError) as e:
            wait = min(30, 2**attempt)
            logging.warning("token-server unreachable (%s), retrying in %ss", e, wait)
            await asyncio.sleep(wait)
            attempt += 1


def open_camera(index: int, width: int, height: int):
    if cv2 is None:
        return None
    cap = cv2.VideoCapture(index)
    if not cap.isOpened():
        return None
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    ok, frame = cap.read()
    if not ok or frame is None:
        cap.release()
        return None
    return cap


async def publish_camera(room: rtc.Room, cap, args: argparse.Namespace) -> None:
    source = rtc.VideoSource(args.width, args.height)
    track = rtc.LocalVideoTrack.create_video_track("camera", source)
    options = rtc.TrackPublishOptions(
        source=rtc.TrackSource.SOURCE_CAMERA,
        simulcast=True,
        video_encoding=rtc.VideoEncoding(max_framerate=args.fps, max_bitrate=3_000_000),
    )
    pub = await room.local_participant.publish_track(track, options)
    logging.info("published REAL CAMERA track %s (%sx%s @ %sfps)", pub.sid, args.width, args.height, args.fps)

    interval = 1.0 / args.fps
    next_at = time.perf_counter()
    try:
        while True:
            ok, bgr = await asyncio.to_thread(cap.read)
            if not ok or bgr is None:
                await asyncio.sleep(0.1)
                continue
            if bgr.shape[1] != args.width or bgr.shape[0] != args.height:
                bgr = cv2.resize(bgr, (args.width, args.height), interpolation=cv2.INTER_AREA)
            i420 = cv2.cvtColor(bgr, cv2.COLOR_BGR2YUV_I420).tobytes()
            source.capture_frame(rtc.VideoFrame(args.width, args.height, rtc.VideoBufferType.I420, i420))
            next_at += interval
            sleep_for = next_at - time.perf_counter()
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
    finally:
        cap.release()
        await source.aclose()


async def publish_synthetic(room: rtc.Room, args: argparse.Namespace) -> None:
    source = rtc.VideoSource(args.width, args.height)
    track = rtc.LocalVideoTrack.create_video_track("synthetic", source)
    options = rtc.TrackPublishOptions(
        source=rtc.TrackSource.SOURCE_CAMERA,
        simulcast=True,
        video_encoding=rtc.VideoEncoding(max_framerate=args.fps, max_bitrate=3_000_000),
    )
    pub = await room.local_participant.publish_track(track, options)
    logging.info("published SYNTHETIC track %s (no camera found / --force-synthetic)", pub.sid)

    buf = bytearray(args.width * args.height * 4)
    arr = np.frombuffer(buf, dtype=np.uint8)
    interval = 1.0 / args.fps
    next_at = time.perf_counter()
    hue = 0.0
    try:
        while True:
            rgb = [int(c * 255) for c in colorsys.hsv_to_rgb(hue, 1.0, 1.0)]
            color = np.array(rgb + [255], dtype=np.uint8)
            arr.flat[0::4] = color[0]
            arr.flat[1::4] = color[1]
            arr.flat[2::4] = color[2]
            arr.flat[3::4] = color[3]
            source.capture_frame(rtc.VideoFrame(args.width, args.height, rtc.VideoBufferType.RGBA, buf))
            hue = (hue + interval / 3) % 1.0
            next_at += interval
            sleep_for = next_at - time.perf_counter()
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
    finally:
        await source.aclose()


async def watch(room: rtc.Room) -> None:
    """Operator role: publish nothing, just log what's happening in the
    room. AutoSubscribe is on by default, so tracks arrive without any
    action needed here."""

    @room.on("track_subscribed")
    def _on_track(track: rtc.Track, pub: rtc.RemoteTrackPublication, participant: rtc.RemoteParticipant) -> None:
        logging.info("subscribed to %s's %s track (%s)", participant.identity, track.kind, pub.sid)

    @room.on("participant_connected")
    def _on_join(p: rtc.RemoteParticipant) -> None:
        logging.info("peer joined: %s", p.identity)

    for p in room.remote_participants.values():
        logging.info("already in room: %s", p.identity)

    await asyncio.Event().wait()  # runs until cancelled


async def connect_and_publish(args: argparse.Namespace, identity: str) -> None:
    token_data = await fetch_token_with_retry(args.token_server, identity, args.room, args.role)

    if args.role == "operator":
        cam = None
        mode = "OPERATOR (watches; this script sends no data)"
    else:
        cam = None if args.force_synthetic else open_camera(args.camera_index, args.width, args.height)
        mode = "REAL CAMERA" if cam else "SYNTHETIC"
    logging.info("role=%s mode=%s room=%s url=%s", args.role, mode, args.room, token_data["server_url"])

    room = rtc.Room()
    disconnected = asyncio.Event()

    @room.on("disconnected")
    def _on_disconnect() -> None:
        disconnected.set()

    try:
        await room.connect(token_data["server_url"], token_data["token"])
        logging.info("connected to room %s as %s", room.name, identity)
    except rtc.ConnectError as e:
        raise ConnectionError(str(e))

    if args.role == "operator":
        task = asyncio.ensure_future(watch(room))
    else:
        task = asyncio.ensure_future(publish_camera(room, cam, args) if cam else publish_synthetic(room, args))
    await disconnected.wait()
    task.cancel()
    await room.disconnect()


async def run() -> None:
    args = parse_args()
    identity, ts_ip = resolve_identity()
    logging.info("resolved identity=%s (tailscale ip %s)", identity, ts_ip)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (SIGINT, SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    stop_wait = asyncio.ensure_future(stop.wait())
    backoff = 1
    while not stop.is_set():
        started = time.monotonic()
        reason = "disconnected"
        session = asyncio.ensure_future(connect_and_publish(args, identity))
        try:
            await asyncio.wait({session, stop_wait}, return_when=asyncio.FIRST_COMPLETED)
            if stop.is_set():
                session.cancel()
                break
            session.result()  # re-raise if it failed
        except Exception as e:
            reason = str(e)
        # Always wait before reconnecting, even after a clean disconnect: the
        # server also disconnects us when another participant takes our
        # identity, and two such clients would otherwise kick each other in a
        # tight loop. Backoff only resets after a session that stayed up.
        if time.monotonic() - started > 30:
            backoff = 1
        logging.warning("session ended (%s), reconnecting in %ss", reason, backoff)
        await asyncio.sleep(backoff)
        backoff = min(30, backoff * 2)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run())
