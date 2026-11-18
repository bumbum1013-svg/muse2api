"""Local media storage for generated images/videos and helpers for image inputs."""

from __future__ import annotations

import base64
import mimetypes
import re
import uuid
from pathlib import Path

import httpx

from ..errors import InvalidRequest, NotFound

_EXT_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/mp4": ".m4a",
    "audio/ogg": ".ogg",
    "audio/flac": ".flac",
    "audio/aac": ".aac",
}


def ext_for(mime: str) -> str:
    return _EXT_BY_MIME.get(mime.split(";")[0].strip().lower(), ".bin")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_\-]+\.[A-Za-z0-9]+$")


class MediaStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, data: bytes, mime: str, prefix: str = "m") -> str:
        ext = _EXT_BY_MIME.get(mime.split(";")[0].strip().lower(), ".bin")
        name = f"{prefix}_{uuid.uuid4().hex[:16]}{ext}"
        (self.root / name).write_bytes(data)
        return name

    def path_of(self, name: str) -> Path:
        if not _SAFE_NAME.match(name):
            raise NotFound("media not found")
        p = self.root / name
        if not p.is_file():
            raise NotFound("media not found")
        return p

    @staticmethod
    def mime_of(name: str) -> str:
        return mimetypes.guess_type(name)[0] or "application/octet-stream"


async def load_image_ref(ref: str, *, timeout: float = 20.0) -> tuple[bytes, str]:
    """Decode a data URL or download an http(s) URL. Returns ``(bytes, mime)``."""
    ref = ref.strip()
    if ref.startswith("data:"):
        header, _, payload = ref.partition(",")
        mime = header[5:].split(";")[0] or "image/png"
        try:
            return base64.b64decode(payload), mime
        except ValueError as exc:
            raise InvalidRequest("invalid base64 image data") from exc
    if ref.startswith(("http://", "https://")):
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(ref)
            resp.raise_for_status()
            mime = resp.headers.get("content-type", "image/png").split(";")[0]
            return resp.content, mime
    try:
        return base64.b64decode(ref, validate=True), "image/png"
    except ValueError as exc:
        raise InvalidRequest("image must be a data URL, http(s) URL or base64 string") from exc


# Upload limits of the muse.ai web composer (maxFilesPerUpload / maxFileSize), as
# read by github.com/czg86389-hub/muse2api (MIT); reference videos may run 120 s.
MAX_REFERENCES = 10
MAX_REFERENCE_BYTES = 40 * 1024 * 1024
MAX_REFERENCE_TOTAL_BYTES = 100 * 1024 * 1024
REFERENCE_MIMES = {
    "image": ("image/png", "image/jpeg", "image/webp", "image/gif"),
    "video": ("video/mp4", "video/quicktime", "video/webm"),
    "audio": ("audio/mpeg", "audio/wav", "audio/mp4", "audio/ogg", "audio/flac", "audio/aac"),
}
_MIME_ALIASES = {"audio/mp3": "audio/mpeg", "audio/x-wav": "audio/wav", "audio/wave": "audio/wav",
                 "audio/x-m4a": "audio/mp4", "audio/m4a": "audio/mp4", "image/jpg": "image/jpeg",
                 "video/mov": "video/quicktime", "audio/x-flac": "audio/flac"}


def sniff_mime(data: bytes) -> str | None:
    """MIME from the file header, for inputs sent without a usable type."""
    head = data[:16]
    if head.startswith(b"\x89PNG"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand.startswith(b"qt"):
            return "video/quicktime"
        return "audio/mp4" if brand.startswith((b"M4A", b"M4B")) else "video/mp4"
    if head.startswith(b"ID3") or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mpeg"
    if head.startswith(b"OggS"):
        return "audio/ogg"
    if head.startswith(b"fLaC"):
        return "audio/flac"
    return None


async def load_media_refs(refs: list[str], *, timeout: float = 60.0) -> list[tuple[bytes, str]]:
    """Load reference files (images, videos, audio) in order, typed from their
    header when the declared type is missing or wrong, within muse.ai's limits."""
    if len(refs) > MAX_REFERENCES:
        raise InvalidRequest(f"at most {MAX_REFERENCES} reference files per request")
    out, total = [], 0
    for i, ref in enumerate(refs, 1):
        data, declared = await load_image_ref(ref, timeout=timeout)
        mime = sniff_mime(data) or _MIME_ALIASES.get(declared.lower(), declared.lower())
        if not any(mime in allowed for allowed in REFERENCE_MIMES.values()):
            raise InvalidRequest(f"reference {i}: unsupported type {mime}")
        if len(data) > MAX_REFERENCE_BYTES:
            raise InvalidRequest(f"reference {i} is {len(data) / 2**20:.0f} MiB; the limit is 40 MiB")
        total += len(data)
        if total > MAX_REFERENCE_TOTAL_BYTES:
            raise InvalidRequest("reference files exceed 100 MiB in total")
        out.append((data, mime))
    return out
