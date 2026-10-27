"""The real plugin UI loop (plugin/ui.html) under Node, against the real relay: signing, sessions, take-over,
and refusing to trust a program that squats on the port."""
import asyncio
import hashlib
import hmac
import json
import re
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

from figma_bridge.client import Bridge

HARNESS = Path(__file__).with_name("ui_harness.mjs")
PLUGIN = Path(__file__).resolve().parent.parent / "plugin"
TOKEN = "u" * 48
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")


@pytest.fixture
def anyio_backend():
    return "asyncio"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class UI:
    """A running ui_harness.mjs process whose stdout events are collected in the background."""

    def __init__(self, port: int, token: str, file_name: str) -> None:
        self.events: list[dict] = []
        self.proc: asyncio.subprocess.Process | None = None
        self.args = ["node", str(HARNESS), str(port), token, file_name]

    async def start(self) -> "UI":
        self.proc = await asyncio.create_subprocess_exec(*self.args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        self._reader = asyncio.create_task(self._read())
        return self

    async def _read(self) -> None:
        assert self.proc and self.proc.stdout
        async for line in self.proc.stdout:
            try:
                self.events.append(json.loads(line))
            except ValueError:
                pass

    def states(self) -> list[str]:
        return [e["text"] for e in self.events if e["event"] == "state"]

    def jobs(self) -> list[dict]:
        return [e["job"] for e in self.events if e["event"] == "job"]

    async def wait_state(self, pattern: str, timeout: float = 10) -> str:
        for _ in range(int(timeout / 0.1)):
            for s in self.states():
                if re.search(pattern, s):
                    return s
            await asyncio.sleep(0.1)
        raise AssertionError(f"state {pattern!r} not reached; saw {self.states()}")

    async def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()
        self._reader.cancel()


def test_manifest_and_ui_agree_on_the_relay_origin():
    manifest = json.loads((PLUGIN / "manifest.json").read_text(encoding="utf-8"))
    domains = manifest["networkAccess"]["devAllowedDomains"]
    relay = re.search(r"const RELAY = '([^']+)'", (PLUGIN / "ui.html").read_text(encoding="utf-8")).group(1)
    assert relay in domains
    for d in domains:   # Figma rejects IP literals in devAllowedDomains
        assert not re.match(r"^\w+://\d+\.\d+\.\d+\.\d+", d) and "[" not in d, d


def test_ui_hmac_matches_python():
    vectors = [(b"key", b"The quick brown fox jumps over the lazy dog"), (b"k" * 100, b""), (TOKEN.encode(), bytes(range(256)) * 300)]
    for key, msg in vectors:
        out = subprocess.run(["node", str(HARNESS), "hmac", key.hex(), "-"], input=msg.hex(), capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == hmac.new(key, msg, hashlib.sha256).hexdigest()


@pytest.mark.anyio
async def test_ui_runs_jobs_and_hands_over_between_files():
    port = free_port()
    bridge = Bridge(port=port, token=TOKEN)
    await bridge.ensure()
    first = await UI(port, TOKEN, "File A").start()
    second = None
    try:
        await bridge.wait_for_plugin(15)
        await first.wait_state("^Connected$")
        out = await bridge.submit("run", {"code": "return 7"}, 15)
        assert out["ok"] and out["result"] == {"ran": "return 7", "file": "File A"}
        assert (await bridge.status())["plugin"]["fileName"] == "File A"

        second = await UI(port, TOKEN, "File B").start()
        await second.wait_state("^Connected$")
        await first.wait_state("Another Figma file has the bridge")
        out = await bridge.submit("run", {"code": "return 8"}, 15)
        assert out["result"]["file"] == "File B"
        assert [j["payload"]["code"] for j in first.jobs()] == ["return 7"]
    finally:
        await first.stop()
        if second:
            await second.stop()
        await bridge.close()


@pytest.mark.anyio
async def test_ui_rejects_wrong_token():
    port = free_port()
    bridge = Bridge(port=port, token=TOKEN)
    await bridge.ensure()
    ui = await UI(port, "w" * 48, "File A").start()
    try:
        await ui.wait_state("Wrong token")
        assert not (await bridge.status())["pluginConnected"]
    finally:
        await ui.stop()
        await bridge.close()


@pytest.mark.anyio
async def test_ui_does_not_trust_a_squatter():
    """A program on the port that cannot sign responses gets no job run in Figma."""
    from fastapi import FastAPI
    import uvicorn

    port = free_port()
    app = FastAPI()

    @app.api_route("/{path:path}", methods=["GET", "POST", "OPTIONS"])
    async def anything(path: str):
        if path == "next":
            return {"id": "evil", "type": "run", "payload": {"code": "figma.root.children.forEach(p => p.remove())"}}
        return {}

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, lifespan="off"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    ui = await UI(port, TOKEN, "File A").start()
    try:
        await ui.wait_state("Port taken by another program")
        await asyncio.sleep(1.5)
        assert ui.jobs() == []
    finally:
        await ui.stop()
        server.should_exit = True
        await task
