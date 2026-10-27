"""The relay: a localhost FastAPI app that queues jobs for the Figma plugin.

    agent (MCP server / CLI) --POST /job----------> relay <--GET /next (long poll)-- plugin UI iframe
                                                      relay <--POST /result--------- plugin UI iframe

Every route except the CORS preflight requires a signed request (see auth.py) whose MAC covers the method, path,
session and body; every authenticated response is signed back. Exactly one plugin instance (one Figma file) is
active at a time: the latest /hello wins; an instance that was taken over gets 409 on /next, and an instance the
relay does not know yet (e.g. after a relay hand-over) gets 410 and says hello again.
"""
from __future__ import annotations

import asyncio
import contextlib
import errno
import hmac
import json
import logging
import math
import re
import socket
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from . import __version__
from .auth import NonceCache, is_mac, request_mac, response_mac
from .config import (
    AUTH_WINDOW_S, DEFAULT_WAIT_FOR_PLUGIN_S, HOST, HOST6, MAX_BODY_BYTES, NOT_CONNECTED, PLUGIN_STALE_S,
    POLL_HOLD_S, PORT, TokenSource, clamp_timeout,
)

log = logging.getLogger("figma_bridge.relay")


class PluginNotConnected(RuntimeError):
    pass


class JobTimeout(RuntimeError):
    pass


class RelayStopped(RuntimeError):
    pass


class PluginSwitched(RuntimeError):
    pass


class BadPayload(ValueError):
    pass


class PortTaken(OSError):
    """The port (on 127.0.0.1 or ::1) is held by another socket."""

    def __init__(self, family: str, cause: OSError) -> None:
        super().__init__(cause.errno, f"port taken on {family}: {cause}")
        self.family = family


def clean_text(value: Any) -> Any:
    """Replace lone UTF-16 surrogates (e.g. an emoji cut in half by JS .slice) with U+FFFD, recursively.
    Strings like that cannot be encoded as UTF-8 and would crash the MCP stdio transport."""
    if isinstance(value, str):
        try:
            value.encode("utf-8")
            return value
        except UnicodeEncodeError:
            return value.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    if isinstance(value, list):
        return [clean_text(v) for v in value]
    if isinstance(value, dict):
        return {clean_text(k): clean_text(v) for k, v in value.items()}
    return value


def dumps(value: Any) -> bytes:
    """Strict JSON: ASCII-only (any decoder reads it), no NaN/Infinity (the plugin's JSON.parse rejects them)."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


@dataclass
class Job:
    id: str
    type: str
    payload: dict
    timeout_s: float
    expires_at_ms: int
    future: asyncio.Future = field(repr=False)
    wire: bytes = b""                # serialised once at submit time
    dispatched: bool = False
    session: str | None = None       # plugin session the job was handed to


class Broker:
    """Job queue between agents and the plugin. Lives on one asyncio loop."""

    def __init__(self) -> None:
        self.jobs: deque[Job] = deque()
        self.pending: dict[str, Job] = {}
        self.cond = asyncio.Condition()
        self.last_seen = 0.0
        self.active_session: str | None = None
        self.superseded: set[str] = set()
        self.plugin_info: dict | None = None
        self.closing = False
        self.started = time.time()

    # ---- plugin side ----
    def touch(self) -> None:
        self.last_seen = time.monotonic()

    def plugin_connected(self) -> bool:
        if not self.active_session:
            return False
        busy = any(j.dispatched for j in self.pending.values())
        return busy or (self.last_seen > 0 and time.monotonic() - self.last_seen < PLUGIN_STALE_S)

    def hello(self, session: str, info: dict) -> None:
        if self.active_session and self.active_session != session:
            self.superseded.add(self.active_session)
            self._fail_dispatched(lambda j: j.session != session, PluginSwitched(
                "Figma Bridge was connected from another Figma file while this job ran; it may have run in the previous file."))
        self.superseded.discard(session)
        if len(self.superseded) > 256:
            self.superseded = set(list(self.superseded)[-128:])
        self.active_session = session
        self.plugin_info = {**info, "connectedAt": time.strftime("%Y-%m-%dT%H:%M:%S")}
        self.touch()

    def bye(self, session: str) -> None:
        if session != self.active_session:
            return
        self.active_session = None
        self.plugin_info = None
        self.last_seen = 0.0
        # Jobs already dispatched to this session stay pending: the paused plugin still answers them (with the
        # real result, or "paused before this job ran; it did not run"), and complete() accepts that answer.

    def session_state(self, session: str) -> str:
        if session == self.active_session:
            return "active"
        return "superseded" if session in self.superseded else "unknown"

    def _fail_dispatched(self, which: Callable[[Job], bool], exc: Exception) -> None:
        for job in list(self.pending.values()):
            if job.dispatched and which(job) and not job.future.done():
                job.future.set_exception(exc)

    async def next_job(self, session: str, hold_s: float, is_disconnected: Callable[[], Awaitable[bool]]) -> Job | None:
        """Wait up to hold_s for a job; give up early on shutdown, take-over, or if the plugin dropped the request."""
        deadline = time.monotonic() + hold_s
        self.touch()
        async with self.cond:
            while True:
                if self.closing or session != self.active_session:
                    return None
                while self.jobs and self.jobs[0].future.done():   # withdrawn, cancelled or timed out meanwhile
                    self.jobs.popleft()
                if self.jobs:
                    job = self.jobs.popleft()
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                try:
                    await asyncio.wait_for(self.cond.wait(), timeout=min(remaining, 1.0))
                except asyncio.TimeoutError:
                    if await is_disconnected():
                        return None
        if await is_disconnected():                                # never hand a job to a dead request
            async with self.cond:
                self.jobs.appendleft(job)
                self.cond.notify()
            return None
        job.dispatched = True
        job.session = session
        self.touch()
        return job

    def complete(self, session: str, job_id: str, result: dict) -> bool:
        self.touch()
        job = self.pending.get(job_id)
        if job is None or job.future.done() or job.session != session:
            return False
        job.future.set_result(result)
        return True

    # ---- agent side ----
    async def wait_for_plugin(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self.plugin_connected():
            if self.closing:
                raise RelayStopped("The relay is shutting down.")
            if time.monotonic() > deadline:
                raise PluginNotConnected(NOT_CONNECTED)
            await asyncio.sleep(0.25)

    async def submit(self, type_: str, payload: dict | None = None, timeout_s: float | None = None,
                     wait_for_plugin_s: float = DEFAULT_WAIT_FOR_PLUGIN_S) -> dict:
        """Queue a job and wait for the plugin's answer: {ok, result, error, logs}.

        If the caller is cancelled (MCP cancellation, client disconnect) or times out, the job is withdrawn:
        a job still in the queue is never handed to the plugin afterwards. The plugin also gets the absolute
        deadline, so a job that sat behind another one is not started after the caller gave up.
        """
        timeout = clamp_timeout(timeout_s)
        job_id = str(uuid.uuid4())
        expires = int(time.time() * 1000 + timeout * 1000)
        try:
            wire = dumps({"id": job_id, "type": type_, "payload": payload or {}, "timeoutMs": int(timeout * 1000),
                          "expiresAt": expires})
        except ValueError as exc:
            raise BadPayload(f"The job cannot be sent as JSON ({exc}); NaN and Infinity are not allowed.") from None
        await self.wait_for_plugin(wait_for_plugin_s)
        job = Job(id=job_id, type=type_, payload=payload or {}, timeout_s=timeout, expires_at_ms=expires,
                  future=asyncio.get_running_loop().create_future(), wire=wire)
        try:
            self.pending[job.id] = job
            async with self.cond:
                self.jobs.append(job)
                self.cond.notify()
            return await asyncio.wait_for(asyncio.shield(job.future), timeout=timeout)
        except asyncio.TimeoutError:
            where = "the plugin received it and may still be running it" if job.dispatched else "the plugin never picked it up"
            raise JobTimeout(f"Figma did not answer within {timeout:.0f} s ({where}).") from None
        finally:
            self.pending.pop(job.id, None)
            if not job.future.done():
                job.future.cancel()

    def status(self, port: int, families: list[str] | None = None) -> dict:
        return {
            "relay": "figma-bridge",
            "version": __version__,
            "port": port,
            "listening": families or [],
            "pluginConnected": bool(self.plugin_connected()),
            "lastSeenSecondsAgo": round(time.monotonic() - self.last_seen, 1) if self.last_seen else None,
            "plugin": self.plugin_info,
            "queued": sum(1 for j in self.jobs if not j.future.done()),
            "inflight": len(self.pending),
            "uptimeSeconds": round(time.time() - self.started, 1),
        }


class BodySizeLimit:
    """Plain ASGI middleware (BaseHTTPMiddleware interferes with long polls and disconnect detection):
    rejects requests whose Content-Length exceeds MAX_BODY_BYTES. Installed inside CORSMiddleware so the
    413 still carries CORS headers and the plugin can read it. Bodies without Content-Length are capped
    again when read (see `_read_body`)."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            length = dict(scope.get("headers") or []).get(b"content-length", b"")
            if length.isdigit() and int(length) > MAX_BODY_BYTES:
                await JSONResponse({"ok": False, "error": "request body too large"}, status_code=413)(scope, receive, send)
                return
        await self.app(scope, receive, send)


_NONCE = re.compile(r"[0-9a-f]{16,64}")
_TS = re.compile(r"[0-9]{1,16}")
_SESSION = re.compile(r"[0-9a-zA-Z_-]{8,64}")


async def _read_body(request: Request) -> bytes:
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="request body too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _no_constants(name: str):
    raise ValueError(f"{name} is not valid JSON")


def _parse(body: bytes) -> dict:
    if not body:
        return {}
    try:
        data = json.loads(body, parse_constant=_no_constants)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid JSON body") from None
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")
    return clean_text(data)


def create_app(broker: Broker, tokens: TokenSource, port_ref: Callable[[], int],
               families_ref: Callable[[], list[str]] = lambda: []) -> FastAPI:
    app = FastAPI(title="figma-bridge relay", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    nonces = NonceCache(ttl_s=2 * AUTH_WINDOW_S)
    app.add_middleware(BodySizeLimit)            # added first = innermost
    # The plugin UI runs in an iframe with an opaque ("null") origin, so CORS allows any origin; the request
    # signature keeps other pages and programs out. allow_private_network answers Chromium's Private Network
    # Access preflight for requests to localhost.
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["X-Bridge-Ts", "X-Bridge-Nonce", "X-Bridge-Auth", "X-Bridge-Session", "Content-Type"],
        expose_headers=["X-Bridge-Sig"], max_age=600, allow_private_network=True,
    )

    async def authenticate(request: Request) -> tuple[str, str, bytes]:
        """Returns (token, nonce, body) or raises 401. The MAC covers method, path, session and body."""
        ts = request.headers.get("x-bridge-ts", "")
        nonce = request.headers.get("x-bridge-nonce", "")
        mac = request.headers.get("x-bridge-auth", "")
        session = request.headers.get("x-bridge-session", "")
        if not (_TS.fullmatch(ts) and _NONCE.fullmatch(nonce) and is_mac(mac)):
            raise HTTPException(status_code=401, detail="unsigned request")
        if abs(time.time() * 1000 - int(ts)) > AUTH_WINDOW_S * 1000:
            raise HTTPException(status_code=401, detail="request timestamp out of range (check the clock)")
        body = await _read_body(request)
        token = tokens.get()
        if not hmac.compare_digest(request_mac(token, request.method, request.url.path, ts, nonce, session, body), mac):
            raise HTTPException(status_code=401, detail="bad signature (wrong token?)")
        if not nonces.add(nonce):
            raise HTTPException(status_code=401, detail="replayed request")
        return token, nonce, body

    def signed(auth: tuple[str, str, bytes], status: int, payload: Any = None, raw: bytes | None = None) -> Response:
        token, nonce, _ = auth
        if raw is not None:
            body = raw
        elif payload is None:
            body = b""
        else:
            try:
                body = dumps(payload)
            except (TypeError, ValueError):
                body = dumps({"ok": False, "error": "the relay could not encode this response as JSON"})
        headers = {"X-Bridge-Sig": response_mac(token, nonce, status, body), "Cache-Control": "no-store"}
        if not body:
            return Response(status_code=status, headers=headers)
        return Response(content=body, status_code=status, media_type="application/json", headers=headers)

    def session_of(request: Request) -> str:
        s = request.headers.get("x-bridge-session", "")
        if not _SESSION.fullmatch(s):
            raise HTTPException(status_code=400, detail="missing or malformed X-Bridge-Session")
        return s

    def not_active(auth, session: str) -> Response | None:
        state = broker.session_state(session)
        if state == "superseded":
            return signed(auth, 409, {"ok": False, "reason": "superseded", "error": "another Figma file has the bridge"})
        if state == "unknown":
            return signed(auth, 410, {"ok": False, "reason": "hello-needed", "error": "say hello first"})
        return None

    @app.get("/status")
    async def status(request: Request):
        auth = await authenticate(request)
        return signed(auth, 200, broker.status(port_ref(), families_ref()))

    @app.get("/next")
    async def next_job(request: Request):
        auth = await authenticate(request)
        session = session_of(request)
        refused = not_active(auth, session)
        if refused:
            return refused
        job = await broker.next_job(session, POLL_HOLD_S, request.is_disconnected)
        if job is None:
            return not_active(auth, session) or signed(auth, 204)
        return signed(auth, 200, raw=job.wire)

    @app.post("/hello")
    async def hello(request: Request):
        auth = await authenticate(request)
        session = session_of(request)
        broker.hello(session, _parse(auth[2]))
        async with broker.cond:
            broker.cond.notify_all()         # superseded sessions' held polls answer 409 now
        log.info("plugin connected: %s", (broker.plugin_info or {}).get("fileName", "?"))
        return signed(auth, 204)

    @app.post("/bye")
    async def bye(request: Request):
        auth = await authenticate(request)
        broker.bye(session_of(request))
        async with broker.cond:
            broker.cond.notify_all()
        return signed(auth, 204)

    @app.post("/result")
    async def result(request: Request):
        auth = await authenticate(request)
        session = session_of(request)
        body = _parse(auth[2])
        broker.complete(session, str(body.get("id", "")), {
            "ok": bool(body.get("ok")),
            "result": body.get("result"),
            "error": body.get("error"),
            "logs": body.get("logs") if isinstance(body.get("logs"), list) else [],
        })
        return signed(auth, 204)

    @app.post("/job")
    async def job(request: Request):
        auth = await authenticate(request)
        body = _parse(auth[2])
        if not isinstance(body.get("type"), str) or not body["type"]:
            return signed(auth, 400, {"ok": False, "error": "job type missing"})
        payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
        timeout_ms = body.get("timeoutMs")
        wait_ms = body.get("waitForPluginMs")
        task = asyncio.create_task(broker.submit(
            body["type"], payload,
            timeout_s=timeout_ms / 1000 if isinstance(timeout_ms, (int, float)) else None,
            wait_for_plugin_s=wait_ms / 1000 if isinstance(wait_ms, (int, float)) else DEFAULT_WAIT_FOR_PLUGIN_S,
        ))
        # Withdraw the job if the agent goes away (cancelled call, crashed session).
        while not task.done():
            done, _ = await asyncio.wait({task}, timeout=0.5)
            if not done and await request.is_disconnected():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
                return signed(auth, 499, {"ok": False, "error": "client went away; job withdrawn"})
        try:
            return signed(auth, 200, task.result())
        except BadPayload as exc:
            return signed(auth, 400, {"ok": False, "error": str(exc), "result": None, "logs": []})
        except (PluginNotConnected, JobTimeout, RelayStopped, PluginSwitched) as exc:
            return signed(auth, 503, {"ok": False, "error": str(exc), "result": None, "logs": []})

    return app


def bind_socket(host: str, port: int, family: int = socket.AF_INET) -> socket.socket:
    """Bind the listening socket ourselves so 'port in use' is a clean exception (uvicorn would sys.exit)."""
    sock = socket.socket(family, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):          # Windows: refuse to share the port
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    if family == socket.AF_INET6 and hasattr(socket, "IPV6_V6ONLY"):
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    try:
        sock.bind((host, port))
        sock.listen(64)
    except OSError:
        sock.close()
        raise
    sock.setblocking(False)
    return sock


def port_in_use(exc: OSError) -> bool:
    if isinstance(exc, PortTaken):
        return True
    return exc.errno == errno.EADDRINUSE or getattr(exc, "winerror", None) in (10048, 10013)


def ipv6_unavailable(exc: OSError) -> bool:
    return exc.errno in (errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL) or getattr(exc, "winerror", None) in (10047, 10049)


class _EmbeddedServer(uvicorn.Server):
    """uvicorn without its process-wide signal handlers: the host process (MCP server / CLI) owns Ctrl+C."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield

    def install_signal_handlers(self) -> None:   # older uvicorn versions
        pass


class RelayServer:
    """Runs the relay app with uvicorn on the current asyncio loop."""

    def __init__(self, host: str = HOST, port: int = PORT, tokens: TokenSource | None = None) -> None:
        self.host = host
        self.port = port
        self.tokens = tokens or TokenSource()
        self.tokens.get()                                     # fail fast on a missing/weak token
        self.broker = Broker()
        self.families: list[str] = []
        self.app = create_app(self.broker, self.tokens, lambda: self.port, lambda: self.families)
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> "RelayServer":
        try:
            sock = bind_socket(self.host, self.port)
        except OSError as exc:
            if port_in_use(exc):
                raise PortTaken("127.0.0.1", exc) from None
            raise
        self.port = sock.getsockname()[1]
        sockets, self.families = [sock], ["127.0.0.1"]
        # The plugin calls http://localhost, which Chromium tries on ::1 first. If another program holds
        # [::1]:port it would receive the plugin's traffic, so that is an error, not something to skip.
        try:
            sockets.append(bind_socket(HOST6, self.port, socket.AF_INET6))
            self.families.append("::1")
        except OSError as exc:
            if not ipv6_unavailable(exc):
                sock.close()
                raise PortTaken("::1", exc) from None
        config = uvicorn.Config(self.app, log_config=None, access_log=False, lifespan="off",
                                timeout_keep_alive=int(POLL_HOLD_S) + 5, timeout_graceful_shutdown=2)
        self._server = _EmbeddedServer(config)
        self._task = asyncio.create_task(self._server.serve(sockets=sockets))
        for _ in range(300):                                  # wait until uvicorn reports started
            if self._server.started:
                break
            if self._task.done():
                self._task.result()
            await asyncio.sleep(0.01)
        log.info("relay listening on http://localhost:%s (%s)", self.port, ", ".join(self.families))
        return self

    async def stop(self) -> None:
        broker = self.broker
        broker.closing = True
        async with broker.cond:
            broker.cond.notify_all()                          # release held long polls at once
        for job in list(broker.pending.values()):
            if not job.future.done():
                job.future.set_exception(RelayStopped("The relay stopped while the job was in flight; it may have run in Figma."))
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (asyncio.TimeoutError, Exception):
                self._task.cancel()
                with contextlib.suppress(BaseException):
                    await self._task
        self._server = None
        self._task = None


def finite(value: Any, name: str) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if not math.isfinite(f):
        raise ValueError(f"{name} must be a finite number")
    return f
