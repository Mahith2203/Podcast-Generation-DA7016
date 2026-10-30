"""Command line interface:  python -m podcast_studio.cli <command> ...

  serve      start the web app (default http://127.0.0.1:8000)
  doctor     check keys, models, TTS, translation and ffmpeg
  models     list OpenRouter models (optionally filtered)
  voices     list the 45 Bodhan voices
  generate   make a podcast from a PDF/.txt/.md file without the UI
  list       list projects
  regen      rewrite or re-record one segment
  edit       change one line and re-record only that line
  export     copy a finished episode (mp3/wav/transcripts) to a folder

  keys       show which keys are set (masked) and what is still missing
  setup      guided first-time setup: asks for your keys, saves them to .env, tests everything
  video      narrated explainer MP4 (slides + captions) from a finished episode; --res/--theme/--no-figures/--redo-visuals
  vregen     redo the visuals of one segment (optionally with an instruction), reusing every other frame
  validate   quality report for an episode (faithfulness, naturalness, audio); --deep adds a fresh LLM fact-check
  tune       fine-tune:  tune show | set | preset | trials | voices | remix
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from .config import Settings
from .jobs import JobCancelled
from .models import ProjectSettings, VideoCfg
from .store import ProjectStore
from .voices import LANGS, VOICES, default_hosts


def _utf8() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


def _progress(frac: float, msg: str = "") -> None:
    bar = "#" * int(frac * 24)
    sys.stdout.write(f"\r[{bar:<24}] {int(frac * 100):3d}%  {msg[:70]:<70}")
    sys.stdout.flush()


def _build(settings: Settings):
    from .pipeline.orchestrator import Orchestrator

    store = ProjectStore(settings.data_dir)
    return store, Orchestrator(settings, store)


def cmd_serve(a, settings: Settings) -> int:
    """`serve`: start the web app and API on the chosen host and port."""
    import uvicorn

    from .api import create_app

    print(f"Podcast Studio running at http://{a.host}:{a.port}   (Ctrl+C to stop)")
    if settings.mock:
        print("Demo mode is ON (MOCK_APIS=1): no real API calls are made.")
    elif settings.missing_keys():
        print("Missing: " + ", ".join(settings.missing_keys()) + "  ->  run `python -m podcast_studio.cli setup`, or click 'Keys' in the app.")
    uvicorn.run(create_app(settings), host=a.host, port=a.port, log_level="warning")
    return 0


def cmd_doctor(a, settings: Settings) -> int:
    """`doctor`: test every key, model, voice, translation service and tool separately and print OK or FAIL with a hint.
    The exit code is 1 if a required check fails.
    """
    from .doctor import is_optional, run_doctor

    ok = True
    for r in run_doctor(settings):
        print(f"[{'OK ' if r['ok'] else 'FAIL'}] {r['name']}: {r['detail']}")
        if r["hint"] and not r["ok"]:
            print(f"        -> {r['hint']}")
        ok &= r["ok"] or is_optional(r)
    return 0 if ok else 1


def cmd_models(a, settings: Settings) -> int:
    """`models [word]`: list OpenRouter models (cheapest first) so a valid name can be copied into `.env`."""
    from .clients.openrouter import OpenRouterClient

    rows = OpenRouterClient(settings).list_models(a.query or "")
    rows.sort(key=lambda r: (r["prompt_per_m"] is None, r["prompt_per_m"] or 0))
    for r in rows[: a.limit]:
        print(f"{r['id']:<55} in ${r['prompt_per_m']}/M  out ${r['completion_per_m']}/M  ctx {r['context']}")
    print(f"({len(rows)} matches; showing cheapest {min(len(rows), a.limit)}. Set LLM_MODEL_WRITER / LLM_MODEL_CHECKER in .env)")
    return 0


def cmd_voices(a, settings: Settings) -> int:
    for n, g, code in VOICES:
        print(f"{n:<10} {g:<7} recorded in {LANGS[code]['name']}")
    return 0


def cmd_generate(a, settings: Settings) -> int:
    """`generate FILE`: make an episode from a PDF or text file, export it to `--out`, and with `--video` build the
    narrated video too.
    """
    store, orch = _build(settings)
    fem, mal = default_hosts(a.lang)
    st = ProjectSettings(
        language=a.lang, writing_mode=a.mode, minutes=a.minutes, show_name=a.show,
        host_a={"name": a.host_a or fem["name"], "voice": a.voice_a or fem["voice"]},
        host_b={"name": a.host_b or mal["name"], "voice": a.voice_b or mal["voice"]},
        fact_check=not a.no_check, auto_voice=not a.script_only, jingle=not a.no_jingle,
        extra_instructions=a.extra or "", polish_translation=a.polish,
    )
    path = Path(a.file)
    if not path.exists():
        print(f"File not found: {path}")
        return 2
    p = orch.create_project(title=a.title or "", settings=st, filename=path.name, data=path.read_bytes())
    print(f"Project {p.id}")
    try:
        orch.run_generate(p.id, _progress)
    except JobCancelled:
        print("\nStopped.")
        return 130
    except Exception as e:  # noqa: BLE001
        print(f"\nFailed: {e}\nRun `generate` again with the same project via: python -m podcast_studio.cli resume {p.id}")
        return 1
    print()
    rc = _finish(store, p.id, Path(a.out) if a.out else None)
    if a.video and rc == 0:
        try:
            orch.run_video(p.id, _progress, _vcfg(a))
        except Exception as e:  # noqa: BLE001
            print(f"\nThe audio is done, but the video failed: {e}\nRetry with: python -m podcast_studio.cli video {p.id}")
            return 1
        print()
        rc = _video_report(store, p.id, Path(a.out) if a.out else None)
    return rc


def cmd_resume(a, settings: Settings) -> int:
    """`resume PROJECT`: continue an interrupted or failed run; finished steps are skipped."""
    store, orch = _build(settings)
    try:
        orch.run_generate(a.project, _progress)
    except Exception as e:  # noqa: BLE001
        print(f"\nFailed: {e}")
        return 1
    print()
    return _finish(store, a.project, Path(a.out) if a.out else None)


def _finish(store: ProjectStore, pid: str, out: Path | None) -> int:
    p = store.load(pid)
    print(f"Status: {p.status}. {p.message}")
    u = p.usage
    print(f"Used: {u.llm_calls} model calls, {u.tts_calls} voice requests ({u.tts_chars} chars, {u.tts_cache_hits} reused).")
    flagged = [s for s in p.segments if (s.version() and s.version().check and s.version().check.needs_review)]  # type: ignore[union-attr]
    for s in flagged:
        print(f"  ! segment {s.index + 1} '{s.title}' still has open issues; review it in the web app.")
    if p.final and out is not None:
        _export(store, p, out)
    elif p.final:
        print(f"Audio: {store.dir(pid) / (p.final.mp3 or p.final.wav)}")
    return 0 if p.status in {"ready", "script_ready"} else 1


def _export(store: ProjectStore, p, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    d = store.dir(p.id)
    for rel in (p.final.mp3, p.final.wav, p.final.transcript_md, p.final.srt, p.final.vtt):  # type: ignore[union-attr]
        if rel and (d / rel).exists():
            shutil.copy2(d / rel, out / Path(rel).name)
    print(f"Exported to {out.resolve()}")


def cmd_list(a, settings: Settings) -> int:
    store = ProjectStore(settings.data_dir)
    for r in store.list():
        print(f"{r['id']}  {r['status']:<13} {r['language']:<4} {r['segments']:>2} segs  {r['title']}")
    return 0


def cmd_regen(a, settings: Settings) -> int:
    """`regen PROJECT SEGMENT`: rewrite one segment (optionally with `-i` instructions), or with `--revoice` record the
    same words again.
    """
    _, orch = _build(settings)
    mode = "revoice" if a.revoice else "rewrite"
    try:
        orch.run_regenerate(a.project, a.segment - 1, a.instruction, mode, _progress)
    except Exception as e:  # noqa: BLE001
        print(f"\nFailed: {e}")
        return 1
    print("\nDone.")
    return 0


def cmd_edit(a, settings: Settings) -> int:
    """`edit PROJECT SEGMENT LINE`: change the text or delivery style of one spoken line; only that line is re-recorded."""
    _, orch = _build(settings)
    try:
        orch.run_edit_turn(a.project, a.segment - 1, a.line - 1, a.text, a.style, _progress)
    except Exception as e:  # noqa: BLE001
        print(f"\nFailed: {e}")
        return 1
    print("\nDone.")
    return 0


def cmd_keys(a, settings: Settings) -> int:
    """Show which keys are set (only the last 4 characters) and where to paste the missing ones."""
    from .config import ROOT
    st = settings.key_status()
    where = {"OPENROUTER_API_KEY": "language model (writes the script) - TA e-mail",
             "BODHAN_TTS_API_KEY": "voices - Bodhan key with model indic-speak",
             "BODHAN_MT_API_KEY": "translation - Bodhan key with model indic-translate (optional)",
             "BODHAN_OCR_API_KEY": "OCR - Bodhan key with model indic-ocr (optional, reserved)"}
    print(f"Keys are read from {ROOT / '.env'}  ({'found' if (ROOT / '.env').exists() else 'NOT FOUND - copy .env.example to .env'})\n")
    for name, info in st.items():
        flag = "OK     " if info["count"] else ("MISSING" if info["required"] else "not set")
        print(f"  [{flag}] {name:<20} {', '.join(info['tails']) if info['count'] else '-':<28} {where[name]}")
    print("\nSeveral keys for one setting: separate them with commas; the next one is used automatically when a key runs out of credit.")
    miss = settings.missing_keys()
    if miss:
        print("\nStill needed: " + ", ".join(miss))
    return 1 if (miss and a.check) else 0


def cmd_setup(a, settings: Settings) -> int:
    """`setup`: the interactive wizard that asks for the keys, saves them to `.env` and tests them."""
    from .setup_wizard import run_wizard

    try:
        return run_wizard(settings)
    except (KeyboardInterrupt, EOFError):
        print("\nSetup cancelled. Nothing was changed after the last saved step.")
        return 130


def cmd_validate(a, settings: Settings) -> int:
    """`validate [PROJECT]`: print the quality report of an episode (the newest by default); `--deep` adds a fresh model
    fact-check. The exit code is 1 when a check fails.
    """
    from .clients import make_clients
    from .validate import format_report, validate_project

    store = ProjectStore(settings.data_dir)
    pid = a.project or next(iter(r["id"] for r in store.list()), None)
    if not pid:
        print(f"No projects in {settings.data_dir}. Run `generate` first, or copy your old `data` folder into this project folder (episodes are stored per folder).")
        return 2
    llm = make_clients(settings)[0] if a.deep else None
    rep = validate_project(store, pid, deep=a.deep, llm=llm, model=a.model)
    if a.json:
        import json
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(format_report(rep))
        print(f"\nSaved: {store.dir(pid) / 'reports' / 'validation.json'}")
    return {"pass": 0, "warn": 0, "fail": 1}[rep["verdict"]]


def cmd_tune(a, settings: Settings) -> int:
    """`tune ...`: show or change tuning (`show`, `preset`, `set`), run model/temperature trials, sample voices, or re-
    mix an episode with new pacing without spending voice credits.
    """
    from . import tune, tuning

    act = a.action
    if act == "show":
        print(f"Tuning file: {tuning.TUNING_FILE}  ({'exists' if tuning.TUNING_FILE.exists() else 'not created yet: defaults in use'})")
        print(tune.describe_tuning())
        print("\nPresets: " + ", ".join(tuning.PRESETS) + "   (tune preset <name>)")
        return 0
    if act == "preset":
        if a.args and a.args[0] in tuning.PRESETS:
            tuning.save(tuning.PRESETS[a.args[0]])
            print(f"Applied preset '{a.args[0]}'.\n" + tune.describe_tuning())
            return 0
        print("Choose one of: " + ", ".join(tuning.PRESETS))
        return 2
    if act == "set":
        vals = {}
        for kv in a.args:
            if "=" not in kv:
                print(f"Use key=value, e.g. writer_temperature=0.7 (got '{kv}')")
                return 2
            k, v = kv.split("=", 1)
            if k not in asdict_keys():
                print(f"Unknown setting '{k}'. Run `tune show` to see them.")
                return 2
            try:
                vals[k] = float(v)
            except ValueError:
                print(f"'{v}' is not a number.")
                return 2
        cur = {**asdict_current(), **vals}
        tuning.save(cur)
        print("Saved to tuning.json.\n" + tune.describe_tuning())
        print("\nSettings that change the sound (gaps, levels) can be applied to an existing episode without new voice credits:\n  python -m podcast_studio.cli tune remix <project>")
        return 0
    if act == "remix":
        if not a.args:
            print("Usage: tune remix <project-id>")
            return 2
        _, orch = _build(settings)
        try:
            orch.run_remix(a.args[0], _progress)
        except Exception as e:  # noqa: BLE001
            print(f"\nFailed: {e}")
            return 1
        print()
        return _finish(ProjectStore(settings.data_dir), a.args[0], None)
    if act == "voices":
        voices = [v for v in (a.voices or "").split(",") if v] or None
        page = tune.render_voice_samples(settings, Path(a.out), voices=voices, lang=a.lang, text=a.text, style=a.style)
        print(f"Done. Open this file in your browser to compare the voices:\n  {page.resolve()}")
        return 0
    if act == "trials":
        if not a.file:
            print("Usage: tune trials --file chapter.pdf|txt [--models a/b,c/d] [--temps 0.5,0.85,1.0]")
            return 2
        path = Path(a.file)
        if not path.exists():
            print(f"File not found: {path}")
            return 2
        from .ingest import ingest_file
        text = ingest_file(path, path.name).text if path.suffix.lower() == ".pdf" else path.read_text(encoding="utf-8")
        models = [m for m in (a.models or "").split(",") if m] or [None]
        temps = [float(x) for x in (a.temps or "0.5,0.85,1.0").split(",") if x]
        rows = tune.run_trials(settings, text, models=models, temperatures=temps,
                               base=ProjectSettings(language=a.lang, minutes=a.minutes), keep=a.keep, say=lambda m: print(m))
        print("\n" + tune.format_trials(rows))
        return 0
    return 2


def asdict_current() -> dict:
    from dataclasses import asdict

    from . import tuning
    return asdict(tuning.current())


def asdict_keys() -> set:
    return set(asdict_current())


def _vcfg(a, base: VideoCfg | None = None) -> VideoCfg:
    c = (base or VideoCfg()).model_copy()
    if getattr(a, "res", None):
        c.resolution = a.res
    if getattr(a, "fps", None):
        c.fps = a.fps
    if getattr(a, "theme", None):
        c.theme = a.theme
    if getattr(a, "no_captions", False):
        c.captions = False
    if getattr(a, "no_speakers", False):
        c.speakers = False
    if getattr(a, "no_figures", False):
        c.use_figures = False
    return VideoCfg.model_validate(c.model_dump())


def _video_report(store: ProjectStore, pid: str, out: Path | None) -> int:
    p = store.load(pid)
    v = p.video.final
    if p.video.status == "error" or not v or not v.mp4:
        print(f"Video failed: {p.video.message}")
        return 1
    mp4 = store.dir(pid) / v.mp4
    print(f"Video: {mp4}  ({v.width}x{v.height}, {v.duration:.0f}s, {mp4.stat().st_size / 1e6:.1f} MB)")
    for w in v.warnings:
        print(f"  ! {w}")
    for sb in p.video.storyboards:
        for iss in sb.issues:
            print(f"  ! segment {sb.index + 1}: {iss}")
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)
        shutil.copy2(mp4, out / "episode.mp4")
        print(f"Copied to {out.resolve()}")
    return 0


def _newest(store: ProjectStore) -> str | None:
    return next(iter(r["id"] for r in store.list()), None)


def cmd_video(a, settings: Settings) -> int:
    """`video [PROJECT]`: build the narrated MP4 of an episode (the newest by default)."""
    store, orch = _build(settings)
    pid = a.project or _newest(store)
    if not pid:
        print(f"No projects in {settings.data_dir}. Run `generate` first, or copy your old `data` folder into this project folder (episodes are stored per folder).")
        return 2
    cfg = _vcfg(a, store.load(pid).video.cfg)
    try:
        orch.run_video(pid, _progress, cfg, redo_visuals=a.redo_visuals)
    except JobCancelled:
        print("\nStopped.")
        return 130
    except Exception as e:  # noqa: BLE001
        print(f"\nFailed: {e}")
        return 1
    print()
    return _video_report(store, pid, Path(a.out) if a.out else None)


def cmd_vregen(a, settings: Settings) -> int:
    """`vregen PROJECT SEGMENT`: redraw one segment's slides, optionally following an instruction."""
    store, orch = _build(settings)
    pid = a.project
    try:
        orch.run_video(pid, _progress, None, segments={a.segment - 1}, instruction=a.instruction, redo_visuals=True)
    except Exception as e:  # noqa: BLE001
        print(f"\nFailed: {e}")
        return 1
    print()
    return _video_report(store, pid, Path(a.out) if a.out else None)


def cmd_export(a, settings: Settings) -> int:
    """`export PROJECT --out DIR`: copy an episode's audio, transcripts and subtitles into a folder."""
    store = ProjectStore(settings.data_dir)
    p = store.load(a.project)
    if not p.final:
        print("This project has no finished episode yet.")
        return 1
    _export(store, p, Path(a.out))
    return 0


def _video_args(s) -> None:
    s.add_argument("--res", choices=["480p", "720p", "1080p"], help="video size (default 720p)")
    s.add_argument("--fps", type=int, help="frames per second, 10-30 (default 25)")
    s.add_argument("--theme", choices=["classic", "dark", "warm"])
    s.add_argument("--no-captions", action="store_true"); s.add_argument("--no-speakers", action="store_true")
    s.add_argument("--no-figures", action="store_true", help="do not use the chapter's own pictures")


def main(argv: list[str] | None = None) -> int:
    """Parse the command line and run the chosen command. Returns the process exit code."""
    _utf8()
    ap = argparse.ArgumentParser(prog="podcast_studio", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mock", action="store_true", help="offline demo mode (no API calls, synthetic beep voices)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve"); s.add_argument("--host", default="127.0.0.1"); s.add_argument("--port", type=int, default=8000)
    sub.add_parser("doctor")
    s = sub.add_parser("models"); s.add_argument("query", nargs="?"); s.add_argument("--limit", type=int, default=25)
    sub.add_parser("voices")
    s = sub.add_parser("generate"); s.add_argument("file")
    s.add_argument("--lang", default="en", choices=sorted(LANGS)); s.add_argument("--mode", default="direct", choices=["direct", "translate"])
    s.add_argument("--minutes", type=float); s.add_argument("--title"); s.add_argument("--show", default="Chapter Chat")
    s.add_argument("--host-a"); s.add_argument("--voice-a"); s.add_argument("--host-b"); s.add_argument("--voice-b")
    s.add_argument("--no-check", action="store_true"); s.add_argument("--script-only", action="store_true")
    s.add_argument("--no-jingle", action="store_true"); s.add_argument("--polish", action="store_true")
    s.add_argument("--extra"); s.add_argument("--out", help="folder to copy the finished files to")
    s.add_argument("--video", action="store_true", help="also make the explainer video")
    _video_args(s)
    s = sub.add_parser("resume"); s.add_argument("project"); s.add_argument("--out")
    sub.add_parser("list")
    s = sub.add_parser("regen"); s.add_argument("project"); s.add_argument("segment", type=int, help="1-based segment number")
    s.add_argument("--instruction", "-i"); s.add_argument("--revoice", action="store_true", help="same words, new recording")
    s = sub.add_parser("edit"); s.add_argument("project"); s.add_argument("segment", type=int); s.add_argument("line", type=int)
    s.add_argument("--text"); s.add_argument("--style")
    s = sub.add_parser("export"); s.add_argument("project"); s.add_argument("--out", required=True)
    s = sub.add_parser("video"); s.add_argument("project", nargs="?", help="project id (default: the newest)"); _video_args(s)
    s.add_argument("--redo-visuals", action="store_true", help="plan every segment's visuals again"); s.add_argument("--out", help="folder to copy episode.mp4 to")
    s = sub.add_parser("vregen"); s.add_argument("project"); s.add_argument("segment", type=int, help="1-based segment number")
    s.add_argument("--instruction", "-i", help='e.g. "show it as a cycle" or "use a comparison table"'); s.add_argument("--out")
    sub.add_parser("setup")
    s = sub.add_parser("keys"); s.add_argument("--check", action="store_true", help="exit with an error if a required key is missing")
    s = sub.add_parser("validate"); s.add_argument("project", nargs="?", help="project id (default: the newest)")
    s.add_argument("--deep", action="store_true", help="also run a fresh LLM fact-check of every segment (uses a few model calls)")
    s.add_argument("--model"); s.add_argument("--json", action="store_true")
    s = sub.add_parser("tune"); s.add_argument("action", choices=["show", "set", "preset", "trials", "voices", "remix"])
    s.add_argument("args", nargs="*", help="set: key=value ...   preset: name   remix: project id")
    s.add_argument("--file"); s.add_argument("--models", help="comma-separated OpenRouter model ids to compare")
    s.add_argument("--temps", help="comma-separated writer temperatures, e.g. 0.5,0.85,1.0")
    s.add_argument("--lang", default="en", choices=sorted(LANGS)); s.add_argument("--minutes", type=float, default=3)
    s.add_argument("--keep", action="store_true", help="keep the trial projects instead of deleting them")
    s.add_argument("--voices", help="comma-separated voice names for `tune voices`"); s.add_argument("--out", default="voice-samples")
    s.add_argument("--text"); s.add_argument("--style")

    a = ap.parse_args(argv)
    overrides = {"mock": True} if a.mock else {}
    settings = Settings.from_env(**overrides)
    return globals()[f"cmd_{a.cmd}"](a, settings)


if __name__ == "__main__":
    sys.exit(main())
