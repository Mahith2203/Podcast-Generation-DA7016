"""On-disk project storage. One folder per project:

  data/projects/<id>/project.json      state (settings, plan, segments + every script version, usage, log)
                      source/          original upload, extracted text, numbered units
                      segments/        per-segment audio (one file per script version)
                      final/           episode wav/mp3 + transcripts
  data/cache/tts/                      raw TTS responses shared by all projects (keyed by text+voice+lang+style)
"""
from __future__ import annotations

import re
import shutil
import threading
from pathlib import Path
from typing import Callable, Optional

from .models import Project, SourceUnit
from .util import atomic_write_bytes, atomic_write_text, now_iso, read_json, read_text, write_json

_ID_RE = re.compile(r"^[0-9a-z][0-9a-z\-]{4,40}$")


class ProjectNotFound(KeyError):
    """Raised when a project id has no folder on disk."""
    pass


class ProjectStore:
    """Reads and writes projects on disk (`data/projects/<id>/project.json` plus segment audio, final outputs and video),
    with a lock per project and a shared cache of voice clips.
    """
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.root = self.data_dir / "projects"
        self.cache_dir = self.data_dir / "cache" / "tts"
        self.root.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.Lock()

    # ------------------------------------------------------------------ paths
    def check_id(self, pid: str) -> str:
        if not isinstance(pid, str) or not _ID_RE.match(pid):
            raise ProjectNotFound(pid)
        return pid

    def dir(self, pid: str) -> Path:
        return self.root / self.check_id(pid)

    def exists(self, pid: str) -> bool:
        try:
            return (self.dir(pid) / "project.json").exists()
        except ProjectNotFound:
            return False

    def lock(self, pid: str) -> threading.RLock:
        with self._guard:
            return self._locks.setdefault(pid, threading.RLock())

    def cache_path(self, key: str) -> Path:
        return self.cache_dir / key[:2] / f"{key}.wav"

    # ------------------------------------------------------------------ project state
    def create(self, project: Project) -> None:
        d = self.dir(project.id)
        (d / "source").mkdir(parents=True, exist_ok=True)
        (d / "segments").mkdir(exist_ok=True)
        (d / "final").mkdir(exist_ok=True)
        self.save(project)

    def load(self, pid: str) -> Project:
        f = self.dir(pid) / "project.json"
        if not f.exists():
            raise ProjectNotFound(pid)
        return Project.model_validate_json(read_text(f))

    def save(self, project: Project) -> None:
        project.updated_at = now_iso()
        atomic_write_text(self.dir(project.id) / "project.json", project.model_dump_json(indent=1))

    def update(self, pid: str, fn: Callable[[Project], None]) -> Project:
        with self.lock(pid):
            p = self.load(pid)
            fn(p)
            self.save(p)
            return p

    def log(self, pid: str, message: str) -> None:
        def _f(p: Project) -> None:
            p.log.append(f"{now_iso()[11:19]} {message}")
            del p.log[:-300]

        self.update(pid, _f)

    def list(self) -> list[dict]:
        """Short summaries of all projects, newest first (a damaged project is skipped, not fatal)."""
        out = []
        for d in sorted(self.root.iterdir(), reverse=True):
            f = d / "project.json"
            if not f.exists():
                continue
            try:
                p = Project.model_validate_json(read_text(f))
            except Exception:  # noqa: BLE001 - a corrupt project must not break the list
                continue
            dur = p.final.duration if p.final else 0
            out.append({"id": p.id, "title": p.title, "status": p.status, "message": p.message,
                        "language": p.settings.language, "created_at": p.created_at, "updated_at": p.updated_at,
                        "segments": len(p.segments), "duration": dur})
        out.sort(key=lambda x: x["created_at"], reverse=True)
        return out

    def delete(self, pid: str) -> None:
        d = self.dir(pid)
        with self.lock(pid):
            shutil.rmtree(d, ignore_errors=True)

    # ------------------------------------------------------------------ source files
    def save_upload(self, pid: str, filename: str, data: bytes) -> Path:
        safe = re.sub(r"[^\w.\-]+", "_", Path(filename).name, flags=re.UNICODE) or "upload"
        p = self.dir(pid) / "source" / safe
        atomic_write_bytes(p, data)
        return p

    def source_upload(self, pid: str) -> Optional[Path]:
        d = self.dir(pid) / "source"
        for f in sorted(d.iterdir()) if d.exists() else []:
            if f.name not in {"source.txt", "units.json"}:
                return f
        return None

    def save_units(self, pid: str, text: str, units: list[SourceUnit]) -> None:
        d = self.dir(pid) / "source"
        atomic_write_text(d / "source.txt", text)
        write_json(d / "units.json", [u.model_dump() for u in units])

    def has_units(self, pid: str) -> bool:
        return (self.dir(pid) / "source" / "units.json").exists()

    def load_units(self, pid: str) -> list[SourceUnit]:
        return [SourceUnit(**u) for u in read_json(self.dir(pid) / "source" / "units.json")]

    def load_source_text(self, pid: str) -> str:
        return read_text(self.dir(pid) / "source" / "source.txt")

    # ------------------------------------------------------------------ files served to the UI
    def resolve_public(self, pid: str, relpath: str) -> Optional[Path]:
        """Only segment audio, final outputs and the video folder may be downloaded (never source files or project.json)."""
        base = self.dir(pid).resolve()
        target = (base / relpath).resolve()
        try:
            rel = target.relative_to(base)
        except ValueError:
            return None
        if not rel.parts or rel.parts[0] not in {"segments", "final", "video"} or not target.is_file():
            return None
        return target
