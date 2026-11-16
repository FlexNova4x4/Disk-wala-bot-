from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

DISKWALA_HOSTS = {"diskwala.com", "www.diskwala.com"}
DISKWALA_PATH_RE = re.compile(r"^/app/([A-Za-z0-9_-]{6,128})/?$")
VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".3gp", ".ts", ".flv"
}


def parse_diskwala_link(text: str) -> tuple[str, str] | None:
    """Return (canonical file key, normalized URL) for strict DiskWala links."""
    candidate = text.strip()
    if not candidate or any(ch.isspace() for ch in candidate):
        return None
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme.lower() != "https" or (parsed.hostname or "").lower() not in DISKWALA_HOSTS:
        return None
    if parsed.username or parsed.password or port not in (None, 443):
        return None
    match = DISKWALA_PATH_RE.fullmatch(parsed.path)
    if not match:
        return None
    file_key = match.group(1)
    normalized = f"https://www.diskwala.com/app/{file_key}"
    return file_key, normalized


def safe_filename(name: str) -> str:
    name = name.replace("\\", "/").split("/")[-1].strip()
    name = re.sub(r'[\x00-\x1f<>:"|?*]', "_", name)
    name = name.strip(" .")
    if not name:
        name = "video.mp4"
    if len(name) > 180:
        stem, suffix = os.path.splitext(name)
        name = stem[: max(1, 180 - len(suffix))] + suffix
    if Path(name).suffix.lower() not in VIDEO_EXTENSIONS:
        name += ".mp4"
    return name
