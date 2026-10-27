"""Local-file checks shared by the MCP server and the CLI."""
from __future__ import annotations

import re
import struct
from pathlib import Path, PureWindowsPath

MAX_IMAGE_SIDE = 4096                 # figma.createImage limit
MAX_IMAGE_BYTES = 40 * 1024 * 1024    # stays well under the relay's 64 MB request limit once base64-encoded
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_DRIVE = re.compile(r"[A-Za-z]:")
_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


class ImageCheckError(ValueError):
    pass


def _local_only(raw: str) -> None:
    """Allow only drive-letter paths and relative paths. Everything else (\\\\host\\share, \\\\?\\..., \\\\.\\...,
    \\??\\UNC\\..., /??/...) is refused before anything touches the filesystem, so no SMB/WebDAV connection
    (and no NTLM credential exchange) can be triggered."""
    pw = PureWindowsPath(raw.strip())
    if pw.drive:
        if not _DRIVE.fullmatch(pw.drive):
            raise ImageCheckError("Network (UNC) and device paths are not allowed; use a local path.")
    elif pw.root:
        raise ImageCheckError("Use a drive-letter path (like C:\\folder\\file.png) or a relative path.")


def local_path(raw: str) -> Path:
    _local_only(raw)
    path = Path(raw.strip()).expanduser()
    _local_only(str(path))            # ~ can expand to a network home directory
    return path


def image_size(data: bytes) -> tuple[str, int, int] | None:
    """(kind, width, height) from PNG / GIF / JPEG headers, or None if unrecognised or unreadable."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24])
        return "png", w, h
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        w, h = struct.unpack("<HH", data[6:10])
        return "gif", w, h
    if data[:2] == b"\xff\xd8":
        i = 2
        n = len(data)
        while i + 1 < n:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker == 0xFF:                 # fill byte before a marker
                i += 1
                continue
            if marker == 0x00 or marker == 0x01 or 0xD0 <= marker <= 0xD8:
                i += 2                         # stuffed byte / standalone markers
                continue
            if marker in (0xD9, 0xDA):         # end of image / start of scan: no frame header found
                return None
            if i + 4 > n:
                return None
            seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
            if marker in _SOF:
                if i + 9 > n:
                    return None
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return "jpg", w, h
            i += 2 + seg_len
        return None
    return None


def check_image_file(path: str) -> tuple[str, bytes | str]:
    """Validate a local image for Figma. Returns ("svg", text) or ("raster", bytes)."""
    file = local_path(path)
    try:
        if not file.is_file():
            raise ImageCheckError(f"No such file: {file}")
        ext = file.suffix.lower()
        if ext == ".svg":
            try:
                return "svg", file.read_text(encoding="utf-8-sig")
            except UnicodeDecodeError:
                raise ImageCheckError("The SVG is not UTF-8 text.") from None
        if ext == ".webp":
            raise ImageCheckError("Figma cannot import WebP images; convert it to PNG or JPG first.")
        if ext not in (".png", ".jpg", ".jpeg", ".gif"):
            raise ImageCheckError(f"Unsupported image type {ext!r}; use PNG, JPG, GIF or SVG.")
        size = file.stat().st_size
        if size > MAX_IMAGE_BYTES:
            raise ImageCheckError(f"The image is {size / 1e6:.1f} MB; the limit is {MAX_IMAGE_BYTES / 1e6:.0f} MB.")
        data = file.read_bytes()
    except OSError as exc:
        raise ImageCheckError(f"Cannot read {file}: {exc.strerror or exc}") from None
    info = image_size(data)
    if info is None:
        raise ImageCheckError("Could not read the image dimensions: the file is not a valid PNG, JPEG or GIF.")
    _, w, h = info
    if max(w, h) > MAX_IMAGE_SIDE:
        raise ImageCheckError(f"The image is {w}x{h}; Figma accepts at most {MAX_IMAGE_SIDE} px per side. Downscale it first.")
    return "raster", data


def safe_output_path(save_path: str, fmt: str, overwrite: bool) -> Path:
    """A local file path whose extension matches the image format; refuses UNC paths and silent overwrites."""
    path = local_path(save_path).resolve()
    allowed = {".jpg", ".jpeg"} if fmt == "JPG" else {".png"}
    if path.suffix.lower() not in allowed:
        raise ImageCheckError(f"The output file must end in {' or '.join(sorted(allowed))} for a {fmt} image.")
    if path.exists() and not path.is_file():
        raise ImageCheckError(f"{path} is not a regular file.")
    if path.exists() and not overwrite:
        raise ImageCheckError(f"{path} already exists; pass overwrite=true to replace it.")
    return path


def safe_file_name(name: str | None, ext: str) -> str:
    """A file name (no directories) built from an untrusted layer name; safe on Windows."""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "").strip(" .")[:120] or "node"
    if s.split(".")[0].upper() in _WIN_RESERVED:
        s = "_" + s
    return f"{s}.{ext}"


__all__ = ["ImageCheckError", "check_image_file", "image_size", "local_path", "safe_output_path", "safe_file_name"]
