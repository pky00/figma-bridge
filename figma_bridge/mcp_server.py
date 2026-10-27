"""MCP server (stdio, official `mcp` SDK v2 `MCPServer`) exposing the Figma desktop app through the Figma Bridge plugin.

Start it with `figma-bridge mcp` (or `python -m figma_bridge mcp`). It hosts the relay the plugin connects to.
Only MCP protocol messages go to stdout; logs go to stderr.
"""
from __future__ import annotations

import base64
import json
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from . import __version__
from .client import Bridge, BridgeError
from .relay import finite
from .images import ImageCheckError, check_image_file, safe_output_path

log = logging.getLogger("figma_bridge.mcp")

INSTRUCTIONS = """Controls the Figma desktop app through the "Figma Bridge" development plugin, which must be running in the file to edit.
Call figma_status first. figma_run executes Figma Plugin API JavaScript in that file (create, edit, inspect, delete);
figma_screenshot renders a node so you can look at it; figma_add_image puts a local image into the file.
Everything is local: there is no rate limit and nothing is sent to Figma's cloud API."""

RUN_DESCRIPTION = """Execute JavaScript in the open Figma file with the full Figma Plugin API.

The code is the body of an async function: top-level `await` works and `return` sends a value back (JSON-serialised;
nodes come back as {id, type, name}; Uint8Array as {$bytes: base64}). In scope: `figma`, `input` (the optional `input`
argument, handy for large data) and `console` (its output is returned in `logs`).

Rules that avoid most failures:
- The plugin uses dynamic page loading: use figma.getNodeByIdAsync, `await figma.setCurrentPageAsync(page)`, page.loadAsync().
- Load every font before touching text: `await figma.loadFontAsync({family, style})`; check exact style names with
  figma.listAvailableFontsAsync() ("Semi Bold" vs "SemiBold" differs per family).
- Colours are 0-1 floats; fills/strokes are read-only arrays: build a new array and assign it.
- New top-level nodes land at (0,0) of the current page; position them.
- Code must finish within timeout_ms: a promise that never settles is abandoned (it may keep running inside Figma).
- Return the ids of what you create or change. Never call figma.closePlugin(): it disconnects the bridge."""

bridge = Bridge()


@asynccontextmanager
async def lifespan(_server: MCPServer):
    try:
        mode = await bridge.ensure()
        log.info("relay ready (%s mode) on port %s", mode, bridge.port)
    except Exception as exc:  # keep the MCP server up; tools report the problem
        log.warning("relay not started: %s", exc)
    try:
        yield {}
    finally:
        await bridge.close()


mcp = MCPServer("figma-bridge", instructions=INSTRUCTIONS, version=__version__, lifespan=lifespan)


async def _job(type_: str, payload: dict, timeout_s: float | None = None) -> dict:
    try:
        out = await bridge.submit(type_, payload, timeout_s)
    except BridgeError as exc:
        raise ToolError(str(exc)) from None
    except Exception as exc:                       # never surface a bare "Error executing tool"
        raise ToolError(f"figma-bridge: {type(exc).__name__}: {exc}") from None
    if not out.get("ok"):
        logs = out.get("logs") or []
        raise ToolError("Figma error: " + str(out.get("error") or "unknown error")
                        + ("\n\nconsole output:\n" + "\n".join(map(str, logs)) if logs else ""))
    return out


@mcp.tool(description="Report whether the Figma plugin is connected and which file, page and selection it sees. Call this first.",
          structured_output=False)
async def figma_status() -> str:
    try:
        st = await bridge.status()
    except BridgeError as exc:
        raise ToolError(str(exc)) from None
    except Exception as exc:
        raise ToolError(f"figma-bridge: {type(exc).__name__}: {exc}") from None
    if not st.get("pluginConnected"):
        st["hint"] = "Open the Figma desktop app, open a file, and run Plugins > Development > Figma Bridge."
        return json.dumps(st, indent=2, ensure_ascii=True)
    try:
        out = await bridge.submit("status", {}, 15)
        if out.get("ok"):
            st["plugin"] = out.get("result")
    except BridgeError as exc:
        st["pluginError"] = str(exc)
    return json.dumps(st, indent=2, ensure_ascii=True)


@mcp.tool(description=RUN_DESCRIPTION, structured_output=False)
async def figma_run(
    code: Annotated[str, Field(description="JavaScript function body to run in the Figma plugin sandbox.")],
    input: Annotated[Any, Field(description="Optional JSON value available to the code as `input`.")] = None,
    timeout_ms: Annotated[int, Field(description="Max run time in ms (default 60000, max 600000).")] = 60000,
) -> str:
    if not code.strip():
        raise ToolError("`code` is empty.")
    out = await _job("run", {"code": code, "input": input}, timeout_ms / 1000)
    body = {"result": out.get("result")}
    if out.get("logs"):
        body["logs"] = out["logs"]
    return json.dumps(body, ensure_ascii=True)


@mcp.tool(description="Render a node (frame, component, group, ...) to PNG or JPG and return the image so you can see it. "
                      "Defaults to the first selected node. Pages cannot be exported; pass a frame. Local and unlimited.")
async def figma_screenshot(
    node_id: Annotated[str | None, Field(description='Node id like "12:34". Omit to use the current selection.')] = None,
    scale: Annotated[float, Field(description="Export scale (default 1); reduced automatically to respect max_dimension.")] = 1.0,
    max_dimension: Annotated[int, Field(description="Largest allowed width/height in pixels (default 1600, max 8192).")] = 1600,
    format: Annotated[str, Field(description="PNG or JPG (default PNG).")] = "PNG",
    save_path: Annotated[str | None, Field(description="Optional local file (.png/.jpg) to also save the image to.")] = None,
    overwrite: Annotated[bool, Field(description="Allow replacing an existing file at save_path.")] = False,
):
    fmt = "JPG" if format.upper() in ("JPG", "JPEG") else "PNG"
    try:
        scale = finite(scale, "scale")
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    dest = None
    if save_path:
        try:
            dest = safe_output_path(save_path, fmt, overwrite)
        except ImageCheckError as exc:
            raise ToolError(str(exc)) from None
    out = await _job("screenshot", {"nodeId": node_id, "scale": scale,
                                    "maxDimension": max(16, min(int(max_dimension), 8192)), "format": fmt}, 120)
    r = out["result"]
    data = base64.b64decode(r["base64"])
    saved = None
    if dest is not None:
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb" if overwrite else "xb") as f:
                f.write(data)
            saved = str(dest)
        except OSError as exc:
            raise ToolError(f"Rendered the node but could not save it: {exc}") from None
    meta = {k: r.get(k) for k in ("id", "name", "type", "width", "height", "scale")}
    meta["savedTo"] = saved
    return [Image(data=data, format="jpeg" if fmt == "JPG" else "png"), json.dumps(meta, ensure_ascii=True)]


@mcp.tool(description="Put a local image (PNG, JPG or GIF up to 4096 px per side, or SVG) into the open Figma file. Raster "
                      "images become an image fill: on node_id if given, otherwise on a new rectangle at the viewport centre "
                      "(or x/y). SVGs become editable vector layers. Returns the imageHash, which figma_run code can reuse: "
                      'node.fills = [{type: "IMAGE", imageHash, scaleMode: "FILL"}].',
          structured_output=False)
async def figma_add_image(
    path: Annotated[str, Field(description="Path of the local image file.")],
    node_id: Annotated[str | None, Field(description="Existing node to receive the image fill (raster only).")] = None,
    scale_mode: Annotated[str, Field(description="FILL, FIT, CROP or TILE (default FILL).")] = "FILL",
    name: Annotated[str | None, Field(description="Layer name for a newly created node.")] = None,
    x: Annotated[float | None, Field(description="X of a newly created node.")] = None,
    y: Annotated[float | None, Field(description="Y of a newly created node.")] = None,
    create_node: Annotated[bool, Field(description="Raster without node_id: false = only add the image and return its hash.")] = True,
) -> str:
    try:
        kind, data = check_image_file(path)
        x = finite(x, "x") if x is not None else None
        y = finite(y, "y") if y is not None else None
    except (ImageCheckError, ValueError) as exc:
        raise ToolError(str(exc)) from None
    except Exception as exc:
        raise ToolError(f"figma-bridge: {type(exc).__name__}: {exc}") from None
    payload: dict[str, Any] = {"nodeId": node_id, "scaleMode": scale_mode.upper(), "name": name, "x": x, "y": y,
                               "createNode": create_node}
    if kind == "svg":
        payload["svg"] = data
    else:
        payload["base64"] = base64.b64encode(data).decode("ascii")
    out = await _job("image", payload, 120)
    return json.dumps(out.get("result"), ensure_ascii=True)


def run() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="[figma-bridge] %(message)s")
    mcp.run()  # stdio transport
