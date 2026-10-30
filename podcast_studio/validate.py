"""Quality validation of a finished (or script-only) episode.

`validate_project` returns a report of independent checks, each `pass` / `warn` / `fail`:

  structure      intro/outro present, every segment has a script (and audio unless script-only)
  faithfulness   numbers not in the chapter, key-point coverage, chapter coverage, open fact-check issues,
                 and (with deep=True) a *fresh* LLM fact-check of every current script
  naturalness    turn length, question/reaction share, host balance, repetition, over-long monologues
  audio          duration vs target, clipping, silence, speech level, TTS language consistency
  outputs        transcripts exist, subtitle cue count matches the script, chapter marks are ordered

`script_metrics` is also what `tune` uses to compare configurations.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from . import audio
from .models import Project, SourceUnit, Turn
from .store import ProjectStore
from .text import contains_latin_letters, split_sentences, unsupported_numbers, word_count

_WORD = re.compile(r"[^\W\d_]{4,}", re.UNICODE)


@dataclass
class Check:
    """One line of a quality report: its name, `pass`, `warn` or `fail`, a plain explanation and the area it belongs to."""
    name: str
    status: str  # pass | warn | fail
    detail: str
    area: str = ""
    data: dict = field(default_factory=dict)

    def public(self) -> dict:
        return {"area": self.area, "name": self.name, "status": self.status, "detail": self.detail, "data": self.data}


def _words(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(text)}


def script_metrics(turns: list[Turn], host_a: str = "A") -> dict:
    """Heuristic 'does this read like a conversation' numbers (language independent)."""
    n = max(1, len(turns))
    wc = [word_count(t.text) for t in turns]
    total = max(1, sum(wc))
    a_words = sum(w for t, w in zip(turns, wc) if t.speaker == "A")
    switches = sum(1 for x, y in zip(turns, turns[1:]) if x.speaker != y.speaker)
    longest_run = run = 0
    prev = None
    for t, w in zip(turns, wc):
        run = run + w if t.speaker == prev else w
        prev = t.speaker
        longest_run = max(longest_run, run)
    grams = Counter()
    toks = [w for t in turns for w in re.findall(r"\w+", t.text.lower())]
    for i in range(len(toks) - 3):
        grams[tuple(toks[i:i + 4])] += 1
    repeated = sum(c - 1 for c in grams.values() if c > 1)
    return {
        "turns": len(turns), "words": total, "avg_words_per_turn": round(total / n, 1), "longest_turn": max(wc, default=0),
        "longest_monologue": longest_run,
        "question_share": round(sum(1 for t in turns if "?" in t.text or "؟" in t.text) / n, 2),
        "reaction_share": round(sum(1 for t in turns if t.reaction) / n, 2),
        "styled_share": round(sum(1 for t in turns if t.style) / n, 2),
        "host_balance_a": round(a_words / total, 2),
        "speaker_switch_rate": round(switches / max(1, n - 1), 2),
        "repeated_4gram_share": round(repeated / max(1, len(toks)), 3),
    }


def _naturalness(p: Project) -> list[Check]:
    out: list[Check] = []
    turns = [t for s in p.segments if s.version() for t in s.version().turns]  # type: ignore[union-attr]
    if not turns:
        return [Check("script", "fail", "There are no scripts to analyse.", "naturalness")]
    m = script_metrics(turns)

    def add(name, ok, warn_ok, detail, **d):
        out.append(Check(name, "pass" if ok else ("warn" if warn_ok else "fail"), detail, "naturalness", d))

    add("Turn length", 8 <= m["avg_words_per_turn"] <= 38, 5 <= m["avg_words_per_turn"] <= 55,
        f"average {m['avg_words_per_turn']} words per turn (good range 8-38; real conversations alternate often)")
    add("Back-and-forth", m["speaker_switch_rate"] >= 0.7, m["speaker_switch_rate"] >= 0.5,
        f"speaker changes on {int(m['speaker_switch_rate'] * 100)}% of turns")
    add("Longest monologue", m["longest_monologue"] <= 110, m["longest_monologue"] <= 160,
        f"{m['longest_monologue']} words in a row by one host (over ~110 sounds like lecturing)")
    add("Questions", 0.12 <= m["question_share"] <= 0.6, m["question_share"] >= 0.05,
        f"{int(m['question_share'] * 100)}% of turns are questions (curious co-host)")
    add("Reactions", m["reaction_share"] >= 0.08, m["reaction_share"] >= 0.03,
        f"{int(m['reaction_share'] * 100)}% short reactions ('hmm', 'oh!', 'right')")
    add("Host balance", 0.25 <= m["host_balance_a"] <= 0.55, 0.15 <= m["host_balance_a"] <= 0.65,
        f"host A speaks {int(m['host_balance_a'] * 100)}% of the words (explainer should speak more than the questioner)")
    add("Repetition", m["repeated_4gram_share"] <= 0.04, m["repeated_4gram_share"] <= 0.08,
        f"{m['repeated_4gram_share'] * 100:.1f}% repeated 4-word phrases")
    out[0].data["all"] = m
    return out


def _faithfulness_offline(p: Project, units: list[SourceUnit]) -> list[Check]:
    out: list[Check] = []
    body_units = [u for u in units if u.kind != "heading"]
    source_text = " ".join(u.text for u in units)
    lang = p.settings.language
    translate = p.settings.writing_mode == "translate"

    bad_numbers: list[str] = []
    for s in p.segments:
        v = s.version()
        if not v:
            continue
        for i, t in enumerate(v.turns):
            txt = (t.text_en or t.text) if translate else t.text
            bad = unsupported_numbers(txt, source_text + " " + " ".join(s.key_points))
            if bad:
                bad_numbers.append(f"segment {s.index + 1} line {i + 1}: {', '.join(bad)}")
    out.append(Check("Numbers match the chapter", "pass" if not bad_numbers else "fail",
                     "every figure spoken appears in the chapter" if not bad_numbers else "; ".join(bad_numbers[:5]), "faithfulness"))

    # coverage of the chapter: each unit's content words should show up in its segment's script
    seg_text = {s.index: " ".join((t.text_en or t.text) if translate else t.text for t in s.version().turns).lower()
                for s in p.segments if s.version()}
    uncovered: list[int] = []
    unit_seg: dict[int, int] = {}
    for s in p.segments:
        for uid in s.unit_ids:
            unit_seg.setdefault(uid, s.index)
    for u in body_units:
        sid = unit_seg.get(u.id)
        if sid is None:
            uncovered.append(u.id)
            continue
        uw = _words(u.text)
        if len(uw) >= 5:
            hit = sum(1 for w in uw if w in seg_text.get(sid, ""))
            if hit / len(uw) < 0.25:
                uncovered.append(u.id)
    cov = 1 - len(uncovered) / max(1, len(body_units))
    if lang != "en" and not translate:
        out.append(Check("Chapter coverage", "warn", "Word-overlap coverage is only reliable when the podcast language equals the chapter language; use --deep for an LLM check.", "faithfulness"))
    else:
        out.append(Check("Chapter coverage", "pass" if cov >= 0.85 else ("warn" if cov >= 0.65 else "fail"),
                         f"{int(cov * 100)}% of the chapter's paragraphs are reflected in the script"
                         + (f" (thin or missing: units {', '.join(map(str, uncovered[:12]))})" if uncovered else ""), "faithfulness",
                         {"uncovered_units": uncovered}))

    missing = [(s.index + 1, k) for s in p.segments if s.version() and s.version().check for k in s.version().check.missing_points]  # type: ignore[union-attr]
    out.append(Check("Key points present", "pass" if not missing else "fail",
                     "every planned key point is covered" if not missing else f"{len(missing)} missing, e.g. segment {missing[0][0]}: {missing[0][1][:80]}", "faithfulness"))

    flagged = [s for s in p.segments if s.version() and s.version().check and s.version().check.needs_review]  # type: ignore[union-attr]
    edited = [s for s in p.segments if s.version() and s.version().origin == "edited"]
    if flagged:
        out.append(Check("Fact-check status", "fail", "needs review: " + ", ".join(f"#{s.index + 1} {s.title}" for s in flagged), "faithfulness"))
    else:
        out.append(Check("Fact-check status", "pass" if p.settings.fact_check else "warn",
                         "all segments passed the fact-checker" if p.settings.fact_check else "fact-check was switched off for this episode", "faithfulness"))
    if edited:
        out.append(Check("Hand-edited lines", "warn", f"segments {', '.join(str(s.index + 1) for s in edited)} were edited by hand and not re-checked (use --deep)", "faithfulness"))

    if lang != "en" and not translate:
        latin = 0
        for s in p.segments:
            for t in (s.version().turns if s.version() else []):  # type: ignore[union-attr]
                letters = [c for c in t.text if c.isalpha()]
                latin += sum(1 for c in letters if contains_latin_letters(c)) > 0.25 * max(1, len(letters))
        out.append(Check("Native script", "pass" if not latin else "fail",
                         "all lines are in the native script" if not latin else f"{latin} lines are mostly Latin letters (the voice will mispronounce them)", "faithfulness"))
    return out


def _faithfulness_deep(p: Project, store: ProjectStore, llm, model: Optional[str]) -> list[Check]:
    from .pipeline.checks import llm_check

    units = store.load_units(p.id)
    translate = p.settings.writing_mode == "translate"
    out: list[Check] = []
    for s in p.segments:
        v = s.version()
        if not v:
            continue
        seg_units = [u for u in units if u.id in set(s.unit_ids)]
        rep = llm_check(llm, turns=v.turns, seg=s, seg_units=seg_units, all_units=units, lang=p.settings.language,
                        translate_master=translate, model=model or p.settings.checker_model)
        majors = [i for i in rep.issues if i.severity == "major"]
        out.append(Check(f"Fresh fact-check: segment {s.index + 1}", "pass" if not majors and not rep.missing_points else "fail",
                         "no unsupported claims" if not majors and not rep.missing_points else
                         "; ".join([f"line {(i.turn or 0) + 1}: {i.detail}" for i in majors[:3]] + [f"missing: {m}" for m in rep.missing_points[:2]]),
                         "faithfulness", {"issues": [i.model_dump() for i in rep.issues]}))
    return out


def _structure(p: Project, need_audio: bool) -> list[Check]:
    out: list[Check] = []
    kinds = [s.kind for s in p.segments]
    out.append(Check("Episode shape", "pass" if kinds and kinds[0] == "intro" and kinds[-1] == "outro" else "warn",
                     f"{len(kinds)} segments: " + " > ".join(kinds), "structure"))
    no_script = [s.index + 1 for s in p.segments if not s.version()]
    out.append(Check("Scripts", "pass" if not no_script else "fail",
                     "every segment has a script" if not no_script else f"missing for segments {no_script}", "structure"))
    if need_audio:
        store_missing = [s.index + 1 for s in p.segments if not (s.version() and s.version().audio_file)]  # type: ignore[union-attr]
        out.append(Check("Segment audio", "pass" if not store_missing else "fail",
                         "every segment is recorded" if not store_missing else f"not recorded: {store_missing}", "structure"))
    return out


def _audio(p: Project, store: ProjectStore) -> list[Check]:
    out: list[Check] = []
    if not (p.final and p.final.wav):
        return [Check("Final episode", "fail", "The episode has not been mixed yet (run `voice` / Generate audio).", "audio")]
    d = store.dir(p.id)
    wav = d / p.final.wav
    if not wav.exists():
        return [Check("Final episode", "fail", "The mixed file is missing from disk.", "audio")]
    x = audio.read_wav(wav)
    dur = audio.duration(x)
    tgt = p.plan.target_minutes * 60
    if tgt:
        ratio = dur / tgt
        out.append(Check("Length vs target", "pass" if 0.75 <= ratio <= 1.25 else ("warn" if 0.55 <= ratio <= 1.6 else "fail"),
                         f"{dur / 60:.1f} min against a {tgt / 60:.1f} min target ({int(ratio * 100)}%)", "audio"))
    peak = float(np.max(np.abs(x))) if len(x) else 0.0
    out.append(Check("Clipping", "pass" if peak < 0.995 else "fail", f"peak {20 * np.log10(max(peak, 1e-6)):.1f} dBFS", "audio"))
    frames = x[: len(x) // 480 * 480].reshape(-1, 480)
    rms = np.sqrt((frames ** 2).mean(axis=1) + 1e-12)
    db = 20 * np.log10(rms)
    active = db[db > -50]
    sil = float((db < -50).mean())
    lvl = float(20 * np.log10(np.sqrt((10 ** (active / 10)).mean()))) if len(active) else -99.0
    out.append(Check("Speech level", "pass" if -24 <= lvl <= -12 else "warn", f"average speech level {lvl:.1f} dB RMS", "audio"))
    out.append(Check("Silence", "pass" if sil <= 0.25 else ("warn" if sil <= 0.4 else "fail"),
                     f"{int(sil * 100)}% of the episode is silence (pauses are fine, long dead air is not)", "audio"))
    longest = 0
    cur = 0
    for v in db < -55:
        cur = cur + 1 if v else 0
        longest = max(longest, cur)
    out.append(Check("Longest pause", "pass" if longest * 0.02 <= 2.0 else "warn", f"{longest * 0.02:.1f} s", "audio"))
    if p.final.mp3:
        mp3 = d / p.final.mp3
        out.append(Check("MP3 export", "pass" if mp3.exists() and mp3.stat().st_size > 2000 else "fail",
                         f"{mp3.stat().st_size / 1024:.0f} KB" if mp3.exists() else "missing", "audio"))
    else:
        out.append(Check("MP3 export", "warn", "no MP3 (ffmpeg not installed); WAV is available", "audio"))
    return out


def _outputs(p: Project, store: ProjectStore) -> list[Check]:
    out: list[Check] = []
    if not p.final or not p.final.srt:
        return out
    d = store.dir(p.id)
    srt = d / p.final.srt
    n_turns = sum(len(split_sentences(t.text) or [t.text]) for s in p.segments if s.version() for t in s.version().turns)  # type: ignore[union-attr]
    if srt.exists():
        cues = len(re.findall(r"-->", srt.read_text(encoding="utf-8")))
        out.append(Check("Subtitles match script", "pass" if cues == n_turns else "warn", f"{cues} subtitle cues for {n_turns} spoken sentences", "outputs"))
    else:
        out.append(Check("Subtitles match script", "fail", "transcript.srt is missing", "outputs"))
    starts = [c.start for c in p.final.chapters]
    out.append(Check("Chapter marks", "pass" if starts == sorted(starts) and len(starts) == len(p.segments) else "fail",
                     f"{len(starts)} chapters in order" if starts == sorted(starts) else "chapter times are out of order", "outputs"))
    if p.final.stale:
        out.append(Check("Episode up to date", "warn", "a segment changed after the last mix; rebuild the episode", "outputs"))
    return out


def _video(p: Project, store: ProjectStore, units: list[SourceUnit]) -> list[Check]:
    """Checks on the explainer video: file, sync with the audio, coverage of the script, faithfulness of the on-screen text."""
    out: list[Check] = []
    fv = p.video.final
    if not fv or not fv.mp4:
        return out
    from .video import mux
    from .video.director import brand_for, check_scenes, corpus_for, effective_language
    from .video.pipeline import is_stale, storyboard_state
    from .video.timeline import TimelineError, build_shots
    A = "video"
    f = store.dir(p.id) / fv.mp4
    if not f.exists():
        return [Check("Video file", "fail", "episode.mp4 is missing; make the video again", A)]
    pr = mux.probe(f)
    ok = pr["video"] and pr["audio"]
    out.append(Check("Video file", "pass" if ok else "fail",
                     f"{pr['width']}x{pr['height']} {pr['vcodec']} + {pr['acodec']}, {f.stat().st_size / 1e6:.1f} MB" if ok else "the file has no picture or no sound", A))
    if ok and p.final:
        d = abs(pr["duration"] - p.final.duration)
        out.append(Check("Video length matches audio", "pass" if d <= 0.3 else ("warn" if d <= 1.0 else "fail"),
                         f"video {pr['duration']:.1f}s, audio {p.final.duration:.1f}s", A, {"diff": round(d, 2)}))
    out.append(Check("Video up to date", "warn" if is_stale(p) else "pass",
                     "the episode changed after the video was made; update the video" if is_stale(p) else "made from the current episode audio", A))
    missing = [s.index + 1 for s in p.segments if storyboard_state(p, s.index) == "missing"]
    stale = [s.index + 1 for s in p.segments if storyboard_state(p, s.index) == "stale"]
    out.append(Check("Visuals planned for every segment", "fail" if missing else ("warn" if stale else "pass"),
                     f"missing for segment(s) {missing}" if missing else (f"older script for segment(s) {stale}" if stale else f"{len(p.video.storyboards)} segments"), A))
    # on-screen text must come from the script (the same rules the director enforces, re-run on what was actually saved)
    majors, minors, fallbacks = [], 0, 0
    for sb in p.video.storyboards:
        if sb.index >= len(p.segments) or not p.segments[sb.index].version():
            continue
        seg = p.segments[sb.index]
        fallbacks += sb.source == "fallback"
        corpus, nums = corpus_for(p, seg, units)
        figs = {x.id for x in p.video.figures}
        found = check_scenes(sb.scenes, n_turns=len(seg.version().turns), corpus=corpus, numbers_source=nums, figure_ids=figs,  # type: ignore[union-attr]
                             language=effective_language(p, seg), translate=p.settings.writing_mode == "translate", brand=brand_for(p))
        majors += [f"segment {sb.index + 1}: {m}" for _, sev, m in found if sev == "major"]
        minors += sum(1 for _, sev, _ in found if sev == "minor")
    out.append(Check("On-screen text comes from the script", "fail" if majors else ("warn" if minors else "pass"),
                     "; ".join(majors[:3]) if majors else (f"{minors} slide(s) paraphrase more than ideal" if minors else "every slide is grounded in what is said"), A))
    if fallbacks:
        out.append(Check("Visuals planned by the model", "warn", f"{fallbacks} segment(s) use the safe built-in layout (the planner was unavailable or its output failed the checks)", A))
    # coverage + pacing
    try:
        sbs = {sb.index: sb for sb in p.video.storyboards}
        shots = build_shots(p, sbs, total=p.final.duration, captions=p.video.cfg.captions)  # type: ignore[union-attr]
        covered = abs(sum(s.dur for s in shots) - p.final.duration) < 0.05  # type: ignore[union-attr]
        out.append(Check("Every moment has a picture", "pass" if covered else "fail", f"{len(shots)} caption shots cover {sum(s.dur for s in shots):.1f}s", A))
        longest, cur, run = 0.0, None, 0.0
        for s in shots:
            key = (s.seg, s.scene)
            run = run + s.dur if key == cur else s.dur
            cur = key
            longest = max(longest, run)
        out.append(Check("Slides change often enough", "pass" if longest <= 45 else "warn", f"longest single slide: {longest:.0f}s", A))
    except TimelineError as e:
        out.append(Check("Every moment has a picture", "fail", str(e), A))
    out.append(Check("Text fits on screen", "warn" if fv.warnings else "pass", "; ".join(fv.warnings[:2]) if fv.warnings else "no clipped text", A))
    return out


def validate_project(store: ProjectStore, pid: str, *, deep: bool = False, llm=None, model: Optional[str] = None) -> dict:
    """Score a saved episode from 0 to 100 and list PASS/WARN/FAIL checks for structure, faithfulness, naturalness,
    audio, outputs and video. `deep=True` also re-verifies every segment with the model. The report is saved in the
    project's `reports` folder.
    """
    p = store.load(pid)
    units = store.load_units(pid) if store.has_units(pid) else []
    has_audio = bool(p.final and p.final.wav)
    checks: list[Check] = []
    checks += _structure(p, need_audio=p.status == "ready" or has_audio)
    if units:
        checks += _faithfulness_offline(p, units)
    if deep:
        if llm is None:
            raise ValueError("deep validation needs an LLM client")
        checks += _faithfulness_deep(p, store, llm, model)
    checks += _naturalness(p)
    if has_audio:
        checks += _audio(p, store)
        checks += _outputs(p, store)
    checks += _video(p, store, units)
    counts = Counter(c.status for c in checks)
    score = round(100 * (counts["pass"] + 0.5 * counts["warn"]) / max(1, len(checks)))
    verdict = "fail" if counts["fail"] else ("warn" if counts["warn"] else "pass")
    report = {"project": pid, "title": p.plan.episode_title or p.title, "verdict": verdict, "score": score,
              "counts": dict(counts), "deep": deep, "checks": [c.public() for c in checks]}
    rdir = store.dir(pid) / "reports"
    rdir.mkdir(exist_ok=True)
    (rdir / "validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def format_report(rep: dict) -> str:
    """Format a validation report as plain text for the terminal."""
    icon = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}
    lines = [f"Validation of '{rep['title']}'  ->  {rep['verdict'].upper()}   (score {rep['score']}/100)", ""]
    area = None
    for c in rep["checks"]:
        if c["area"] != area:
            area = c["area"]
            lines.append(f"-- {area}")
        lines.append(f"  [{icon[c['status']]}] {c['name']}: {c['detail']}")
    lines.append("")
    lines.append(f"{rep['counts'].get('pass', 0)} passed, {rep['counts'].get('warn', 0)} warnings, {rep['counts'].get('fail', 0)} failed.")
    return "\n".join(lines)
