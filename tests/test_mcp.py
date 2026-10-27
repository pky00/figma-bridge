"""End to end: the real MCP server over stdio, hosting the relay, with the fake plugin connected."""
import asyncio
import json
import os
import socket
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from fake_plugin import run_fake_plugin

TOKEN = "m" * 48


@pytest.fixture
def anyio_backend():
    return "asyncio"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.anyio
async def test_mcp_tools_end_to_end(tmp_path):
    port = free_port()
    env = {**os.environ, "FIGMA_BRIDGE_PORT": str(port), "FIGMA_BRIDGE_TOKEN": TOKEN, "PYTHONUNBUFFERED": "1"}
    params = StdioServerParameters(command=sys.executable, args=["-m", "figma_bridge", "mcp"], env=env)
    stop = asyncio.Event()
    plugin = asyncio.create_task(run_fake_plugin(f"http://127.0.0.1:{port}", TOKEN, stop))
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                assert init.server_info.name == "figma-bridge"
                tools = {t.name for t in (await session.list_tools()).tools}
                assert tools == {"figma_status", "figma_run", "figma_screenshot", "figma_add_image"}

                status = await session.call_tool("figma_status", {})
                assert not status.is_error
                for _ in range(40):                       # the fake plugin needs a moment to find the relay
                    if json.loads(status.content[0].text)["pluginConnected"]:
                        break
                    await asyncio.sleep(0.25)
                    status = await session.call_tool("figma_status", {})
                st = json.loads(status.content[0].text)
                assert st["pluginConnected"] and st["plugin"]["fileName"] == "Test file"

                run = await session.call_tool("figma_run", {"code": "return 42", "input": {"x": 1}})
                assert not run.is_error
                body = json.loads(run.content[0].text)
                assert body["result"] == {"echoCode": "return 42", "input": {"x": 1}} and body["logs"] == ["hello from figma"]

                bad = await session.call_tool("figma_run", {"code": "throw"})
                assert bad.is_error and "boom" in bad.content[0].text

                shot_path = tmp_path / "shot.png"
                shot = await session.call_tool("figma_screenshot", {"node_id": "1:2", "save_path": str(shot_path)})
                assert not shot.is_error
                assert shot.content[0].type == "image" and shot.content[0].mime_type == "image/png"
                assert shot_path.read_bytes().startswith(b"\x89PNG")

                img_file = tmp_path / "pic.png"
                img_file.write_bytes(shot_path.read_bytes())
                img = await session.call_tool("figma_add_image", {"path": str(img_file), "node_id": "5:5"})
                assert not img.is_error and json.loads(img.content[0].text)["imageHash"] == "abc123"
    finally:
        stop.set()
        plugin.cancel()
