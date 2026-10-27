"""Bridge: what the MCP server and the CLI talk to.

It hosts the relay in-process when the port is free. If another process already hosts it (for example a
second agent session), it becomes an HTTP client of that relay, and takes over hosting if that process exits.
Responses from a relay in another process are checked against the token (see auth.py), so a program squatting
on the port is detected instead of trusted.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx

from .auth import request_headers, verify_response
from .config import DEFAULT_WAIT_FOR_PLUGIN_S, HOST, NOT_CONNECTED, PORT, TokenSource, clamp_timeout
from .relay import BadPayload, JobTimeout, PluginNotConnected, PluginSwitched, PortTaken, RelayServer, RelayStopped, port_in_use

log = logging.getLogger("figma_bridge.client")
TAKEOVER_WAIT_S = 8.0       # how long to retry binding when the previous host is still releasing the port


class BridgeError(RuntimeError):
    pass


class _Unsigned(Exception):
    """A response without a valid signature: not our relay, or a different token."""

    def __init__(self, status: int, text: str) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.text = text


class Bridge:
    def __init__(self, host: str = HOST, port: int = PORT, token: str | None = None) -> None:
        self.host = host
        self.port = port
        self.tokens = TokenSource(fixed=token)
        self.relay: RelayServer | None = None
        self.mode: str | None = None          # "host" | "client"
        self._lock = asyncio.Lock()

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    async def _http(self, method: str, path: str, payload: dict | None = None, timeout: float = 15.0) -> tuple[int, dict | None]:
        """Signed request to the relay; returns (status, json body) of a correctly signed response."""
        token = self.tokens.get()
        content = b""
        if payload is not None:
            try:
                content = json.dumps(payload, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")
            except ValueError as exc:
                raise BridgeError(f"The job cannot be sent as JSON ({exc}); NaN and Infinity are not allowed.") from None
        headers, nonce = request_headers(token, method, path, content)
        if payload is not None:
            headers["Content-Type"] = "application/json"
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            r = await client.request(method, self.base_url + path, content=content or None, headers=headers)
        if not verify_response(token, nonce, r.status_code, r.content, r.headers.get("x-bridge-sig")):
            raise _Unsigned(r.status_code, r.text[:300])
        try:
            body = r.json() if r.content else None
        except ValueError:
            raise _Unsigned(r.status_code, r.text[:300]) from None
        return r.status_code, body

    async def ensure(self) -> str:
        async with self._lock:
            if self.mode:
                return self.mode
            deadline = time.monotonic() + TAKEOVER_WAIT_S
            family = "127.0.0.1"
            while True:
                relay = RelayServer(self.host, self.port, self.tokens)
                try:
                    await relay.start()
                    self.relay, self.mode = relay, "host"
                    return self.mode
                except OSError as exc:
                    if not port_in_use(exc):
                        raise
                    family = exc.family if isinstance(exc, PortTaken) else "127.0.0.1"
                try:
                    status, body = await self._http("GET", "/status", timeout=5)
                    if status == 200 and isinstance(body, dict) and body.get("relay") == "figma-bridge":
                        self.mode = "client"
                        log.info("using the relay already running on port %s", self.port)
                        return self.mode
                    raise BridgeError(f"Port {self.port} answered unexpectedly (HTTP {status}).")
                except _Unsigned as exc:
                    hint = (" It looks like a figma-bridge relay using a different token; check FIGMA_BRIDGE_TOKEN "
                            "and ~/.figma-bridge/token." if exc.status == 401 else "")
                    raise BridgeError(f"Port {self.port} is in use by another program that is not a figma-bridge relay "
                                      f"with our token.{hint} Set FIGMA_BRIDGE_PORT to a free port and change "
                                      "devAllowedDomains in plugin/manifest.json and RELAY in plugin/ui.html to match.") from None
                except httpx.TransportError:
                    # Nobody answers yet the port is taken: usually the previous host is still releasing it.
                    if time.monotonic() > deadline:
                        if family == "::1":
                            raise BridgeError(f"Another program holds [::1]:{self.port}. The Figma plugin connects to "
                                              f"localhost, which tries ::1 first, so it would talk to that program. Stop it, "
                                              "or set FIGMA_BRIDGE_PORT (and update the plugin) to a free port.") from None
                        raise BridgeError(f"Port {self.port} is busy and nothing answers on it. If a relay just stopped, "
                                          "try again in a few seconds.") from None
                    await asyncio.sleep(0.25)

    async def _lost_relay(self) -> None:
        async with self._lock:
            if self.mode == "client":
                self.mode = None

    async def status(self, _retried: bool = False) -> dict:
        await self.ensure()
        if self.mode == "host":
            assert self.relay is not None
            return self.relay.broker.status(self.relay.port, self.relay.families)
        try:
            status, body = await self._http("GET", "/status")
        except httpx.TransportError:
            await self._lost_relay()
            if _retried:
                raise BridgeError("The relay went away and could not be reached again.") from None
            return await self.status(_retried=True)
        except _Unsigned as exc:
            await self._lost_relay()
            raise BridgeError(f"The relay answered without a valid signature (HTTP {exc.status}); wrong token?") from None
        if status != 200 or not isinstance(body, dict):
            raise BridgeError(f"The relay answered HTTP {status}.")
        return body

    async def wait_for_plugin(self, seconds: float) -> dict:
        deadline = time.monotonic() + seconds
        while True:
            st = await self.status()
            if st.get("pluginConnected"):
                return st
            if time.monotonic() > deadline:
                raise BridgeError(NOT_CONNECTED)
            await asyncio.sleep(0.3)

    async def submit(self, type_: str, payload: dict | None = None, timeout_s: float | None = None,
                     wait_for_plugin_s: float = DEFAULT_WAIT_FOR_PLUGIN_S, _retried: bool = False) -> dict:
        """Run a job in Figma. Returns {ok, result, error, logs}; raises BridgeError if Figma is unreachable."""
        await self.ensure()
        timeout = clamp_timeout(timeout_s)
        if self.mode == "host":
            assert self.relay is not None
            try:
                return await self.relay.broker.submit(type_, payload or {}, timeout, wait_for_plugin_s)
            except (PluginNotConnected, JobTimeout, RelayStopped, PluginSwitched, BadPayload) as exc:
                raise BridgeError(str(exc)) from None
        try:
            status, body = await self._http("POST", "/job", {
                "type": type_, "payload": payload or {}, "timeoutMs": int(timeout * 1000),
                "waitForPluginMs": int(wait_for_plugin_s * 1000),
            }, timeout=timeout + wait_for_plugin_s + 15)
        except httpx.ConnectError:
            # The request never reached a relay, so nothing ran: take over hosting and retry once.
            if _retried:
                raise BridgeError("The relay went away and could not be restarted.") from None
            await self._lost_relay()
            return await self.submit(type_, payload, timeout, max(wait_for_plugin_s, 8.0), _retried=True)
        except httpx.TransportError as exc:
            await self._lost_relay()
            raise BridgeError(f"The relay went away while the job was in flight ({type(exc).__name__}); it may or may "
                              "not have run in Figma. Check the file before retrying.") from None
        except _Unsigned as exc:
            await self._lost_relay()
            raise BridgeError(f"The relay answered without a valid signature (HTTP {exc.status}): {exc.text}") from None
        if status == 200 and isinstance(body, dict):
            return body
        message = body.get("error") if isinstance(body, dict) else None
        raise BridgeError(message or f"The relay answered HTTP {status}.")

    async def close(self) -> None:
        if self.relay is not None:
            await self.relay.stop()
        self.relay = None
        self.mode = None
