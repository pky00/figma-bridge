"""Command line: `figma-bridge <command>` (or `python -m figma_bridge <command>`).

  serve                 run the relay on its own (the MCP server also hosts it)
  mcp                   run the MCP server on stdio (what you register with your agent)
  status                is the plugin connected, and what file is open
  run FILE|-            run Plugin API JavaScript from a file (or stdin) in the open Figma file
  screenshot [NODE_ID]  export a node to PNG/JPG
  image PATH            put a local image into the file
  token [--show|--reset]
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import sys
from pathlib import Path

from . import __version__
from .client import Bridge, BridgeError
from .config import PORT, TOKEN_FILE, TokenError, load_token, reset_token
from .images import ImageCheckError, check_image_file, safe_file_name, safe_output_path
from .relay import finite

PLUGIN_WAIT_S = 15.0  # when the CLI hosts the relay itself, give the plugin time to find it


def _print(obj) -> None:
    # ASCII-only JSON is valid whatever code page the reader decodes with (Windows pipes default to cp1252).
    print(json.dumps(obj, indent=2, ensure_ascii=True))


def _read_text(path: str) -> str:
    """UTF-8 (with or without BOM) or UTF-16 with BOM, which Windows PowerShell 5.1 writes by default."""
    data = sys.stdin.buffer.read() if path == "-" else Path(path).read_bytes()
    try:
        if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
            return data.decode("utf-16")
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        where = "stdin" if path == "-" else path
        raise BridgeError(f"{where} is not UTF-8 or UTF-16 text (in PowerShell 5.1 save it with -Encoding utf8).") from None


async def _with_bridge(fn):
    bridge = Bridge()
    try:
        await bridge.ensure()
        return await fn(bridge)
    finally:
        await bridge.close()


async def _job(bridge: Bridge, type_: str, payload: dict, timeout_s: float | None = None) -> dict:
    wait = PLUGIN_WAIT_S if bridge.mode == "host" else 5.0
    return await bridge.submit(type_, payload, timeout_s, wait_for_plugin_s=wait)


def _report(out: dict) -> int:
    for line in out.get("logs") or []:
        print(line, file=sys.stderr)
    if out.get("ok"):
        _print(out.get("result"))
        return 0
    print("Figma error: " + str(out.get("error")), file=sys.stderr)
    return 1


def cmd_serve(_args) -> int:
    from .relay import RelayServer

    from .relay import port_in_use

    async def main():
        server = RelayServer()
        try:
            await server.start()
        except OSError as exc:
            if port_in_use(exc):
                raise BridgeError(f"Port {PORT} is already in use (is an MCP session or another `serve` running?).") from None
            raise
        print(f"figma-bridge relay {__version__} on http://localhost:{server.port} (Ctrl+C to stop)", file=sys.stderr)
        try:
            await asyncio.Event().wait()
        finally:
            await server.stop()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    return 0


def cmd_mcp(_args) -> int:
    from .mcp_server import run
    run()
    return 0


def cmd_status(_args) -> int:
    async def go(bridge: Bridge):
        st = await bridge.status()
        if bridge.mode == "host" and not st.get("pluginConnected"):
            try:
                st = await bridge.wait_for_plugin(PLUGIN_WAIT_S)
            except BridgeError:
                pass
        if st.get("pluginConnected"):
            out = await bridge.submit("status", {}, 15)
            if out.get("ok"):
                st["plugin"] = out["result"]
        return st

    _print(asyncio.run(_with_bridge(go)))
    return 0


def cmd_run(args) -> int:
    finite(args.timeout, "--timeout")
    code = _read_text(args.file)
    data = None
    if args.input:
        data = json.loads(_read_text(args.input[1:]) if args.input.startswith("@") else args.input)
    out = asyncio.run(_with_bridge(lambda b: _job(b, "run", {"code": code, "input": data}, args.timeout)))
    return _report(out)


def cmd_screenshot(args) -> int:
    ext = "jpg" if args.format == "JPG" else "png"
    finite(args.scale, "--scale")
    dest = safe_output_path(args.out, args.format, args.overwrite) if args.out else None
    payload = {"nodeId": args.node_id, "scale": args.scale, "maxDimension": max(16, min(args.max_dimension, 8192)),
               "format": args.format}
    out = asyncio.run(_with_bridge(lambda b: _job(b, "screenshot", payload, 120)))
    if not out.get("ok"):
        return _report(out)
    r = out["result"]
    if dest is None:   # name it after the layer, sanitised, in the current folder
        dest = Path.cwd() / safe_file_name(r.get("name"), ext)
        if dest.exists() and not args.overwrite:
            dest = Path.cwd() / safe_file_name(f"{r.get('name')}-{r.get('id', '').replace(':', '-')}", ext)
    with open(dest, "wb" if args.overwrite else "xb") as f:
        f.write(base64.b64decode(r["base64"]))
    _print({k: r.get(k) for k in ("id", "name", "type", "width", "height", "scale")} | {"savedTo": str(dest.resolve())})
    return 0


def cmd_image(args) -> int:
    kind, data = check_image_file(args.path)
    payload = {"nodeId": args.node, "scaleMode": args.mode, "name": args.name or Path(args.path).stem, "createNode": True}
    if kind == "svg":
        payload["svg"] = data
    else:
        payload["base64"] = base64.b64encode(data).decode("ascii")
    out = asyncio.run(_with_bridge(lambda b: _job(b, "image", payload, 120)))
    return _report(out)


def cmd_token(args) -> int:
    if os.environ.get("FIGMA_BRIDGE_TOKEN") is not None:
        if args.reset:
            print("FIGMA_BRIDGE_TOKEN is set and overrides the token file; change that variable instead.", file=sys.stderr)
            return 2
        print("note: FIGMA_BRIDGE_TOKEN is set and overrides the token file.", file=sys.stderr)
    token = reset_token() if args.reset else load_token()
    print(f"token file: {TOKEN_FILE}")
    if args.show or args.reset:
        print(token)
    else:
        print("(use --show to print it; paste it into the Figma Bridge plugin the first time)")
    if args.reset:
        print("Running relays switch to the new token automatically. Paste it into the Figma Bridge plugin "
              "(click Connect) to reconnect.", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(prog="figma-bridge", description="Local bridge between AI agents and the Figma desktop app.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the relay on its own").set_defaults(fn=cmd_serve)
    sub.add_parser("mcp", help="run the MCP server (stdio)").set_defaults(fn=cmd_mcp)
    sub.add_parser("status", help="show the connection and the open file").set_defaults(fn=cmd_status)
    r = sub.add_parser("run", help="run Plugin API JavaScript in Figma")
    r.add_argument("file", help="JavaScript file, or - for stdin (UTF-8)")
    r.add_argument("--input", help="JSON value (or @file.json) available to the code as `input`")
    r.add_argument("--timeout", type=float, default=60.0, help="seconds (default 60)")
    r.set_defaults(fn=cmd_run)
    s = sub.add_parser("screenshot", help="export a node to an image file")
    s.add_argument("node_id", nargs="?", help="node id like 12:34 (default: current selection)")
    s.add_argument("--scale", type=float, default=1.0)
    s.add_argument("--max-dimension", type=int, default=4096)
    s.add_argument("--format", choices=["PNG", "JPG"], default="PNG")
    s.add_argument("--out", help="output file (default: <layer name>.png in the current folder)")
    s.add_argument("--overwrite", action="store_true", help="replace an existing file")
    s.set_defaults(fn=cmd_screenshot)
    i = sub.add_parser("image", help="put a local image into the open file")
    i.add_argument("path")
    i.add_argument("--node", help="existing node id to receive the image fill")
    i.add_argument("--mode", choices=["FILL", "FIT", "CROP", "TILE"], default="FILL")
    i.add_argument("--name", help="layer name")
    i.set_defaults(fn=cmd_image)
    t = sub.add_parser("token", help="show or reset the shared token")
    t.add_argument("--show", action="store_true")
    t.add_argument("--reset", action="store_true")
    t.set_defaults(fn=cmd_token)
    args = p.parse_args(argv)
    if args.cmd != "mcp":
        logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="[figma-bridge] %(message)s")
    try:
        return args.fn(args)
    except (BridgeError, TokenError, ImageCheckError) as exc:
        print(f"figma-bridge: {exc}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"figma-bridge: --input is not valid JSON ({exc}). In PowerShell, prefer --input @file.json.", file=sys.stderr)
        return 2
    except FileExistsError as exc:
        print(f"figma-bridge: {exc.filename} already exists; use --overwrite or another --out.", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"figma-bridge: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"figma-bridge: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
