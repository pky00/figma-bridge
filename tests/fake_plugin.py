"""A stand-in for the Figma plugin's UI loop (plugin/ui.html), used by the tests.

It talks to the relay exactly like the real plugin: requests signed over method, path, session and body,
POST /hello, long-poll GET /next (verifying the relay's response signature), POST /result, 410 -> hello again,
409 -> stop (another file took over). Jobs are answered with canned results instead of running in Figma.
"""
from __future__ import annotations

import asyncio
import base64
import json
import secrets

import httpx

from figma_bridge.auth import request_headers, verify_response

# 1x1 transparent PNG
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def answer(job: dict) -> dict:
    kind, payload = job["type"], job.get("payload") or {}
    if kind == "run":
        code = payload.get("code", "")
        if "throw" in code:
            return {"ok": False, "error": "Error: boom", "logs": ["about to throw"]}
        if "surrogate" in code:   # what JSON.stringify sends for an emoji cut in half
            return {"ok": True, "result": {"text": "cut \ud83d"}, "logs": ["log \udc00 half"]}
        return {"ok": True, "result": {"echoCode": code, "input": payload.get("input")}, "logs": ["hello from figma"]}
    if kind == "status":
        return {"ok": True, "result": {"fileName": "Test file", "editorType": "figma", "currentPage": {"id": "0:1", "name": "Page 1"}, "selection": []}}
    if kind == "screenshot":
        return {"ok": True, "result": {"id": payload.get("nodeId") or "1:2", "name": "Frame", "type": "FRAME", "width": 1, "height": 1,
                                        "scale": 1, "mimeType": "image/png", "base64": base64.b64encode(PNG_1PX).decode()}}
    if kind == "image":
        return {"ok": True, "result": {"imageHash": "abc123", "width": 1, "height": 1, "nodeId": payload.get("nodeId") or "9:9"}}
    return {"ok": False, "error": f"Unknown job type: {kind}"}


class FakePlugin:
    def __init__(self, base_url: str, token: str, *, file_name: str = "Test file", silent: bool = False) -> None:
        self.base_url = base_url
        self.token = token
        self.session = secrets.token_hex(16)
        self.file_name = file_name
        self.silent = silent            # take jobs but never answer (timeout tests)
        self.handled: list[dict] = []
        self.superseded = False
        self.hellos = 0

    async def call(self, client: httpx.AsyncClient, method: str, path: str, body: dict | None = None):
        content = json.dumps(body).encode() if body is not None else b""     # json.dumps escapes lone surrogates
        headers, nonce = request_headers(self.token, method, path, content, self.session)
        if body is not None:
            headers["Content-Type"] = "application/json"
        r = await client.request(method, self.base_url + path, content=content or None, headers=headers)
        ok = verify_response(self.token, nonce, r.status_code, r.content, r.headers.get("x-bridge-sig"))
        return r, ok

    async def run(self, stop: asyncio.Event) -> None:
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            announced = False
            while not stop.is_set():
                try:
                    if not announced:
                        r, ok = await self.call(client, "POST", "/hello", {"fileName": self.file_name, "editorType": "figma"})
                        assert ok and r.status_code == 204, (r.status_code, r.text)
                        announced = True
                        self.hellos += 1
                    r, ok = await self.call(client, "GET", "/next")
                    assert ok, "unsigned response from relay"
                    if r.status_code == 409:
                        self.superseded = True
                        return
                    if r.status_code == 410:
                        announced = False
                        continue
                    if r.status_code == 204:
                        continue
                    job = r.json()
                    self.handled.append(job)
                    if self.silent:
                        continue
                    r2, ok2 = await self.call(client, "POST", "/result", {"id": job["id"], **answer(job)})
                    assert ok2 and r2.status_code == 204
                except httpx.HTTPError:
                    announced = False
                    await asyncio.sleep(0.2)


async def run_fake_plugin(base_url: str, token: str, stop: asyncio.Event, **kw) -> FakePlugin:
    plugin = FakePlugin(base_url, token, **kw)
    await plugin.run(stop)
    return plugin
