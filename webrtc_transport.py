"""Telegram Mini App signaling and WebRTC audio transport for Ringback.

The public API is deliberately synchronous so the conversation loop can use
regular blocking speech turns. aiohttp and aiortc live on a dedicated asyncio
thread while :class:`WebRTCTransport` bridges call control and PCM audio to the
conversation engine in ``voice_agent.py``.

No Telegram secret is ever sent to the browser.  The Mini App authenticates by
sending Telegram.WebApp.initData, whose HMAC is verified here before a WebRTC
offer is accepted.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import os
import ssl
import struct
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
import wave
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable

import httpx

try:  # Keep pure turn-logic tests importable before optional media deps exist.
    import av
    from aiohttp import WSMsgType, web
    from aiortc import (
        RTCConfiguration,
        RTCIceServer,
        RTCPeerConnection,
        RTCSessionDescription,
    )
    from aiortc.mediastreams import MediaStreamError, MediaStreamTrack
    from av.audio.resampler import AudioResampler
except ImportError as exc:  # pragma: no cover - exercised by setup/runtime checks
    av = None
    web = None
    WSMsgType = None
    RTCConfiguration = None
    RTCIceServer = None
    RTCPeerConnection = None
    RTCSessionDescription = None
    MediaStreamError = Exception
    MediaStreamTrack = object
    AudioResampler = None
    _MEDIA_IMPORT_ERROR: Exception | None = exc
else:
    _MEDIA_IMPORT_ERROR = None


_HERE = Path(__file__).resolve().parent
_WEB_DIR = _HERE / "web"


def _log(message: str) -> None:
    print(f"[ringback-webrtc] {message}", file=sys.stderr, flush=True)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_user_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for token in raw.replace(",", " ").replace(";", " ").split():
        try:
            ids.add(int(token))
        except ValueError:
            continue
    return ids


def validate_telegram_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age: int = 86400,
    now: int | None = None,
) -> dict[str, Any]:
    """Validate Telegram Mini App ``initData`` and return the decoded payload.

    Implements Telegram's documented two-step HMAC-SHA-256 validation.  Stale
    payloads are rejected as replay attempts.  ``ValueError`` messages never
    contain the bot token or the raw signed payload.
    """
    if not init_data or not bot_token:
        raise ValueError("Telegram authentication is not configured")
    pairs = urllib.parse.parse_qsl(init_data, keep_blank_values=True)
    values = dict(pairs)
    supplied_hash = values.pop("hash", "")
    if not supplied_hash:
        raise ValueError("Telegram initData has no signature")

    check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected = hmac.new(secret_key, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, supplied_hash):
        raise ValueError("Telegram initData signature is invalid")

    try:
        auth_date = int(values.get("auth_date", "0"))
    except ValueError as exc:
        raise ValueError("Telegram initData auth_date is invalid") from exc
    current = int(time.time()) if now is None else int(now)
    if auth_date <= 0 or current - auth_date > max_age or auth_date > current + 60:
        raise ValueError("Telegram initData is stale")

    payload: dict[str, Any] = dict(values)
    for key in ("user", "receiver", "chat"):
        if key in payload:
            try:
                payload[key] = json.loads(payload[key])
            except json.JSONDecodeError as exc:
                raise ValueError(f"Telegram initData field {key} is invalid") from exc
    user = payload.get("user")
    if not isinstance(user, dict) or not isinstance(user.get("id"), int):
        raise ValueError("Telegram initData has no user")
    return payload


def _parse_ice_servers(raw: str) -> list[dict[str, Any]]:
    """Parse browser-shaped ICE JSON and reject accidental secret disclosure."""
    if not raw.strip():
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("WEBRTC_ICE_SERVERS_JSON must be valid JSON") from exc
    if not isinstance(value, list):
        raise ValueError("WEBRTC_ICE_SERVERS_JSON must be a JSON array")
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict) or not item.get("urls"):
            raise ValueError("Every ICE server needs a urls field")
        clean: dict[str, Any] = {"urls": item["urls"]}
        if item.get("username") is not None:
            clean["username"] = str(item["username"])
        if item.get("credential") is not None:
            clean["credential"] = str(item["credential"])
        result.append(clean)
    return result


def _aiortc_configuration(items: list[dict[str, Any]]):
    if RTCConfiguration is None:
        return None
    return RTCConfiguration(
        iceServers=[
            RTCIceServer(
                urls=item["urls"],
                username=item.get("username"),
                credential=item.get("credential"),
            )
            for item in items
        ]
    )


class InboundAudioBuffer:
    """Thread-safe, call-scoped 16 kHz mono signed-16 PCM capture buffer."""

    sample_rate = 16000
    sample_width = 2

    def __init__(self) -> None:
        self._data = bytearray()
        self._lock = threading.Lock()

    def append(self, pcm: bytes) -> None:
        if not pcm:
            return
        with self._lock:
            self._data.extend(pcm)

    def cursor(self) -> int:
        with self._lock:
            return len(self._data)

    def bytes_since(self, cursor: int) -> int:
        with self._lock:
            return max(0, len(self._data) - max(0, cursor))

    def clear(self) -> None:
        """Erase captured speech from memory when the call ends."""
        with self._lock:
            if self._data:
                self._data[:] = b"\0" * len(self._data)
                self._data.clear()

    def snapshot(self, cursor: int) -> str:
        with self._lock:
            pcm = bytes(self._data[max(0, cursor):])
        if not pcm:
            return ""
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        with wave.open(path, "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(self.sample_width)
            out.setframerate(self.sample_rate)
            out.writeframes(pcm)
        return path

    def tail_rms(self, cursor: int = 0, seconds: float = 0.25) -> float:
        wanted = int(self.sample_rate * self.sample_width * seconds)
        with self._lock:
            start = max(max(0, cursor), len(self._data) - wanted)
            raw = bytes(self._data[start:])
        if len(raw) < 2:
            return 0.0
        count = len(raw) // 2
        values = struct.unpack(f"<{count}h", raw[: count * 2])
        return math.sqrt(sum(value * value for value in values) / count)


class OutboundAudioTrack(MediaStreamTrack):
    """A continuously negotiated WebRTC track with replaceable WAV playback."""

    kind = "audio"
    sample_rate = 48000
    samples_per_frame = 960  # 20 ms Opus-friendly frames

    def __init__(self) -> None:
        if _MEDIA_IMPORT_ERROR is not None:  # pragma: no cover - setup failure path
            raise RuntimeError(
                "WebRTC dependencies are missing; run setup.sh or pip install -r requirements.txt"
            ) from _MEDIA_IMPORT_ERROR
        super().__init__()
        self._lock = threading.Lock()
        self._pcm = b""
        self._position = 0
        self._done = threading.Event()
        self._done.set()
        self._timestamp = 0
        self._started_at: float | None = None

    @staticmethod
    def _wav_to_pcm(path: str) -> bytes:
        chunks: list[bytes] = []
        container = av.open(path)
        try:
            resampler = AudioResampler(format="s16", layout="mono", rate=48000)
            for frame in container.decode(audio=0):
                for converted in resampler.resample(frame):
                    chunks.append(converted.to_ndarray().tobytes())
            for converted in resampler.resample(None):
                chunks.append(converted.to_ndarray().tobytes())
        finally:
            container.close()
        return b"".join(chunks)

    def play_wav(self, path: str) -> float:
        pcm = self._wav_to_pcm(path)
        with self._lock:
            self._pcm = pcm
            self._position = 0
            self._done.clear()
            if not pcm:
                self._done.set()
        return len(pcm) / float(self.sample_rate * 2)

    def cancel_playback(self) -> None:
        with self._lock:
            self._pcm = b""
            self._position = 0
            self._done.set()

    def playback_progress(self) -> float:
        with self._lock:
            if not self._pcm:
                return 1.0 if self._done.is_set() else 0.0
            return min(1.0, self._position / len(self._pcm))

    def wait_playback(self, timeout: float, disconnected) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            if disconnected():
                self.cancel_playback()
                return False
            if self._done.wait(0.05):
                return True
        self.cancel_playback()
        return False

    async def recv(self):
        if self._started_at is None:
            self._started_at = time.monotonic()
        target = self._started_at + (self._timestamp / self.sample_rate)
        delay = target - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

        wanted = self.samples_per_frame * 2
        with self._lock:
            chunk = self._pcm[self._position:self._position + wanted]
            self._position += len(chunk)
            if self._pcm and self._position >= len(self._pcm):
                self._pcm = b""
                self._position = 0
                self._done.set()
        if len(chunk) < wanted:
            chunk += b"\0" * (wanted - len(chunk))

        frame = av.AudioFrame(format="s16", layout="mono", samples=self.samples_per_frame)
        frame.planes[0].update(chunk)
        frame.sample_rate = self.sample_rate
        frame.pts = self._timestamp
        frame.time_base = Fraction(1, self.sample_rate)
        self._timestamp += self.samples_per_frame
        return frame


@dataclass
class _CallContext:
    call_id: str
    user_id: int
    inbound: InboundAudioBuffer = field(default_factory=InboundAudioBuffer)
    outbound: OutboundAudioTrack = field(default_factory=OutboundAudioTrack)
    connected_event: threading.Event = field(default_factory=threading.Event)
    declined_event: threading.Event = field(default_factory=threading.Event)
    ended_event: threading.Event = field(default_factory=threading.Event)
    remote_track_event: threading.Event = field(default_factory=threading.Event)
    pc: Any = None
    ws: Any = None
    receiver_task: asyncio.Task | None = None
    telegram_message_id: int | None = None
    reason: str = ""


class WebRTCTransport:
    """Synchronous facade over the Telegram Mini App + aiortc gateway."""

    def __init__(self) -> None:
        self.host = os.environ.get("WEBRTC_HOST", "127.0.0.1").strip()
        self.port = int(os.environ.get("WEBRTC_PORT", "8765"))
        self.public_url = os.environ.get(
            "WEBRTC_PUBLIC_URL", f"http://localhost:{self.port}"
        ).strip().rstrip("/")
        self.bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        raw_allowed = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").strip()
        self.allowed_user_ids = _parse_user_ids(raw_allowed)
        if not self.allowed_user_ids and self.chat_id.lstrip("-").isdigit():
            self.allowed_user_ids.add(int(self.chat_id))
        self.auth_max_age = int(os.environ.get("TELEGRAM_INIT_DATA_MAX_AGE", "86400"))
        self.ws_max_bytes = max(16384, int(os.environ.get("WEBRTC_WS_MAX_BYTES", "262144")))
        self.max_pending_auth = max(
            1, int(os.environ.get("WEBRTC_MAX_PENDING_AUTH", "32"))
        )
        self.max_pending_auth_per_ip = max(
            1, int(os.environ.get("WEBRTC_MAX_PENDING_AUTH_PER_IP", "4"))
        )
        self.max_clients_per_user = max(
            1, int(os.environ.get("WEBRTC_MAX_CLIENTS_PER_USER", "4"))
        )
        self.dev_mode = _env_bool("TELEGRAM_DEV_MODE")
        self.dev_user_id = int(os.environ.get("TELEGRAM_DEV_USER_ID", "1"))
        self.tls_cert = os.environ.get("WEBRTC_TLS_CERT", "").strip()
        self.tls_key = os.environ.get("WEBRTC_TLS_KEY", "").strip()
        raw_ice = os.environ.get(
            "WEBRTC_ICE_SERVERS_JSON",
            '[{"urls":["stun:stun.l.google.com:19302"]}]',
        )
        self.ice_servers = _parse_ice_servers(raw_ice)

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._runner: Any = None
        self._site: Any = None
        self._started = threading.Event()
        self._startup_error: Exception | None = None
        self._state_lock = threading.RLock()
        self._active: _CallContext | None = None
        self._clients: dict[int, set[Any]] = {}
        self._pending_auth = 0
        self._pending_auth_by_ip: dict[str, int] = {}
        self._last_bot_error = ""
        # Optional standalone-app hook.  It is invoked only after Telegram
        # initData authentication, so the browser never gets to choose an
        # arbitrary call target.  The callback must be fast and return False
        # when another call is already queued or active.
        self._call_request_handler: Callable[[int], bool] | None = None

    def set_call_request_handler(self, handler: Callable[[int], bool] | None) -> None:
        """Handle an authenticated Mini App ``request_call`` signal.

        This keeps the WebRTC transport independent of the standalone service;
        other callers may simply leave the handler unset.
        """
        self._call_request_handler = handler

    def notify_call_request_failed(self, user_id: int) -> None:
        """Return an accepted Mini App request to idle when preflight fails."""
        try:
            self._submit(
                self._send_user(
                    int(user_id),
                    {"type": "call_request", "accepted": False, "reason": "unavailable"},
                )
            ).result(timeout=3)
        except Exception:
            pass

    # ---- lifecycle ---------------------------------------------------------
    def start(self, timeout: float = 10.0) -> None:
        if _MEDIA_IMPORT_ERROR is not None:
            raise RuntimeError(
                "WebRTC dependencies are missing; run setup.sh or pip install -r requirements.txt"
            ) from _MEDIA_IMPORT_ERROR
        if self._thread and self._thread.is_alive():
            return
        if not _WEB_DIR.is_dir():
            raise RuntimeError(f"Telegram Mini App assets are missing at {_WEB_DIR}")
        self._started.clear()
        self._startup_error = None
        self._thread = threading.Thread(target=self._thread_main, name="ringback-webrtc", daemon=True)
        self._thread.start()
        if not self._started.wait(timeout):
            raise RuntimeError("WebRTC signaling server did not start in time")
        if self._startup_error:
            raise RuntimeError(f"WebRTC signaling server failed: {self._startup_error}")

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._start_server())
        except Exception as exc:  # pragma: no cover - bind/TLS environment failures
            self._startup_error = exc
            self._started.set()
            loop.close()
            return
        self._started.set()
        try:
            loop.run_forever()
        finally:
            loop.run_until_complete(self._stop_server())
            loop.close()

    async def _start_server(self) -> None:
        app = web.Application(client_max_size=1024 * 1024)
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/app.js", self._handle_app_js)
        app.router.add_get("/styles.css", self._handle_styles)
        app.router.add_get("/health", self._handle_health)
        app.router.add_get("/ws", self._handle_ws)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        ssl_context = None
        if self.tls_cert or self.tls_key:
            if not (self.tls_cert and self.tls_key):
                raise ValueError("WEBRTC_TLS_CERT and WEBRTC_TLS_KEY must be set together")
            ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ssl_context.load_cert_chain(self.tls_cert, self.tls_key)
        self._site = web.TCPSite(self._runner, self.host, self.port, ssl_context=ssl_context)
        await self._site.start()
        scheme = "https" if ssl_context else "http"
        _log(f"Mini App listening on {scheme}://{self.host}:{self.port}")

    async def _stop_server(self) -> None:
        await self._close_active("server stopped")
        with self._state_lock:
            client_sets = [list(sockets) for sockets in self._clients.values()]
            self._clients.clear()
        for sockets in client_sets:
            for ws in list(sockets):
                await ws.close(code=1001, message=b"server stopped")
        if self._runner is not None:
            await self._runner.cleanup()
        self._runner = None
        self._site = None

    def shutdown(self) -> None:
        loop = self._loop
        if loop and loop.is_running():
            try:
                future = asyncio.run_coroutine_threadsafe(self._close_active("server stopped"), loop)
                future.result(timeout=3)
            except Exception:
                pass
            loop.call_soon_threadsafe(loop.stop)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None
        self._loop = None

    # ---- HTTP / WebSocket --------------------------------------------------
    async def _handle_index(self, request):
        return web.FileResponse(_WEB_DIR / "index.html", headers={"Cache-Control": "no-store"})

    async def _handle_app_js(self, request):
        return web.FileResponse(
            _WEB_DIR / "app.js",
            headers={"Cache-Control": "no-store", "Content-Type": "text/javascript; charset=utf-8"},
        )

    async def _handle_styles(self, request):
        return web.FileResponse(
            _WEB_DIR / "styles.css",
            headers={"Cache-Control": "no-store", "Content-Type": "text/css; charset=utf-8"},
        )

    async def _handle_health(self, request):
        with self._state_lock:
            active = self._active
            status = "connected" if active and active.connected_event.is_set() else (
                "ringing" if active and not active.ended_event.is_set() else "idle"
            )
        return web.json_response({"ok": True, "call": status})

    def _authenticate(self, init_data: str) -> dict[str, Any]:
        if self.dev_mode and not init_data:
            if self.host not in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError("TELEGRAM_DEV_MODE is allowed only on loopback")
            return {"user": {"id": self.dev_user_id, "first_name": "Local developer"}}
        payload = validate_telegram_init_data(
            init_data, self.bot_token, max_age=self.auth_max_age
        )
        user_id = int(payload["user"]["id"])
        if not self.allowed_user_ids:
            raise ValueError("No Telegram user is allowed; set TELEGRAM_ALLOWED_USER_IDS")
        if user_id not in self.allowed_user_ids:
            raise ValueError("This Telegram user is not allowed")
        return payload

    async def _handle_ws(self, request):
        peer = str(request.remote or "unknown")
        peer_pending = self._pending_auth_by_ip.get(peer, 0)
        if (
            self._pending_auth >= self.max_pending_auth
            or peer_pending >= self.max_pending_auth_per_ip
        ):
            return web.Response(status=503, text="Too many pending connections")
        self._pending_auth += 1
        self._pending_auth_by_ip[peer] = peer_pending + 1
        pending_auth = True

        def release_pending_auth() -> None:
            nonlocal pending_auth
            if not pending_auth:
                return
            pending_auth = False
            self._pending_auth = max(0, self._pending_auth - 1)
            remaining = self._pending_auth_by_ip.get(peer, 1) - 1
            if remaining > 0:
                self._pending_auth_by_ip[peer] = remaining
            else:
                self._pending_auth_by_ip.pop(peer, None)

        ws = web.WebSocketResponse(
            heartbeat=20,
            receive_timeout=90,
            max_msg_size=self.ws_max_bytes,
        )
        user_id: int | None = None
        try:
            # Keep prepare inside this try: a dropped HTTP upgrade must release
            # its pending-auth slot as reliably as a failed auth message.
            await ws.prepare(request)
            first = await ws.receive(timeout=12)
            if first.type != WSMsgType.TEXT:
                raise ValueError("Authentication message is required")
            try:
                message = json.loads(first.data)
            except json.JSONDecodeError as exc:
                raise ValueError("Authentication message is invalid") from exc
            if message.get("type") != "auth":
                raise ValueError("Authentication message is required")
            payload = self._authenticate(str(message.get("initData", "")))
            user = payload["user"]
            user_id = int(user["id"])
            with self._state_lock:
                if len(self._clients.get(user_id, set())) >= self.max_clients_per_user:
                    raise ValueError("Too many Mini App connections")
                self._clients.setdefault(user_id, set()).add(ws)
            release_pending_auth()
            await ws.send_json(
                {
                    "type": "authenticated",
                    "user": {
                        "id": user_id,
                        "firstName": user.get("first_name", ""),
                        "username": user.get("username", ""),
                    },
                    "iceServers": self.ice_servers,
                }
            )
            with self._state_lock:
                active = self._active
            if active and active.user_id == user_id and not active.ended_event.is_set():
                await ws.send_json({"type": "incoming", "callId": active.call_id})

            async for item in ws:
                if item.type == WSMsgType.TEXT:
                    try:
                        incoming = json.loads(item.data)
                    except json.JSONDecodeError:
                        await ws.send_json({"type": "error", "message": "Invalid JSON"})
                        continue
                    await self._handle_ws_message(ws, user_id, incoming)
                elif item.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                    break
        except (ValueError, asyncio.TimeoutError) as exc:
            await ws.send_json({"type": "error", "message": str(exc)})
            await ws.close(code=4001, message=b"authentication failed")
        finally:
            if pending_auth:
                release_pending_auth()
            if user_id is not None:
                with self._state_lock:
                    sockets = self._clients.get(user_id)
                    if sockets is not None:
                        sockets.discard(ws)
                        if not sockets:
                            self._clients.pop(user_id, None)
                    active = self._active
                if active and active.ws is ws and not active.ended_event.is_set():
                    await self._close_active("Mini App closed")
        return ws

    async def _handle_ws_message(self, ws, user_id: int, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "ping":
            await ws.send_json({"type": "pong"})
            return
        if kind == "request_call":
            with self._state_lock:
                active = self._active
            if active and not active.ended_event.is_set():
                await ws.send_json({"type": "call_request", "accepted": False, "reason": "busy"})
                return
            handler = self._call_request_handler
            if handler is None:
                await ws.send_json(
                    {"type": "call_request", "accepted": False, "reason": "unavailable"}
                )
                return
            try:
                accepted = bool(handler(user_id))
            except Exception as exc:
                _log(f"call request handler failed: {type(exc).__name__}")
                accepted = False
            await ws.send_json(
                {
                    "type": "call_request",
                    "accepted": accepted,
                    "reason": "queued" if accepted else "busy",
                }
            )
            return
        with self._state_lock:
            active = self._active
        call_id = str(message.get("callId", ""))
        if not active or active.call_id != call_id or active.user_id != user_id:
            await ws.send_json({"type": "error", "message": "Call is no longer available"})
            return
        if kind == "decline":
            active.declined_event.set()
            await self._close_active("declined")
            return
        if kind == "hangup":
            await self._close_active("user hung up")
            return
        if kind == "offer":
            await self._accept_offer(active, ws, message)
            return
        await ws.send_json({"type": "error", "message": "Unknown message type"})

    async def _accept_offer(self, context: _CallContext, ws, message: dict[str, Any]) -> None:
        if context.pc is not None:
            await ws.send_json({"type": "error", "message": "Call is already connecting"})
            return
        sdp = str(message.get("sdp", ""))
        if not sdp or message.get("sdpType", "offer") != "offer":
            await ws.send_json({"type": "error", "message": "Invalid WebRTC offer"})
            return
        pc = RTCPeerConnection(configuration=_aiortc_configuration(self.ice_servers))
        context.pc = pc
        context.ws = ws

        @pc.on("track")
        def on_track(track):
            if track.kind != "audio":
                return
            context.remote_track_event.set()
            context.receiver_task = asyncio.create_task(self._receive_audio(context, track))

            @track.on("ended")
            async def on_ended():
                if not context.ended_event.is_set():
                    await self._close_active("microphone track ended")

        @pc.on("connectionstatechange")
        async def on_connectionstatechange():
            state = pc.connectionState
            if state == "connected" and context.remote_track_event.is_set():
                context.connected_event.set()
                await ws.send_json({"type": "connected", "callId": context.call_id})
                await self._mark_notification_answered(context)
            elif state in {"failed", "closed", "disconnected"} and not context.ended_event.is_set():
                await self._close_active(f"WebRTC {state}")

        try:
            await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
            pc.addTrack(context.outbound)
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)
            await ws.send_json(
                {
                    "type": "answer",
                    "callId": context.call_id,
                    "sdp": pc.localDescription.sdp,
                    "sdpType": pc.localDescription.type,
                }
            )
        except Exception as exc:
            _log(f"offer failed: {type(exc).__name__}: {exc}")
            await ws.send_json({"type": "error", "message": "WebRTC negotiation failed"})
            await self._close_active("negotiation failed")

    async def _receive_audio(self, context: _CallContext, track) -> None:
        resampler = AudioResampler(format="s16", layout="mono", rate=16000)
        try:
            while not context.ended_event.is_set():
                frame = await track.recv()
                for converted in resampler.resample(frame):
                    context.inbound.append(converted.to_ndarray().tobytes())
        except (MediaStreamError, asyncio.CancelledError):
            pass
        except Exception as exc:
            _log(f"audio receiver stopped: {type(exc).__name__}: {exc}")

    async def _send_user(self, user_id: int, payload: dict[str, Any]) -> None:
        with self._state_lock:
            sockets = list(self._clients.get(user_id, set()))
        for ws in sockets:
            if not ws.closed:
                try:
                    await ws.send_json(payload)
                except Exception:
                    pass

    async def _close_active(self, reason: str) -> None:
        with self._state_lock:
            context = self._active
            if context is None or context.ended_event.is_set():
                return
            context.reason = reason
            context.ended_event.set()
        context.outbound.cancel_playback()
        await self._send_user(
            context.user_id,
            {"type": "ended", "callId": context.call_id, "reason": reason},
        )
        current = asyncio.current_task()
        if context.receiver_task and context.receiver_task is not current:
            context.receiver_task.cancel()
        if context.pc is not None and context.pc.connectionState != "closed":
            await context.pc.close()
        context.inbound.clear()
        context.pc = None
        context.receiver_task = None
        context.ws = None

    # ---- Telegram notification --------------------------------------------
    def _bot_call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
        try:
            response = httpx.post(
                f"https://api.telegram.org/bot{self.bot_token}/{method}",
                json=payload,
                timeout=10.0,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Never stringify an httpx error here: its URL contains the bot token.
            raise RuntimeError(
                f"Telegram Bot API returned HTTP {exc.response.status_code}"
            ) from None
        except httpx.RequestError:
            raise RuntimeError("Telegram Bot API is unreachable") from None
        try:
            body = response.json()
        except ValueError:
            raise RuntimeError("Telegram Bot API returned invalid JSON") from None
        if not body.get("ok"):
            raise RuntimeError(str(body.get("description", "Telegram Bot API error")))
        return body

    def _send_call_notification(self, context: _CallContext) -> None:
        if not (self.bot_token and self.public_url):
            return
        url = f"{self.public_url}/?call={urllib.parse.quote(context.call_id)}"
        try:
            body = self._bot_call(
                "sendMessage",
                {
                    "chat_id": context.user_id,
                    "text": "📞 Входящий звонок от Ringback",
                    "disable_notification": False,
                    "reply_markup": {
                        "inline_keyboard": [[
                            {"text": "Ответить", "web_app": {"url": url}}
                        ]]
                    },
                },
            )
            context.telegram_message_id = int(body["result"]["message_id"])
            self._last_bot_error = ""
        except Exception as exc:
            self._last_bot_error = type(exc).__name__
            _log(f"Telegram notification failed: {type(exc).__name__}")

    async def _mark_notification_answered(self, context: _CallContext) -> None:
        if not (context.telegram_message_id and self.bot_token):
            return
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None,
                lambda: self._bot_call(
                    "editMessageText",
                    {
                        "chat_id": context.user_id,
                        "message_id": context.telegram_message_id,
                        "text": "🟢 Звонок Ringback принят",
                    },
                ),
            )
        except Exception:
            pass

    # ---- synchronous media / call facade ----------------------------------
    def _submit(self, coroutine):
        if not self._loop or not self._loop.is_running():
            raise RuntimeError("WebRTC signaling server is not running")
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    def _target_user_id(self, callee: str | None = None) -> int:
        if callee:
            try:
                return int(str(callee).removeprefix("telegram:"))
            except ValueError as exc:
                raise ValueError("callee must be a Telegram numeric user id") from exc
        explicit = os.environ.get("VOICE_TELEGRAM_USER_ID", "").strip()
        if explicit:
            return int(explicit)
        if self.chat_id.lstrip("-").isdigit():
            return int(self.chat_id)
        if len(self.allowed_user_ids) == 1:
            return next(iter(self.allowed_user_ids))
        raise RuntimeError(
            "Set VOICE_TELEGRAM_USER_ID (or a numeric TELEGRAM_CHAT_ID) for the call target"
        )

    def place_call(self, answer_timeout: float = 25.0, callee: str | None = None) -> bool:
        self.start()
        user_id = self._target_user_id(callee)
        with self._state_lock:
            previous = self._active
        if previous and not previous.ended_event.is_set():
            try:
                self._submit(self._close_active("replaced by a new call")).result(timeout=3)
            except Exception:
                pass

        context = _CallContext(call_id=uuid.uuid4().hex, user_id=user_id)
        with self._state_lock:
            self._active = context
        self._submit(
            self._send_user(user_id, {"type": "incoming", "callId": context.call_id})
        ).result(timeout=3)
        self._send_call_notification(context)

        deadline = time.monotonic() + answer_timeout
        while time.monotonic() < deadline:
            if context.connected_event.wait(0.1):
                return True
            if context.declined_event.is_set() or context.ended_event.is_set():
                return False
        try:
            self._submit(self._close_active("no answer")).result(timeout=3)
        except Exception:
            context.ended_event.set()
        return False

    @property
    def connected(self) -> bool:
        with self._state_lock:
            context = self._active
        return bool(
            context
            and context.connected_event.is_set()
            and not context.ended_event.is_set()
        )

    @property
    def disconnected(self) -> bool:
        with self._state_lock:
            context = self._active
        return bool(context and context.ended_event.is_set())

    @property
    def reason(self) -> str:
        with self._state_lock:
            return self._active.reason if self._active else ""

    def capture_cursor(self) -> int:
        with self._state_lock:
            context = self._active
        return context.inbound.cursor() if context else 0

    def capture_snapshot(self, cursor: int) -> str:
        with self._state_lock:
            context = self._active
        return context.inbound.snapshot(cursor) if context else ""

    def captured_bytes(self, cursor: int) -> int:
        with self._state_lock:
            context = self._active
        return context.inbound.bytes_since(cursor) if context else 0

    def tail_rms(self, cursor: int = 0, seconds: float = 0.25) -> float:
        with self._state_lock:
            context = self._active
        return context.inbound.tail_rms(cursor, seconds) if context else 0.0

    def play_wav(self, path: str) -> float:
        with self._state_lock:
            context = self._active
        if not context or not self.connected:
            raise RuntimeError("No active WebRTC call")
        return context.outbound.play_wav(path)

    def wait_playback(self, timeout: float) -> bool:
        with self._state_lock:
            context = self._active
        if not context:
            return False
        return context.outbound.wait_playback(timeout, lambda: self.disconnected)

    def playback_progress(self) -> float:
        with self._state_lock:
            context = self._active
        return context.outbound.playback_progress() if context else 0.0

    def stop_playback(self) -> None:
        with self._state_lock:
            context = self._active
        if context:
            context.outbound.cancel_playback()

    def hangup(self) -> None:
        with self._state_lock:
            context = self._active
        if not context or context.ended_event.is_set():
            return
        try:
            self._submit(self._close_active("agent hung up")).result(timeout=3)
        except Exception:
            context.ended_event.set()

    def status(self) -> str:
        with self._state_lock:
            context = self._active
            clients = sum(len(value) for value in self._clients.values())
        state = "idle"
        if context:
            if context.ended_event.is_set():
                state = f"ended ({context.reason or 'unknown'})"
            elif context.connected_event.is_set():
                state = "connected"
            else:
                state = "ringing"
        scheme = "https" if self.tls_cert else "http"
        details = f"webrtc={state} mini_app={scheme}://{self.host}:{self.port} clients={clients}"
        if self._last_bot_error:
            details += " telegram_notification=error"
        return details
