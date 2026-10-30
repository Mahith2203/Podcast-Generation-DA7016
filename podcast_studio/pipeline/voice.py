"""Turn a script version into audio: chunk -> TTS (cached, parallel) -> trim/level -> assemble with natural pacing."""
from __future__ import annotations

import logging
import random
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from .. import audio, tuning
from ..config import Settings
from ..models import Project, SegmentVersion, Turn
from ..store import ProjectStore
from ..text import chunk_for_tts, clean_spoken, word_count
from ..util import sha1

log = logging.getLogger("podcast.voice")

TURN_VERSION = "v3"  # bump if chunking/text-prep changes so stale cache keys are not reused


@dataclass
class TurnClip:
    """The recorded audio of one spoken line, and the text chunks it was recorded in."""
    audio: np.ndarray
    chunk_texts: list[str]


def _gap_after(chunk: str) -> float:
    t = chunk.rstrip("\"”’') ")
    if t.endswith("?"):
        return 0.20
    if t.endswith(("।", ".", "!", "॥", "۔", "؟")):
        return 0.17
    return 0.09  # clause break inside a long sentence


class Voicer:
    """Turns scripts into audio: splits each line into short chunks, records them with Bodhan (reusing clips already in
    the cache) and joins them with natural pauses and overlaps.
    """
    def __init__(self, bodhan, store: ProjectStore, app: Settings, meter):
        self.bodhan, self.store, self.app, self.meter = bodhan, store, app, meter

    # ------------------------------------------------------------------ chunk level (cached)
    def _key(self, text: str, voice: str, lang: str, style: Optional[str], take: int) -> str:
        return sha1(self.app.tts_model, voice, lang, style or "", text, take, TURN_VERSION)

    def synth_chunk(self, text: str, voice: str, lang: str, style: Optional[str], take: int = 0) -> np.ndarray:
        """Audio for one text chunk: taken from the cache if this text, voice, language, style and take were recorded
        before, otherwise a new Bodhan request that is then cached.
        """
        key = self._key(text, voice, lang, style, take)
        path = self.store.cache_path(key)
        if path.exists():
            try:
                arr = audio.wav_bytes_to_array(path.read_bytes())
                self.meter.tts_cache_hit()
                return arr
            except audio.AudioError:
                path.unlink(missing_ok=True)
        last: Optional[np.ndarray] = None
        for attempt in range(3):
            data = self.bodhan.tts(text, voice, lang, style)
            arr = audio.wav_bytes_to_array(data)
            last = arr
            dur = audio.duration(arr)
            # a long sentence that comes back as a fraction of a second is a failed synthesis: ask again
            if word_count(text) >= 4 and dur < 0.12 * word_count(text) and attempt < 2:
                log.warning("suspiciously short TTS audio (%.2fs for %d words); retrying", dur, word_count(text))
                continue
            break
        assert last is not None
        from ..util import atomic_write_bytes

        atomic_write_bytes(path, audio.array_to_wav_bytes(last))
        return last

    # ------------------------------------------------------------------ turn level
    def prepare_chunks(self, turn: Turn, lang: str, max_words: int) -> list[str]:
        text = clean_spoken(turn.text, lang)
        return chunk_for_tts(text, max_words=max_words) or [text]

    def render_turns(self, project: Project, version: SegmentVersion, progress: Optional[Callable[[float], None]] = None) -> list[TurnClip]:
        """Record every line of a segment version (chunks in parallel) and return one `TurnClip` per line."""
        st = project.settings
        lang = st.language
        plans: list[tuple[Turn, str, list[str]]] = []
        for t in version.turns:
            plans.append((t, project.host(t.speaker).voice, self.prepare_chunks(t, lang, st.tts_max_words)))
        jobs = [(ti, ci, c) for ti, (t, v, chunks) in enumerate(plans) for ci, c in enumerate(chunks)]
        results: dict[tuple[int, int], np.ndarray] = {}
        done = 0

        def run(job):
            ti, ci, c = job
            t, voice, _ = plans[ti]
            return (ti, ci), self.synth_chunk(c, voice, lang, t.style, t.take)

        with ThreadPoolExecutor(max_workers=self.app.tts_concurrency) as ex:
            for key, arr in ex.map(run, jobs):
                results[key] = audio.process_clip(arr)
                done += 1
                if progress:
                    progress(done / max(1, len(jobs)))
        clips: list[TurnClip] = []
        for ti, (t, v, chunks) in enumerate(plans):
            parts: list[np.ndarray] = []
            for ci, c in enumerate(chunks):
                parts.append(results[(ti, ci)])
                if ci < len(chunks) - 1:
                    parts.append(audio.silence(_gap_after(c) * 1000))
            clips.append(TurnClip(audio.concat(parts), chunks))
        return clips

    # ------------------------------------------------------------------ segment level
    def assemble_segment(self, version: SegmentVersion, clips: list[TurnClip], seed: int = 0) -> tuple[np.ndarray, list[list[float]]]:
        """Join a segment's line clips into one audio array, with pauses (shorter after reactions and questions, with
        slight variation) and a small overlap for quick reactions. Returns the audio and each line's `[start, end]` in
        seconds.
        """
        sr = audio.SR
        out = np.zeros(int(sr * 0.2), dtype=np.float32)
        times: list[list[float]] = []
        cursor = int(sr * 0.2)
        prev: Optional[Turn] = None
        prev_len = 0
        for i, (turn, clip) in enumerate(zip(version.turns, clips)):
            rng = random.Random(seed * 1000 + i)
            tu = tuning.current()
            jitter = rng.uniform(-tu.gap_jitter, tu.gap_jitter)
            clip_len = len(clip.audio)
            if prev is None:
                start = cursor
            else:
                same = prev.speaker == turn.speaker
                interrupt = (tu.reaction_overlap > 0 and turn.reaction and not same and clip_len <= int(sr * 2.4) and prev_len >= int(sr * 1.2)
                             and prev.text.rstrip().endswith((".", "।", "!", "?")))
                if interrupt:
                    overlap = int(sr * tu.reaction_overlap)
                    start = max(cursor - overlap, 0)
                    # tuck the tail of the previous speaker under the interjection
                    seg = out[start:cursor]
                    if len(seg):
                        out[start:cursor] = seg * np.linspace(1.0, 0.55, len(seg), dtype=np.float32)
                else:
                    gap = tu.gap_same_speaker if same else tu.gap_speaker_change
                    if prev.text.rstrip().endswith("?"):
                        gap += tu.gap_after_question
                    if prev.reaction:
                        gap = max(0.14, gap - 0.14)
                    start = cursor + int(sr * max(0.08, gap + jitter))
            out = audio.overlay(out, clip.audio, start, gain=0.92 if (prev and turn.reaction) else 1.0)
            end = start + clip_len
            times.append([round(start / sr, 3), round(end / sr, 3)])
            cursor = max(cursor, end)
            prev, prev_len = turn, clip_len
        out = np.concatenate([out[:cursor], audio.silence(280)])
        return audio.soft_limit(out), times

    def voice_segment(self, project: Project, seg_index: int, version: SegmentVersion,
                      progress: Optional[Callable[[float], None]] = None) -> None:
        """Render + save the audio file for one version and fill in its duration and turn times."""
        clips = self.render_turns(project, version, progress)
        arr, times = self.assemble_segment(version, clips, seed=seg_index * 31 + version.version)
        rel = Path("segments") / f"seg{seg_index + 1:02d}_v{version.version}.wav"
        audio.write_wav(self.store.dir(project.id) / rel, arr)
        version.audio_file = rel.as_posix()
        version.duration = round(audio.duration(arr), 2)
        version.turn_times = times
