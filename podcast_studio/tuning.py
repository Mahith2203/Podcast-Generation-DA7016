"""Every knob that changes how the podcast *sounds* or *reads*, in one place.

Edit `tuning.json` in the project folder (or run `python -m podcast_studio.cli tune`), no code changes needed.
Unknown keys are ignored and out-of-range values are clamped, so a typo can never break a run.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, fields
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TUNING_FILE = Path(os.environ.get("TUNING_FILE") or ROOT / "tuning.json")


@dataclass
class Tuning:
    """The knobs that change how an episode reads and sounds: model temperatures, density, pauses, overlaps, loudness and
    video scene length.
    """
    # --- writing ------------------------------------------------------------------
    writer_temperature: float = 0.85      # 0.3 = safe/plain ... 1.1 = chatty/creative
    planner_temperature: float = 0.4
    checker_temperature: float = 0.1
    director_temperature: float = 0.5     # video: how inventive the scene planner is (visual layouts)
    scene_words: float = 55.0             # video: target words of narration per scene (about 20 s); lower = more scene changes
    words_per_minute_scale: float = 1.0   # >1 = denser episodes (more words per minute), <1 = slower/airier
    # --- pacing (seconds) ------------------------------------------------------------
    gap_same_speaker: float = 0.22
    gap_speaker_change: float = 0.34
    gap_after_question: float = 0.06      # extra pause after a question
    gap_jitter: float = 0.04              # random +/- so the rhythm is not robotic
    reaction_overlap: float = 0.16        # how far a short "hmm / oh!" may overlap the previous speaker (0 = never overlap)
    segment_gap: float = 0.55             # silence between segments
    # --- sound ---------------------------------------------------------------------
    speech_level_db: float = -18.0        # speech RMS before the final MP3 loudness pass
    final_loudness_lufs: float = -16.0    # podcast standard
    jingle_gain: float = 1.0

    def clamp(self) -> "Tuning":
        """Force every value into its safe range so a typo cannot break a run. Returns `self`."""
        lim = {"writer_temperature": (0.0, 1.5), "planner_temperature": (0.0, 1.2), "checker_temperature": (0.0, 0.6), "director_temperature": (0.0, 1.0), "scene_words": (25.0, 140.0),
               "words_per_minute_scale": (0.6, 1.5), "gap_same_speaker": (0.05, 1.0), "gap_speaker_change": (0.05, 1.5),
               "gap_after_question": (0.0, 0.8), "gap_jitter": (0.0, 0.2), "reaction_overlap": (0.0, 0.5),
               "segment_gap": (0.1, 2.5), "speech_level_db": (-30.0, -10.0), "final_loudness_lufs": (-24.0, -10.0),
               "jingle_gain": (0.0, 1.5)}
        for k, (lo, hi) in lim.items():
            setattr(self, k, float(min(hi, max(lo, float(getattr(self, k))))))
        return self


PRESETS: dict[str, dict] = {
    "balanced": {},
    "faithful": {"writer_temperature": 0.5, "words_per_minute_scale": 0.95},
    "lively": {"writer_temperature": 1.0, "gap_jitter": 0.06, "reaction_overlap": 0.22, "gap_speaker_change": 0.28},
    "calm": {"writer_temperature": 0.7, "gap_same_speaker": 0.3, "gap_speaker_change": 0.45, "segment_gap": 0.8, "words_per_minute_scale": 0.9},
}

_lock = threading.Lock()
_cache: dict = {"mtime": None, "path": None, "value": None}


def from_dict(d: dict) -> Tuning:
    names = {f.name for f in fields(Tuning)}
    t = Tuning(**{k: v for k, v in (d or {}).items() if k in names and isinstance(v, (int, float)) and not isinstance(v, bool)})
    return t.clamp()


def current() -> Tuning:
    """The active tuning (re-read automatically when tuning.json changes)."""
    with _lock:
        if _pinned:
            return _pinned[-1]
        path = TUNING_FILE
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = None
        if _cache["value"] is not None and _cache["mtime"] == mtime and _cache["path"] == str(path):
            return _cache["value"]
        data: dict = {}
        if mtime is not None:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                data = {**PRESETS.get(raw.get("preset", ""), {}), **raw} if isinstance(raw, dict) else {}
            except (OSError, ValueError):
                data = {}
        _cache.update(mtime=mtime, path=str(path), value=from_dict(data))
        return _cache["value"]


def save(values: dict, path: Path | None = None) -> Tuning:
    """Write tuning.json (only values that differ from the defaults, plus a note)."""
    t = from_dict(values)
    base = asdict(Tuning())
    out = {k: v for k, v in asdict(t).items() if v != base[k]}
    p = path or TUNING_FILE
    p.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    return t


_pinned: list[Tuning] = []


def override(**kw):
    """Context manager for experiments (used by `tune`): temporarily replace the active tuning for ALL threads."""
    import contextlib

    @contextlib.contextmanager
    def _cm():
        t = from_dict({**asdict(current()), **kw})
        with _lock:
            _pinned.append(t)
        try:
            yield t
        finally:
            with _lock:
                _pinned.remove(t)
    return _cm()
