"""Build the final episode (jingle + segments + jingle), chapter marks and transcripts."""
from __future__ import annotations

from pathlib import Path

import numpy as np

from .. import audio, tuning
from ..models import ChapterMark, FinalAudio, Project
from ..store import ProjectStore
from ..text import split_sentences
from ..util import atomic_write_text, fmt_time, now_iso

SEGMENT_GAP_S = 0.55


class AssembleError(RuntimeError):
    """Raised when the final episode cannot be assembled."""
    pass


def _ts(sec: float, comma: bool) -> str:
    sec = max(0.0, sec)
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{',' if comma else '.'}{ms:03d}"


def _cues(project: Project) -> list[tuple[float, float, str, str]]:
    """(start, end, speaker name, sentence) for every sentence, timed proportionally inside each turn."""
    cues = []
    offset = project.final.chapters if project.final else []
    for seg, mark in zip(project.segments, offset):
        v = seg.version()
        if not v or not v.turn_times:
            continue
        for turn, (a, b) in zip(v.turns, v.turn_times):
            sents = split_sentences(turn.text) or [turn.text]
            weights = [max(1, len(s)) for s in sents]
            total = sum(weights)
            t = mark.start + a
            span = b - a
            for s, w in zip(sents, weights):
                d = span * w / total
                cues.append((t, t + d, project.host(turn.speaker).name, s))
                t += d
    return cues


def write_transcripts(project: Project, final_dir: Path) -> tuple[str, str, str]:
    """Write the Markdown transcript (with a heading and start time per segment), the SRT and the WebVTT subtitles into
    `final_dir`. Returns their paths relative to the project as `(markdown, srt, vtt)`.
    """
    cues = _cues(project)
    srt = "\n".join(f"{i}\n{_ts(a, True)} --> {_ts(b, True)}\n{who}: {txt}\n" for i, (a, b, who, txt) in enumerate(cues, 1))
    vtt = "WEBVTT\n\n" + "\n".join(f"{_ts(a, False)} --> {_ts(b, False)}\n<v {who}>{txt}\n" for a, b, who, txt in cues)
    lines = [f"# {project.plan.episode_title or project.title}", f"*{project.settings.show_name}*", ""]
    for seg, mark in zip(project.segments, project.final.chapters if project.final else []):
        v = seg.version()
        if not v:
            continue
        lines.append(f"## {seg.title}  \n`{fmt_time(mark.start)}`")
        lines.append("")
        for turn, tt in zip(v.turns, v.turn_times or [[0, 0]] * len(v.turns)):
            lines.append(f"**{project.host(turn.speaker).name}:** {turn.text}  ")
        lines.append("")
    md = "\n".join(lines)
    atomic_write_text(final_dir / "transcript.srt", srt)
    atomic_write_text(final_dir / "transcript.vtt", vtt)
    atomic_write_text(final_dir / "transcript.md", md)
    return "final/transcript.md", "final/transcript.srt", "final/transcript.vtt"


def build_episode(project: Project, store: ProjectStore) -> FinalAudio:
    """Lay every segment on one timeline: intro chime fades under the first words, 0.55 s between segments,
    outro chime under the last tail. Chapter marks are the exact start of each segment."""
    pdir = store.dir(project.id)
    final_dir = pdir / "final"
    final_dir.mkdir(exist_ok=True)
    sr = audio.SR
    timeline = np.zeros(0, dtype=np.float32)
    pos = 0
    if project.settings.jingle:
        st = audio.make_stinger("in") * tuning.current().jingle_gain
        timeline = audio.overlay(timeline, st, 0)
        pos = max(0, len(st) - int(1.1 * sr))
    chapters: list[ChapterMark] = []
    for n, seg in enumerate(project.segments):
        v = seg.version()
        if not v or not v.audio_file or not (pdir / v.audio_file).exists():
            raise AssembleError(f"Segment {seg.index + 1} ('{seg.title}') has no audio yet.")
        arr = audio.read_wav(pdir / v.audio_file)
        if n > 0:
            pos += int(tuning.current().segment_gap * sr)
        chapters.append(ChapterMark(index=seg.index, title=seg.title, start=round(pos / sr, 2)))
        timeline = audio.overlay(timeline, arr, pos)
        pos += len(arr)
    if project.settings.jingle:
        timeline = audio.overlay(timeline, audio.make_stinger("out") * tuning.current().jingle_gain, max(0, pos - int(0.25 * sr)))
    full = audio.soft_limit(timeline)

    wav_rel, mp3_rel = "final/podcast.wav", "final/podcast.mp3"
    audio.write_wav(pdir / wav_rel, full)
    mp3_ok = audio.encode_mp3(pdir / wav_rel, pdir / mp3_rel)
    if not mp3_ok:
        (pdir / mp3_rel).unlink(missing_ok=True)
    fin = FinalAudio(wav=wav_rel, mp3=mp3_rel if mp3_ok else None, duration=round(audio.duration(full), 2),
                     chapters=chapters, built_at=now_iso(), stale=False)
    project.final = fin
    md, srt, vtt = write_transcripts(project, final_dir)
    fin.transcript_md, fin.srt, fin.vtt = md, srt, vtt
    return fin
