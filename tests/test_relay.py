import asyncio
import socket

import httpx
import pytest

from figma_bridge.auth import request_headers
from figma_bridge.client import Bridge, BridgeError
from figma_bridge.config import TokenError, TokenSource
from figma_bridge.relay import RelayServer
from fake_plugin import FakePlugin

TOKEN = "t" * 48


@pytest.fixture
def anyio_backend():
    return "asyncio"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_plugin(port: int, stop: asyncio.Event, **kw) -> tuple[FakePlugin, asyncio.Task]:
    plugin = FakePlugin(f"http://127.0.0.1:{port}", TOKEN, **kw)
    return plugin, asyncio.create_task(plugin.run(stop))


@pytest.mark.anyio
async def test_job_roundtrip_host_and_client_mode():
    port = free_port()
    host = Bridge(port=port, token=TOKEN)
    assert await host.ensure() == "host"
    stop = asyncio.Event()
    _, task = start_plugin(port, stop)
    try:
        await host.wait_for_plugin(5)
        out = await host.submit("run", {"code": "return 1", "input": {"a": 1}}, 10)
        assert out["ok"] and out["result"] == {"echoCode": "return 1", "input": {"a": 1}}
        assert out["logs"] == ["hello from figma"]

        # A second process sees the port taken and becomes a client of the same relay.
        other = Bridge(port=port, token=TOKEN)
        assert await other.ensure() == "client"
        out2 = await other.submit("status", {}, 10)
        assert out2["ok"] and out2["result"]["fileName"] == "Test file"
        st = await other.status()
        assert st["pluginConnected"] and st["plugin"]["fileName"] == "Test file"

        failed = await other.submit("run", {"code": "throw"}, 10)
        assert not failed["ok"] and "boom" in failed["error"] and failed["logs"] == ["about to throw"]
    finally:
        stop.set()
        await host.close()
        task.cancel()


@pytest.mark.anyio
async def test_client_takes_over_when_host_exits():
    port = free_port()
    host = Bridge(port=port, token=TOKEN)
    await host.ensure()
    stop = asyncio.Event()
    _, task = start_plugin(port, stop)
    client = Bridge(port=port, token=TOKEN)
    try:
        await host.wait_for_plugin(5)
        assert await client.ensure() == "client"
        await host.close()                               # hosting process goes away (plugin was long-polling)
        out = await client.submit("status", {}, 10, wait_for_plugin_s=8)
        assert out["ok"] and client.mode == "host"
    finally:
        stop.set()
        await client.close()
        task.cancel()


@pytest.mark.anyio
async def test_auth_signatures_cors_and_private_network():
    server = RelayServer(port=0, tokens=TokenSource(TOKEN))
    await server.start()
    base = f"http://127.0.0.1:{server.port}"
    try:
        async with httpx.AsyncClient(trust_env=False) as c:
            assert (await c.get(base + "/status")).status_code == 401                         # unsigned
            assert (await c.get(base + "/status", headers={"X-Bridge-Token": TOKEN})).status_code == 401  # raw token is not accepted
            bad, _ = request_headers("x" * 48, "GET", "/status")
            assert (await c.get(base + "/status", headers=bad)).status_code == 401            # wrong token
            good, nonce = request_headers(TOKEN, "GET", "/status")
            ok = await c.get(base + "/status", headers=good)
            assert ok.status_code == 200 and ok.json()["relay"] == "figma-bridge" and ok.headers["x-bridge-sig"]
            assert (await c.get(base + "/status", headers=good)).status_code == 401           # replayed nonce
            wrong_path, _ = request_headers(TOKEN, "GET", "/next")
            assert (await c.get(base + "/status", headers=wrong_path)).status_code == 401     # signature bound to the path
            pre = await c.options(base + "/next", headers={
                "Origin": "null",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "x-bridge-ts,x-bridge-nonce,x-bridge-auth,x-bridge-session",
                "Access-Control-Request-Private-Network": "true",
            })
            assert pre.status_code == 200
            assert pre.headers.get("access-control-allow-origin") == "*"
            assert pre.headers.get("access-control-allow-private-network") == "true"
        # Oversized body: the 413 must still carry CORS headers so the plugin can read it (raw socket: httpx
        # refuses to send a Content-Length that does not match the body).
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        writer.write(b"POST /result HTTP/1.1\r\nHost: localhost\r\nOrigin: null\r\nContent-Type: application/json\r\n"
                     b"Content-Length: " + str(70 * 1024 * 1024).encode() + b"\r\nConnection: close\r\n\r\n{}")
        await writer.drain()
        head = (await reader.read(4096)).decode("latin-1").lower()
        writer.close()
        assert head.startswith("http/1.1 413") and "access-control-allow-origin: *" in head
    finally:
        await server.stop()


@pytest.mark.anyio
async def test_not_connected_timeout_and_cancellation_withdraws_job():
    port = free_port()
    bridge = Bridge(port=port, token=TOKEN)
    await bridge.ensure()
    stop = asyncio.Event()
    try:
        with pytest.raises(BridgeError, match="not connected"):
            await bridge.submit("status", {}, 5, wait_for_plugin_s=0.5)

        silent, task = start_plugin(port, stop, silent=True)
        await bridge.wait_for_plugin(5)
        with pytest.raises(BridgeError, match="did not answer"):
            await bridge.submit("run", {"code": "return 1"}, 1.5)
        assert len(silent.handled) == 1
        stop.set()
        task.cancel()

        # A cancelled call must never reach the plugin later.
        broker = bridge.relay.broker
        broker.touch()
        pending_submit = asyncio.create_task(bridge.submit("run", {"code": "dangerous()"}, 30))
        await asyncio.sleep(0.3)
        pending_submit.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending_submit
        assert not broker.pending and all(j.future.done() for j in broker.jobs)
        stop2 = asyncio.Event()
        late, task2 = start_plugin(port, stop2)
        await asyncio.sleep(1.5)
        assert all(j["payload"].get("code") != "dangerous()" for j in late.handled)
        stop2.set()
        task2.cancel()
    finally:
        stop.set()
        await bridge.close()


@pytest.mark.anyio
async def test_second_figma_file_takes_over_and_first_is_told():
    port = free_port()
    bridge = Bridge(port=port, token=TOKEN)
    await bridge.ensure()
    stop = asyncio.Event()
    first, t1 = start_plugin(port, stop, file_name="File A")
    try:
        await bridge.wait_for_plugin(5)
        await asyncio.sleep(0.3)
        second, t2 = start_plugin(port, stop, file_name="File B")
        for _ in range(50):
            if first.superseded:
                break
            await asyncio.sleep(0.1)
        assert first.superseded
        out = await bridge.submit("status", {}, 10)
        assert out["ok"] and (await bridge.status())["plugin"]["fileName"] == "File B"
        assert any(j["type"] == "status" for j in second.handled) and not any(j["type"] == "status" for j in first.handled)
    finally:
        stop.set()
        await bridge.close()
        t1.cancel()
        if "t2" in locals():
            t2.cancel()


@pytest.mark.anyio
async def test_squatter_on_the_port_is_not_trusted():
    """Something else answers on the port with 200s but cannot sign them: the client refuses it."""
    from fastapi import FastAPI
    import uvicorn

    port = free_port()
    app = FastAPI()

    @app.api_route("/{path:path}", methods=["GET", "POST"])
    async def anything(path: str):
        return {"relay": "figma-bridge", "pluginConnected": True, "ok": True, "result": "evil"}

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, lifespan="off"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    try:
        with pytest.raises(BridgeError, match="not a figma-bridge relay"):
            await Bridge(port=port, token=TOKEN).ensure()
    finally:
        server.should_exit = True
        await task


def test_weak_tokens_are_refused(monkeypatch):
    monkeypatch.setenv("FIGMA_BRIDGE_TOKEN", "   ")
    with pytest.raises(TokenError):
        TokenSource().get()
    monkeypatch.setenv("FIGMA_BRIDGE_TOKEN", "short")
    with pytest.raises(TokenError):
        TokenSource().get()
    with pytest.raises(TokenError):
        TokenSource("abc")
