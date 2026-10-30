"""Small shared helpers (all file I/O is explicitly UTF-8 so Indic text is safe on Windows)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_id() -> str:
    return time.strftime("%y%m%d") + "-" + uuid.uuid4().hex[:6]


def sha1(*parts: Any) -> str:
    """Stable SHA-1 hex digest of several parts (used for cache keys); parts are separated so ('ab','c') differs from
    ('a','bc').
    """
    h = hashlib.sha1()
    for p in parts:
        if isinstance(p, bytes):
            h.update(p)
        else:
            h.update(str(p).encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


def slugify(text: str, default: str = "podcast") -> str:
    s = re.sub(r"[^\w\-]+", "-", text.strip().lower(), flags=re.UNICODE).strip("-")
    return s[:60] or default


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write a file so a crash cannot leave it half-written: write to a temporary file in the same folder, then replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def read_text(path: Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def write_json(path: Path, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2))


def read_json(path: Path) -> Any:
    return json.loads(read_text(path))


_SECRET_PATTERNS = [
    re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.=:]{8,}", re.I),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(api[_-]?key[\"'\s:=]+)[A-Za-z0-9_\-\.]{8,}", re.I),
]


def redact(text: str, *secrets: str) -> str:
    """Strip anything that looks like an API key out of a string before it is logged or shown."""
    out = text or ""
    for s in secrets:
        if s and len(s) >= 6:
            out = out.replace(s, "***")
    for pat in _SECRET_PATTERNS:
        out = pat.sub(lambda m: (m.group(1) if m.lastindex else "") + "***", out)
    return out


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def fmt_time(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def update_env_file(path, updates: dict[str, str]) -> None:
    """Set KEY=value lines in a .env file (UTF-8), keeping every other line and comment untouched."""
    from pathlib import Path

    p = Path(path)
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    done: set[str] = set()
    out: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip().lstrip("#").strip()
        if "=" in line and key in updates and not line.lstrip().startswith("#"):
            out.append(f"{key}={updates[key]}")
            done.add(key)
        else:
            out.append(line)
    for k, v in updates.items():
        if k not in done:
            out.append(f"{k}={v}")
    atomic_write_text(p, "\n".join(out).rstrip("\n") + "\n")
