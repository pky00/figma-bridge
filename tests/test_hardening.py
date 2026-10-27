"""Regression tests for the second review: IPv6 squatting, relay hand-over, broken surrogates, NaN payloads,
body-bound signatures, strict auth headers, path checks and JPEG parsing."""
import asyncio
import socket
import struct

import httpx
import pytest

from figma_bridge.auth import request_headers
from figma_bridge.client import Bridge, BridgeError
from figma_bridge.config import TokenSource
from figma_bridge.images import ImageCheckError, check_image_file, image_size, safe_output_path
from figma_bridge.relay import RelayServer
from fake_plugin import FakePlugin

TOKEN = "h" * 48


@pytest.fixture
def anyio_backend():
    return "asyncio"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def ipv6_available() -> bool:
    try:
        with socket.socket(socket.AF_INET6) as s:
            s.bind(("::1", 0))
        return True
    except OSError:
        return False


@pytest.mark.anyio
@pytest.mark.skipif(not ipv6_available(), reason="no IPv6 loopback")
async def test_squatter_on_ipv6_loopback_is_an_error_not_ignored():
    port = free_port()
    squatter = socket.socket(socket.AF_INET6)
    squatter.bind(("::1", port))
    squatter.listen(1)
    try:
        bridge = Bridge(port=port, token=TOKEN)
        import figma_bridge.client as client_mod
        client_mod.TAKEOVER_WAIT_S, old = 1.0, client_mod.TAKEOVER_WAIT_S
        try:
            with pytest.raises(BridgeError, match=r"\[::1\]"):
                await bridge.ensure()
        finally:
            client_mod.TAKEOVER_WAIT_S = old
        # the IPv4 socket must have been released again
        with socket.socket() as s:
            s.bind(("127.0.0.1", port))
    finally:
        squatter.close()


@pytest.mark.anyio
async def test_plugin_says_hello_again_after_relay_hand_over():
    port = free_port()
    a = Bridge(port=port, token=TOKEN)
    await a.ensure()
    stop = asyncio.Event()
    plugin = FakePlugin(f"http://127.0.0.1:{port}", TOKEN)
    task = asyncio.create_task(plugin.run(stop))
    b = Bridge(port=port, token=TOKEN)
    try:
        await a.wait_for_plugin(5)
        await a.close()
        await asyncio.sleep(0.3)
        assert await b.ensure() == "host"
        out = await b.submit("status", {}, 10, wait_for_plugin_s=10)
        assert out["ok"] and not plugin.superseded and plugin.hellos >= 2
    finally:
        stop.set()
        await b.close()
        task.cancel()


@pytest.mark.anyio
async def test_lone_surrogates_are_repaired_and_nan_is_refused():
    port = free_port()
    bridge = Bridge(port=port, token=TOKEN)
    await bridge.ensure()
    stop = asyncio.Event()
    plugin = FakePlugin(f"http://127.0.0.1:{port}", TOKEN)
    task = asyncio.create_task(plugin.run(stop))
    try:
        await bridge.wait_for_plugin(5)
        out = await bridge.submit("run", {"code": "surrogate"}, 10)
        assert out["result"]["text"] == "cut \ufffd" and out["logs"] == ["log \ufffd half"]
        out["result"]["text"].encode("utf-8")                       # now encodable
        # client mode gets the same, through the signed /job response
        other = Bridge(port=port, token=TOKEN)
        assert await other.ensure() == "client"
        out2 = await other.submit("run", {"code": "surrogate"}, 10)
        assert out2["result"]["text"] == "cut \ufffd"
        with pytest.raises(BridgeError, match="NaN"):
            await bridge.submit("run", {"code": "x", "input": float("nan")}, 10)
        with pytest.raises(BridgeError, match="NaN"):
            await other.submit("run", {"code": "x", "input": float("inf")}, 10)
        assert not any(j["payload"].get("code") == "x" for j in plugin.handled)
    finally:
        stop.set()
        await bridge.close()
        task.cancel()


@pytest.mark.anyio
async def test_signature_covers_body_and_session_and_headers_are_strict():
    server = RelayServer(port=0, tokens=TokenSource(TOKEN))
    await server.start()
    base = f"http://127.0.0.1:{server.port}"
    try:
        async with httpx.AsyncClient(trust_env=False) as c:
            body = b'{"type":"status"}'
            headers, _ = request_headers(TOKEN, "POST", "/job", body)
            tampered = await c.post(base + "/job", content=b'{"type":"run"}', headers=headers)
            assert tampered.status_code == 401                         # body changed after signing
            h2, _ = request_headers(TOKEN, "GET", "/next", b"", "a" * 16)
            h2["X-Bridge-Session"] = "b" * 16
            assert (await c.get(base + "/next", headers=h2)).status_code == 401   # session swapped
            for bad in ({"X-Bridge-Ts": "\u00b2" * 3}, {"X-Bridge-Ts": "9" * 5000}, {"X-Bridge-Auth": "\u00e9" * 64}):
                h3, _ = request_headers(TOKEN, "GET", "/status")
                h3.update(bad)
                r = await c.get(base + "/status", headers={k: v.encode("utf-8") for k, v in h3.items()})
                assert r.status_code == 401
    finally:
        await server.stop()


@pytest.mark.parametrize("path", [r"\??\UNC\host\share\x.png", "/??/UNC/host/share/x.png",
                                  r"\??\GLOBALROOT\Device\Mup\host\share\x.png", r"\\host\share\x.png",
                                  "//host/share/x.png", r"\\?\C:\x.png", r"\\.\pipe\x.png", r"\rooted\x.png"])
def test_network_and_device_paths_are_refused(path):
    with pytest.raises(ImageCheckError):
        check_image_file(path)
    with pytest.raises(ImageCheckError):
        safe_output_path(path, "PNG", overwrite=False)


def test_local_paths_still_work(tmp_path):
    out = safe_output_path(str(tmp_path / "a.png"), "PNG", overwrite=False)
    assert out.name == "a.png"
    with pytest.raises(ImageCheckError, match="must end in"):
        safe_output_path(str(tmp_path / "a.txt"), "PNG", overwrite=False)
    existing = tmp_path / "b.png"
    existing.write_bytes(b"x")
    with pytest.raises(ImageCheckError, match="already exists"):
        safe_output_path(str(existing), "PNG", overwrite=False)


def _jpeg(width: int, height: int, fill: int = 0) -> bytes:
    sof = b"\xff" * fill + b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 4) + b"\x00\x00" + sof + b"\xff\xda\x00\x02\xff\xd9"


def test_jpeg_parser_handles_fill_bytes_and_fails_closed(tmp_path):
    assert image_size(_jpeg(9000, 9000)) == ("jpg", 9000, 9000)
    assert image_size(_jpeg(9000, 9000, fill=5)) == ("jpg", 9000, 9000)
    assert image_size(b"\xff\xd8\xff\xda\x00\x02\xff\xd9") is None        # no frame header
    big = tmp_path / "big.jpg"
    big.write_bytes(_jpeg(9000, 9000, fill=3))
    with pytest.raises(ImageCheckError, match="4096"):
        check_image_file(str(big))
    webp = tmp_path / "x.webp"
    webp.write_bytes(b"RIFF....WEBP")
    with pytest.raises(ImageCheckError, match="WebP"):
        check_image_file(str(webp))


def test_cli_reads_utf8_and_utf16_scripts(tmp_path):
    from figma_bridge.cli import _read_text
    f8 = tmp_path / "a.js"
    f8.write_bytes("return 'é'".encode("utf-8-sig"))
    assert _read_text(str(f8)) == "return 'é'"
    f16 = tmp_path / "b.js"
    f16.write_bytes("return 'é'".encode("utf-16"))          # with BOM, like PowerShell 5.1's > and Out-File
    assert _read_text(str(f16)) == "return 'é'"
    bad = tmp_path / "c.js"
    bad.write_bytes(b"\x80\x81 not text")
    with pytest.raises(BridgeError, match="not UTF-8 or UTF-16"):
        _read_text(str(bad))
