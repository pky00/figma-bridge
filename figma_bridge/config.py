"""Shared settings: address, access token, timeouts and limits."""
from __future__ import annotations

import os
import re
import secrets
import time
from pathlib import Path

HOST = "127.0.0.1"          # the relay listens here (and on ::1 when available); the plugin calls http://localhost
HOST6 = "::1"
PLUGIN_ORIGIN_HOST = "localhost"


def _port() -> int:
    raw = os.environ.get("FIGMA_BRIDGE_PORT", "3789").strip()
    if not raw.isdigit() or not 0 < int(raw) < 65536:
        raise SystemExit(f"figma-bridge: FIGMA_BRIDGE_PORT must be a port number, got {raw!r}")
    return int(raw)


PORT = _port()
CONFIG_DIR = Path(os.environ.get("FIGMA_BRIDGE_HOME") or (Path.home() / ".figma-bridge")).expanduser().resolve()
TOKEN_FILE = CONFIG_DIR / "token"

POLL_HOLD_S = 20.0            # how long a plugin long-poll is held open before answering 204
PLUGIN_STALE_S = 45.0         # the plugin counts as connected if it polled within this window
DEFAULT_JOB_TIMEOUT_S = 60.0
MAX_JOB_TIMEOUT_S = 600.0
DEFAULT_WAIT_FOR_PLUGIN_S = 5.0
MAX_BODY_BYTES = 64 * 1024 * 1024
AUTH_WINDOW_S = 120.0         # accepted clock difference for signed requests
MIN_TOKEN_LEN = 32


class TokenError(RuntimeError):
    pass


def _validate(token: str, where: str) -> str:
    if len(token) < MIN_TOKEN_LEN or not re.fullmatch(r"[\x21-\x7e]+", token):
        raise TokenError(f"The token in {where} is missing, too short or malformed "
                         f"(need {MIN_TOKEN_LEN}+ printable characters). Run `figma-bridge token --reset`.")
    return token


def _read_token_file() -> str:
    for _ in range(40):                              # another process may be writing it right now
        text = TOKEN_FILE.read_text(encoding="utf-8-sig").strip()
        if text:
            return text
        time.sleep(0.05)
    return ""


def _create_token_file() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return                                       # someone else created it first
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(secrets.token_hex(24) + "\n")


class TokenSource:
    """The shared secret. FIGMA_BRIDGE_TOKEN wins; otherwise ~/.figma-bridge/token, created on first use and
    re-read whenever the file changes (so `figma-bridge token --reset` revokes the old token in running relays)."""

    def __init__(self, fixed: str | None = None) -> None:
        self._fixed = _validate(fixed.strip(), "the given token") if fixed else None
        self._cached: str | None = None
        self._mtime: int | None = None

    def get(self) -> str:
        if self._fixed:
            return self._fixed
        env = os.environ.get("FIGMA_BRIDGE_TOKEN")
        if env is not None:
            try:
                return _validate(env.strip(), "FIGMA_BRIDGE_TOKEN")
            except TokenError:
                raise TokenError("FIGMA_BRIDGE_TOKEN is set but empty or invalid (it needs 32+ printable characters). "
                                 "Fix or remove it (e.g. in your MCP config's \"env\" block); `figma-bridge token --reset` "
                                 "does not change it.") from None
        if not TOKEN_FILE.exists():
            _create_token_file()
        mtime = TOKEN_FILE.stat().st_mtime_ns
        if self._cached is None or mtime != self._mtime:
            self._cached = _validate(_read_token_file(), str(TOKEN_FILE))
            self._mtime = mtime
        return self._cached


def load_token() -> str:
    return TokenSource().get()


def reset_token() -> str:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(24)
    tmp = TOKEN_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    os.replace(tmp, TOKEN_FILE)                      # atomic: readers never see a half-written file
    return token


def clamp_timeout(seconds: float | None) -> float:
    try:
        s = float(seconds) if seconds is not None else DEFAULT_JOB_TIMEOUT_S
    except (TypeError, ValueError):
        s = DEFAULT_JOB_TIMEOUT_S
    if not s > 0:
        s = DEFAULT_JOB_TIMEOUT_S
    return min(max(s, 1.0), MAX_JOB_TIMEOUT_S)


NOT_CONNECTED = (
    "The Figma plugin is not connected. Open the Figma desktop app, open a file and run "
    "Plugins > Development > Figma Bridge (the first time, paste the token printed by `figma-bridge token --show`)."
)
