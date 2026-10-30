"""Turns a finished episode + its storyboards into a gap-free list of shots: what is on screen from t0 to t1.
Every instant of the audio, including the jingles and the pauses between segments, is covered exactly once."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from ..models import Project, Scene, SegmentStoryboard
from ..text import split_sentences


class TimelineError(RuntimeError):
    """Raised when the scenes do not cover the audio correctly."""
    pass


@dataclass(frozen=True)
class Shot:
    """One still picture held on screen from `start` to `end` seconds: which segment and scene, how many items are
    revealed, and who is speaking.
    """
    start: float
    end: float
    seg: int          # position in project.segments
    scene: int        # index in that segment's storyboard
    reveal: int       # how many items/rows are visible
    speaker: str      # "A" / "B" / "" (no caption)
    name: str         # host name shown on the caption chip
    text: str         # caption text ("" = none)

    @property
    def dur(self) -> float:
        return self.end - self.start

    def state(self) -> tuple:
        return (self.seg, self.scene, self.reveal, self.speaker, self.name, self.text)


def caption_chunks(text: str, max_chars: int) -> list[str]:
    """Split a long sentence into 2-3 balanced caption lines at clause or word boundaries."""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return [text] if text else []
    k = math.ceil(len(text) / max_chars)
    chunks, rest = [], text
    for i in range(k, 1, -1):
        target = len(rest) // i
        lo, hi = max(1, int(target * 0.6)), min(len(rest) - 1, int(target * 1.4))
        best, best_score = None, 1e9
        for m in re.finditer(r"\s", rest[lo:hi + 1]):
            pos = lo + m.start()
            prev = rest[pos - 1] if pos else ""
            score = abs(pos - target) - (target * 0.35 if prev in ",;:–—-।،" else 0)
            if score < best_score:
                best, best_score = pos, score
        if best is None:
            best = target
        chunks.append(rest[:best].strip())
        rest = rest[best:].strip()
    chunks.append(rest)
    return [c for c in chunks if c]


def scene_at(sb: SegmentStoryboard, turn: int) -> int:
    for i, sc in enumerate(sb.scenes):
        if sc.from_turn <= turn <= sc.to_turn:
            return i
    return len(sb.scenes) - 1 if turn > 0 else 0


def reveal_at(sc: Scene, turn: int) -> int:
    if sc.layout in ("title", "definition", "keyfact", "figure"):
        return 0
    n = len(sc.rows) if sc.layout == "compare" else len(sc.items)
    rv = sc.reveal if len(sc.reveal) == n else [sc.from_turn] * n
    return sum(1 for r in rv if r <= turn)


def build_shots(project: Project, storyboards: dict[int, SegmentStoryboard], *, total: float, caption_chars: int = 88,
                captions: bool = True) -> list[Shot]:
    """`total` is the exact length of the finished audio; the shots end exactly there."""
    if not project.final or not project.final.chapters:
        raise TimelineError("The episode audio is not built yet.")
    shots: list[Shot] = []
    chapters = project.final.chapters
    for n, (seg, mark) in enumerate(zip(project.segments, chapters)):
        sb = storyboards.get(seg.index)
        v = seg.version()
        if sb is None or not sb.scenes:
            raise TimelineError(f"Segment {seg.index + 1} has no storyboard.")
        if not v or not v.turn_times:
            raise TimelineError(f"Segment {seg.index + 1} has no timed audio.")
        nxt = chapters[n + 1].start if n + 1 < len(chapters) else total
        lead = v.turn_times[0][0] if v.turn_times else 0.0
        if lead > 1e-3:  # the recording's leading silence already belongs to this segment's first slide
            shots.append(Shot(round(mark.start, 3), round(mark.start + lead, 3), n, scene_at(sb, 0), 0, "", "", ""))
        for ti, (turn, (a, b)) in enumerate(zip(v.turns, v.turn_times)):
            si = scene_at(sb, ti)
            sc = sb.scenes[si]
            rv = reveal_at(sc, ti)
            sents = split_sentences(turn.text) or [turn.text]
            pieces: list[str] = []
            for sent in sents:
                pieces += caption_chunks(sent, caption_chars if _latin(sent) else int(caption_chars * 0.8))
            if not pieces:
                pieces = [""]
            weights = [max(1, len(p)) for p in pieces]
            tot = sum(weights)
            t = mark.start + a
            span = max(0.0, b - a)
            name = project.host(turn.speaker).name
            for p, w in zip(pieces, weights):
                d = span * w / tot
                shots.append(Shot(round(t, 3), round(t + d, 3), n, si, rv, turn.speaker if captions and p else "",
                                  name if captions and p else "", p if captions else ""))
                t += d
        # make sure this segment's last scene stays up until the next segment starts (the pause is shown without a caption)
        last = shots[-1] if shots else None
        if last is not None and last.seg == n and last.end < nxt - 1e-3:
            shots.append(Shot(last.end, round(nxt, 3), n, last.scene, last.reveal, "", "", ""))
    if not shots:
        raise TimelineError("Nothing to show.")
    first = shots[0]
    if first.start > 1e-3:  # intro jingle: the opening scene, nothing revealed yet
        shots.insert(0, Shot(0.0, first.start, first.seg, first.scene, 0, "", "", ""))
    return _tidy(shots, total)


def _latin(s: str) -> bool:
    letters = [c for c in s if c.isalpha()]
    return not letters or sum(c.isascii() for c in letters) / len(letters) > 0.6


def _tidy(shots: list[Shot], total: float) -> list[Shot]:
    """Gap-free, in order, ends exactly at `total`, no zero-length shots."""
    out: list[Shot] = []
    t = 0.0
    for s in sorted(shots, key=lambda x: x.start):
        a, b = max(s.start, t), min(s.end, total)
        if b - a < 1e-3:
            continue
        if a > t + 1e-3 and out:   # hole: keep the previous picture on screen (no caption)
            p = out[-1]
            out.append(Shot(t, a, p.seg, p.scene, p.reveal, "", "", ""))
        elif a > t + 1e-3:
            out.append(Shot(t, a, s.seg, s.scene, 0, "", "", ""))
        out.append(Shot(a, b, s.seg, s.scene, s.reveal, s.speaker, s.name, s.text))
        t = b
    if not out:
        raise TimelineError("Nothing to show.")
    if t < total - 1e-3:  # tail (outro jingle): hold the final picture, no caption
        p = out[-1]
        out.append(Shot(t, total, p.seg, p.scene, p.reveal, "", "", ""))
    return out
