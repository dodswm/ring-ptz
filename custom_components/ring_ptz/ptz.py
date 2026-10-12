"""Ring Pan-Tilt Indoor Cam (stickup_cam_mini_ptz_v1, Ring-internal "Penguin") control.

No Home Assistant imports (usable standalone).

Protocol, reverse-engineered from the Ring Android app 3.114.1: JSON-RPC 2.0 commands sent as
signalling method "rpc" inside a live-view session (the websocket python-ring-doorbell / HA use):

  {"method": "rpc", "dialog_id": D, "body": {"doorbot_id": ID, "session_id": S, "command":
     {"jsonrpc": "2.0", "id": "<uuid>", "method": "PTZ.Pan.Step",
      "params": {"sessionId": S, "direction": "LEFT", "timestamp": <ms>, "version": 1}}}}

Replies: body.command = {"destination": "app", "message": <jsonrpc reply>}; errors may lack "id".
Camera notifications arrive the same way with a "method" (PTZ.Pan.Halted / PTZ.Tilt.Halted
{direction, reason: LIMIT_REACHED}, …Aborted, PTZ.AutoTrack.StateChanged).

Methods: PTZ.Pan.Step / PTZ.Tilt.Step {direction LEFT|RIGHT / UP|DOWN} (≈16° / 10°),
PTZ.Pan.Continuous / PTZ.Tilt.Continuous {direction, speed} (speed 0 = stop; 0.3 doesn't move,
0.5 ≈ 34°/s; settings max 0.63 / 0.56), PTZ.Set {panAngle, tiltAngle}, PTZ.Get, PTZ.Overview,
PTZ.Calibrate, PTZ.AutoTrack.Get. PTZ.HomePosition.Return = "Method not found" → home = PTZ.Set to
GET /devices/v1/devices/<id>/settings → ptz_settings.{pan,tilt}_settings.positions.default.
Limits seen: pan ≈ ±177°, tilt -55…30°.

We never do WebRTC: a receive-only dummy SDP offer is enough for Ring's media server to create the
session and connect the camera (~0.2 s); Ring then drops it ~9 s later ("ICE connection failed").
A continuous move KEEPS GOING when the session drops (until the limit), so stop must always be sent:
RingPtz.move() has a deadline watchdog and keeps a fresh session ready for the stop.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import ssl
import time
from typing import Any
import uuid

from ring_doorbell import Auth
from ring_doorbell.const import (
    API_URI,
    APP_API_URI,
    DEVICES_ENDPOINT,
    INDOOR_CAM_PTZ_KINDS,
    RTC_STREAMING_TICKET_ENDPOINT,
    RTC_STREAMING_WEB_SOCKET_ENDPOINT,
)
from websockets.asyncio.client import ClientConnection, connect

_LOGGER = logging.getLogger(__name__)

USER_AGENT = "ring-ptz-ha/1.0"
SESSION_MAX_AGE = 7.0  # use a session for at most this long after camera_connected (Ring drops it ~9 s)
CONNECT_TIMEOUT = 10.0
REPLY_TIMEOUT = 5.0
DEFAULT_SPEED = 0.5
DIRECTIONS = {"left": ("Pan", "LEFT"), "right": ("Pan", "RIGHT"), "up": ("Tilt", "UP"), "down": ("Tilt", "DOWN")}


class PtzError(Exception):
    """PTZ command failed."""


def dummy_offer() -> str:
    """Receive-only audio+video SDP offer with random ICE/DTLS credentials (never connected)."""
    ufrag, pwd = secrets.token_hex(4), secrets.token_hex(12)
    fp = ":".join(f"{b:02X}" for b in secrets.token_bytes(32))
    common = (
        "c=IN IP4 0.0.0.0\r\na=rtcp:9 IN IP4 0.0.0.0\r\n"
        f"a=ice-ufrag:{ufrag}\r\na=ice-pwd:{pwd}\r\na=ice-options:trickle\r\n"
        f"a=fingerprint:sha-256 {fp}\r\na=setup:actpass\r\n"
    )
    return (
        f"v=0\r\no=- {secrets.randbits(62)} 2 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"
        "a=group:BUNDLE 0 1\r\na=msid-semantic: WMS\r\n"
        "m=audio 9 UDP/TLS/RTP/SAVPF 111 0\r\n" + common +
        "a=mid:0\r\na=recvonly\r\na=rtcp-mux\r\n"
        "a=rtpmap:111 opus/48000/2\r\na=fmtp:111 minptime=10;useinbandfec=1\r\na=rtpmap:0 PCMU/8000\r\n"
        "m=video 9 UDP/TLS/RTP/SAVPF 96\r\n" + common +
        "a=mid:1\r\na=recvonly\r\na=rtcp-mux\r\na=rtcp-rsize\r\n"
        "a=rtpmap:96 H264/90000\r\na=rtcp-fb:96 nack\r\na=rtcp-fb:96 nack pli\r\n"
        "a=fmtp:96 level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f\r\n"
    )


async def async_discover(auth: Auth) -> list[dict[str, Any]]:
    """Pan-tilt cameras on the account: [{id, device_id, name, kind}].

    Calls the device list with the bearer token only - not Ring.async_update_data(), whose
    clients_api/session call Ring answers with 406 when an account creates too many sessions.
    """
    devices = (await auth.async_query(API_URI + DEVICES_ENDPOINT)).json()
    return [
        {"id": d["id"], "device_id": d.get("device_id"), "name": d.get("description") or str(d["id"]), "kind": d["kind"]}
        for family in devices.values() if isinstance(family, list)
        for d in family if d.get("kind") in INDOOR_CAM_PTZ_KINDS
    ]


class _Session:
    """One live-view signalling session used only as an RPC pipe."""

    def __init__(self, ws: ClientConnection, device_id: int) -> None:
        self.ws = ws
        self.device_id = device_id
        self.dialog = str(uuid.uuid4())
        self.session_id: str | None = None
        self.started: float | None = None
        self.closed = False
        self.ready = asyncio.Event()
        self._pending: dict[str, asyncio.Future] = {}
        self._reader = asyncio.create_task(self._read())

    async def send(self, method: str, body: dict[str, Any]) -> None:
        await self.ws.send(json.dumps({"method": method, "dialog_id": self.dialog,
                                       "body": {"doorbot_id": self.device_id, **body}}))

    async def _read(self) -> None:
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                method, body = msg.get("method"), msg.get("body", {})
                if method == "session_created":
                    self.session_id = body["session_id"]
                elif method == "sdp":
                    await self.send("activate_session", {"session_id": self.session_id})
                elif method == "notification" and body.get("text") == "camera_connected":
                    self.started = time.monotonic()
                    self.ready.set()
                elif method == "rpc":
                    self._on_rpc(body.get("command"))
                elif method == "close":
                    _LOGGER.debug("Ring closed PTZ session: %s", body.get("reason"))
                    break
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("PTZ session reader ended: %r", err)
        finally:
            self.closed = True
            self.ready.set()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(PtzError("session closed"))
            self._pending.clear()
            await self.ws.close()

    def _on_rpc(self, command: Any) -> None:
        if isinstance(command, str):
            command = json.loads(command)
        msg = (command or {}).get("message", command) or {}
        if "method" in msg and "result" not in msg and "error" not in msg:
            _LOGGER.debug("PTZ notification %s %s", msg["method"], msg.get("params"))
            return
        fut = self._pending.pop(msg.get("id"), None)
        if fut is None and "error" in msg and len(self._pending) == 1:
            fut = self._pending.popitem()[1]  # error replies may come without an id
        if fut and not fut.done():
            if "error" in msg:
                fut.set_exception(PtzError(msg["error"].get("data") or msg["error"].get("message")))
            else:
                result = msg.get("result") or {}
                result.pop("sessionId", None)
                fut.set_result(result)

    def time_left(self) -> float:
        if self.closed or self.started is None:
            return 0.0
        return SESSION_MAX_AGE - (time.monotonic() - self.started)

    async def rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        rid = str(uuid.uuid4())
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        command = {"jsonrpc": "2.0", "id": rid, "method": method,
                   "params": {"sessionId": self.session_id, **params,
                              "timestamp": int(time.time() * 1000), "version": 1}}
        try:
            await self.send("rpc", {"session_id": self.session_id, "command": command})
            return await asyncio.wait_for(fut, REPLY_TIMEOUT)
        finally:
            self._pending.pop(rid, None)

    async def close(self) -> None:
        if not self._reader.done():
            await self.ws.close()
            await asyncio.wait([self._reader], timeout=5)


class RingPtz:
    """Pan/tilt one camera, reusing a short-lived session across commands."""

    def __init__(self, auth: Auth, device_id: int, ssl_context: ssl.SSLContext | None = None) -> None:
        self.auth = auth
        self.device_id = device_id
        self._ssl = ssl_context
        self._session: _Session | None = None
        self._lock = asyncio.Lock()
        self._move_lock = asyncio.Lock()  # FIFO: a quick release's stop can't overtake its move
        self._moving: tuple[str, str] | None = None
        self._deadline = 0.0
        self._watchdog: asyncio.Task | None = None
        self._closing: set[asyncio.Task] = set()

    @property
    def moving(self) -> bool:
        return self._moving is not None

    async def _open(self) -> _Session:
        # Auth directly (not Ring.async_query): skips the clients_api session Ring sometimes 406s
        resp = await self.auth.async_query(APP_API_URI + RTC_STREAMING_TICKET_ENDPOINT, method="POST")
        ws = await connect(RTC_STREAMING_WEB_SOCKET_ENDPOINT.format(uuid.uuid4(), resp.json()["ticket"]),
                           user_agent_header="android:com.ringapp", ssl=self._ssl or True)
        session = _Session(ws, self.device_id)
        await session.send("live_view", {"stream_options": {"audio_enabled": False, "video_enabled": True},
                                         "sdp": dummy_offer(), "type": "offer"})
        try:
            await asyncio.wait_for(session.ready.wait(), CONNECT_TIMEOUT)
        except TimeoutError:
            await session.close()
            raise PtzError("camera did not connect") from None
        if session.closed:
            raise PtzError("session closed before the camera connected")
        return session

    async def session(self, min_left: float = 0.5) -> _Session:
        """A session with at least `min_left` seconds of use left (opens a new one if needed)."""
        async with self._lock:
            if not self._session or self._session.time_left() < min_left:
                old, self._session = self._session, await self._open()
                if old:
                    task = asyncio.create_task(old.close())
                    self._closing.add(task)
                    task.add_done_callback(self._closing.discard)
            return self._session

    async def rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        session = await self.session()
        return await session.rpc(method, params or {})

    async def prepare(self) -> None:
        """Open a session ahead of a command (e.g. on button press)."""
        await self.session(min_left=3.0)

    async def get(self) -> dict[str, Any]:
        return await self.rpc("PTZ.Get")

    async def step(self, direction: str) -> None:
        axis, value = DIRECTIONS[direction]
        await self.rpc(f"PTZ.{axis}.Step", {"direction": value})

    async def set(self, pan: float, tilt: float) -> None:
        await self.rpc("PTZ.Set", {"panAngle": round(pan), "tiltAngle": round(tilt)})

    async def home_position(self) -> tuple[float, float]:
        resp = await self.auth.async_query(f"{API_URI}/devices/v1/devices/{self.device_id}/settings")
        ptz = resp.json()["ptz_settings"]
        return ptz["pan_settings"]["positions"]["default"], ptz["tilt_settings"]["positions"]["default"]

    async def home(self) -> None:
        await self.set(*await self.home_position())

    async def move(self, direction: str, speed: float = DEFAULT_SPEED, duration: float = 3.0) -> None:
        """Start, or extend, a continuous move; it stops after `duration` s unless move() is called again."""
        target = DIRECTIONS[direction]
        async with self._move_lock:
            if self._moving and self._moving != target:
                await self._stop()
            self._deadline = time.monotonic() + duration
            if self._moving != target:
                # arm the watchdog before the rpc: if the reply is lost it still sends stop
                self._moving = target
                if not self._watchdog or self._watchdog.done():
                    self._watchdog = asyncio.create_task(self._watch())
                await self.rpc(f"PTZ.{target[0]}.Continuous", {"direction": target[1], "speed": speed})

    async def _watch(self) -> None:
        """Stop at the deadline; meanwhile keep a fresh session so the stop goes out immediately."""
        try:
            while self._moving:
                if time.monotonic() >= self._deadline:
                    _LOGGER.debug("PTZ move deadline reached, stopping")
                    await self.stop()
                    return
                await self.session(min_left=2.5)
                await asyncio.sleep(0.2)
        except Exception:
            _LOGGER.exception("PTZ watchdog failed; retrying stop")
            try:
                await self.stop()
            except Exception:
                _LOGGER.exception("PTZ stop failed; camera may keep moving to its limit")

    async def stop(self) -> None:
        """Stop a continuous move (both axes if we don't know which one is moving)."""
        async with self._move_lock:
            await self._stop()

    async def _stop(self) -> None:
        moving, self._moving = self._moving, None
        for axis, value in [moving] if moving else [("Pan", "LEFT"), ("Tilt", "UP")]:
            await self.rpc(f"PTZ.{axis}.Continuous", {"direction": value, "speed": 0})

    async def close(self) -> None:
        if self._watchdog:
            self._watchdog.cancel()
        if self._session:
            await self._session.close()
        await asyncio.gather(*self._closing, return_exceptions=True)
