"""Request/response signing. The token itself never goes over the wire.

Request:  X-Bridge-Ts (ms), X-Bridge-Nonce (hex), X-Bridge-Auth =
          HMAC-SHA256(token, "METHOD\\npath\\nts\\nnonce\\nsession\\nsha256hex(body)")
Response: X-Bridge-Sig = HMAC-SHA256(token, "resp\\nnonce\\nstatus\\n" + body)

The relay checks requests (so other programs and web pages cannot send jobs or forge results); the plugin and the
Python client check responses (so a program squatting on the port can neither feed the plugin code nor learn the
token). plugin/ui.html implements the same scheme in JavaScript.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time

_HEX64 = re.compile(r"[0-9a-f]{64}")


def _mac(token: str, data: bytes) -> str:
    return hmac.new(token.encode("utf-8"), data, hashlib.sha256).hexdigest()


def request_mac(token: str, method: str, path: str, ts: str, nonce: str, session: str = "", body: bytes = b"") -> str:
    body_hash = hashlib.sha256(body).hexdigest()
    return _mac(token, f"{method.upper()}\n{path}\n{ts}\n{nonce}\n{session}\n{body_hash}".encode("utf-8"))


def response_mac(token: str, nonce: str, status: int, body: bytes) -> str:
    return _mac(token, f"resp\n{nonce}\n{status}\n".encode("utf-8") + body)


def request_headers(token: str, method: str, path: str, body: bytes = b"", session: str = "") -> tuple[dict[str, str], str]:
    ts = str(int(time.time() * 1000))
    nonce = secrets.token_hex(16)
    headers = {
        "X-Bridge-Ts": ts,
        "X-Bridge-Nonce": nonce,
        "X-Bridge-Auth": request_mac(token, method, path, ts, nonce, session, body),
    }
    if session:
        headers["X-Bridge-Session"] = session
    return headers, nonce


def is_mac(value: str | None) -> bool:
    return bool(value) and _HEX64.fullmatch(value) is not None


def verify_response(token: str, nonce: str, status: int, body: bytes, sig: str | None) -> bool:
    return is_mac(sig) and hmac.compare_digest(response_mac(token, nonce, status, body), sig)


class NonceCache:
    """Remembers recent nonces so a captured request cannot be replayed."""

    def __init__(self, ttl_s: float) -> None:
        self.ttl = ttl_s
        self.seen: dict[str, float] = {}

    def add(self, nonce: str) -> bool:
        now = time.monotonic()
        if len(self.seen) > 4096:
            self.seen = {n: t for n, t in self.seen.items() if t > now}
        if self.seen.get(nonce, 0) > now:
            return False
        self.seen[nonce] = now + self.ttl
        return True
