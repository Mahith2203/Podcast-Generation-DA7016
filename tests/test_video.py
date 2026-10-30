"""Video extension: storyboards (checks, repair, fallback), timeline, figures, real browser rendering, MP4 output, redo, CLI, API."""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from podcast_studio import audio
from podcast_studio.api import create_app
from podcast_studio.clients import make_clients
from podcast_studio.config import Settings
from podcast_studio.models import Project, ProjectSettings, Scene, SourceUnit, VideoCfg
from podcast_studio.pipeline.orchestrator import Orchestrator
from podcast_studio.store import ProjectStore
from podcast_studio.validate import validate_project
from podcast_studio.video import mux, render, templates
from podcast_studio.video.director import (
    Director,
    brand_for,
    check_scenes,
    corpus_for,
    fallback_storyboard,
    grounding,
    normalize_scenes,
)
from podcast_studio.video.figures import extract_figures
from podcast_studio.video.pipeline import VideoError, is_stale, storyboard_state
from podcast_studio.video.timeline import build_shots, caption_chunks

from .conftest import noprog
from .pdfmake import make_chapter_pdf

ROOT = Path(__file__).resolve().parent.parent
TEXT = (ROOT / "samples" / "water_cycle.txt").read_text(encoding="utf-8")

_BROWSER: list = []


def browser_ok() -> bool:
    if not _BROWSER:
        _BROWSER.append(render.browser_status()[0])
    return _BROWSER[0]


need_browser = pytest.mark.skipif(not browser_ok(), reason="no Chromium/Edge for Playwright (python -m playwright install chromium)")
need_ffmpeg = pytest.mark.skipif(not (audio.ffmpeg_path() and mux.has_x264()), reason="ffmpeg with libx264 not installed")
VIDEO = pytest.mark.usefixtures()  # marker placeholder so intent is visible in -k selections


def make_episode(data_dir: Path, *, lang: str = "en", pdf: bool = False, minutes: float = 4):
    s = Settings.from_env(mock=True, data_dir=data_dir)
    store = ProjectStore(data_dir)
    orch = Orchestrator(s, store)
    st = ProjectSettings(minutes=minutes, language=lang)
    if pdf:
        data_dir.mkdir(parents=True, exist_ok=True)
        f = make_chapter_pdf(TEXT, data_dir / "chapter.pdf")
        p = orch.create_project(title="", settings=st, filename="chapter.pdf", data=f.read_bytes())
    else:
        p = orch.create_project(title="", settings=st, text=TEXT)
    orch.run_generate(p.id, noprog)
    return s, store, orch, p.id


# ------------------------------------------------------------------------------------------------ fixtures
@pytest.fixture(scope="module")
def ep(tmp_path_factory):
    """A finished mock episode with no video yet."""
    d = tmp_path_factory.mktemp("ep") / "data"
    return make_episode(d)


@pytest.fixture(scope="module")
def vid(tmp_path_factory):
    """A finished mock episode (from a PDF with a figure) with a 480p video."""
    d = tmp_path_factory.mktemp("vid") / "data"
    s, store, orch, pid = make_episode(d, pdf=True)
    orch.run_video(pid, noprog, VideoCfg(resolution="480p"))
    return s, store, orch, pid


def clone(src_store: ProjectStore, tmp_path: Path):
    """An independent copy of a data folder (frames cache included, so a redo is fast)."""
    dst = tmp_path / "copy"
    shutil.copytree(src_store.data_dir, dst)
    s = Settings.from_env(mock=True, data_dir=dst)
    store = ProjectStore(dst)
    return s, store, Orchestrator(s, store)


# ------------------------------------------------------------------------------------------------ pure logic
def test_caption_chunks_split_long_sentences_evenly():
    short = "Water evaporates."
    assert caption_chunks(short, 88) == [short]
    long = ("When the Sun heats the oceans, lakes and rivers, some of the liquid water changes into an invisible gas, "
            "and that gas rises into the cooler air high above the ground.")
    parts = caption_chunks(long, 88)
    assert 2 <= len(parts) <= 3 and all(len(x) <= 110 for x in parts)
    assert " ".join(parts) == long  # nothing lost, nothing added
    assert caption_chunks("", 50) == []


def test_normalize_scenes_makes_a_gap_free_cover_and_fixes_reveals():
    sc = [Scene(layout="bullets", title="A", items=["x"] * 9, from_turn=2, to_turn=5, reveal=[9, 9]),
          Scene(layout="steps", title="B", items=["one", "two"], from_turn=4, to_turn=20, reveal=[0, 1])]
    out = normalize_scenes(sc, 12)
    assert out[0].from_turn == 0 and out[-1].to_turn == 11
    for a, b in zip(out, out[1:]):
        assert b.from_turn == a.to_turn + 1
    assert len(out[0].items) <= 5
    for s in out:
        assert len(s.reveal) == len(s.items) and all(s.from_turn <= r <= s.to_turn for r in s.reveal)


SCRIPT = "Evaporation happens when the Sun heats water. About 71 percent of the Earth is covered with water."
CORPUS = {t[:5] for t in SCRIPT.lower().replace(".", "").split() if len(t) > 3}


def _chk(scene, **kw):
    args = dict(n_turns=4, corpus=CORPUS, numbers_source=SCRIPT, figure_ids={"fig01"}, language="en", translate=False)
    args.update(kw)
    return check_scenes([scene], **args)


def test_check_scenes_catches_invented_numbers_and_unknown_figures():
    ok = Scene(layout="bullets", title="Evaporation", items=["Sun heats water", "71 percent covered"], to_turn=3, reveal=[0, 1])
    assert not [m for m in _chk(ok) if m[1] == "major"]
    bad = Scene(layout="bullets", title="Evaporation", items=["Sun heats water", "93 percent covered"], to_turn=3, reveal=[0, 1])
    assert any("93" in m[2] for m in _chk(bad) if m[1] == "major")
    fig = Scene(layout="figure", title="Evaporation", figure="fig99", to_turn=3)
    assert any("fig99" in m[2] for m in _chk(fig))
    inv = Scene(layout="bullets", title="Volcanoes", items=["Magma erupts violently", "Lava flows downhill"], to_turn=3, reveal=[0, 1])
    assert any("come from the script" in m[2] for m in _chk(inv) if m[1] == "major")
    assert grounding(ok, CORPUS) > 0.7 > grounding(inv, CORPUS)


def test_check_scenes_requires_native_script_for_indian_languages():
    latin = Scene(layout="bullets", title="Jal chakra", items=["Surya pani ko garam karta hai", "Bhaap upar uthti hai"], to_turn=3, reveal=[0, 1])
    assert any("script" in m[2] for m in _chk(latin, language="hi", corpus=set(), numbers_source="") if m[1] == "major")


def test_templates_escape_html_and_mark_rtl():
    sc = Scene(layout="bullets", title="<script>alert(1)</script>", items=["a & b <i>x</i>"], reveal=[0])
    html = templates.page_html(sc, templates.Ctx(width=1280, height=720, lang="ur", rtl=True))
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html and "a &amp; b &lt;i&gt;" in html
    assert 'dir="rtl"' in html and 'lang="ur"' in html
    assert render.is_rtl("ur") and render.is_rtl("sd") and not render.is_rtl("hi") and not render.is_rtl("en")


def test_models_load_old_projects_without_video():
    old = Project(id="x", title="t", settings=ProjectSettings()).model_dump()
    old.pop("video")
    q = Project.model_validate(old)
    assert q.video.status == "none" and q.video.cfg.resolution == "720p" and q.video.storyboards == []


# ------------------------------------------------------------------------------------------------ director
def test_director_makes_checked_storyboards_for_every_segment(ep):
    s, store, orch, pid = ep
    p, units = store.load(pid), store.load_units(pid)
    d = Director(make_clients(s)[0], say=lambda m: None)
    for seg in p.segments:
        sb = d.storyboard(p, seg, units, [])
        n = len(seg.version().turns)
        assert sb.scenes and sb.scenes[0].from_turn == 0 and sb.scenes[-1].to_turn == n - 1
        assert sb.version == seg.current and not sb.issues
        corpus, nums = corpus_for(p, seg, units)
        assert not [f for f in check_scenes(sb.scenes, n_turns=n, corpus=corpus, numbers_source=nums, figure_ids=set(), language="en", translate=False)
                    if f[1] == "major"]
    assert {sc.layout for seg in p.segments for sc in d.storyboard(p, seg, units, []).scenes} >= {"title", "bullets", "recap"}


def test_director_repairs_a_bad_plan_and_falls_back_when_it_cannot(ep):
    s, store, orch, pid = ep
    p, units = store.load(pid), store.load_units(pid)
    seg = p.segments[1]
    msgs: list[str] = []
    d = Director(make_clients(s)[0], say=msgs.append)
    fixed = d.storyboard(p, seg, units, [], instruction="MOCK_BAD_VISUAL", base=d.storyboard(p, seg, units, []))
    assert not fixed.issues and any("fixing" in m for m in msgs)
    stuck = d.storyboard(p, seg, units, [], instruction="MOCK_ALWAYS_BAD_VISUAL", base=d.storyboard(p, seg, units, []))
    assert any("1987" in i or "4812" in i for i in stuck.issues)  # the invented numbers are reported
    corpus, nums = corpus_for(p, seg, units)
    n = len(seg.version().turns)
    assert not [f for f in check_scenes(stuck.scenes, n_turns=n, corpus=corpus, numbers_source=nums, figure_ids=set(), language="en", translate=False)
                if f[1] == "major"]  # whatever is shown is safe
    assert "1987" not in " ".join(sc.visible_text() for sc in stuck.scenes)


def test_fallback_storyboard_uses_only_script_words(ep):
    s, store, orch, pid = ep
    p, units = store.load(pid), store.load_units(pid)
    for seg in p.segments:
        sb = fallback_storyboard(p, seg)
        assert sb.source == "fallback" and sb.scenes
        corpus, nums = corpus_for(p, seg, units)
        assert not [f for f in check_scenes(sb.scenes, n_turns=len(seg.version().turns), corpus=corpus, numbers_source=nums, figure_ids=set(),
                                            language="en", translate=False) if f[1] == "major"]


def test_director_survives_a_dead_planner(ep, monkeypatch):
    s, store, orch, pid = ep
    p, units = store.load(pid), store.load_units(pid)
    llm = make_clients(s)[0]
    from podcast_studio.clients.base import LLMError

    def boom(*a, **k):
        raise LLMError("the model is down", fatal=False)

    monkeypatch.setattr(llm, "json", boom)
    sb = Director(llm, say=lambda m: None).storyboard(p, p.segments[1], units, [])
    assert sb.source == "fallback" and sb.scenes and any("unavailable" in i.lower() for i in sb.issues)
    fatal = LLMError("bad key", fatal=True)
    monkeypatch.setattr(llm, "json", lambda *a, **k: (_ for _ in ()).throw(fatal))
    with pytest.raises(LLMError):  # a wrong key is not hidden behind a silent fallback
        Director(llm, say=lambda m: None).storyboard(p, p.segments[1], units, [])


# ------------------------------------------------------------------------------------------------ timeline
def test_timeline_covers_every_instant_exactly_once(vid):
    s, store, orch, pid = vid
    p = store.load(pid)
    sbs = {b.index: b for b in p.video.storyboards}
    shots = build_shots(p, sbs, total=p.final.duration)
    assert shots[0].start == 0.0 and abs(shots[-1].end - p.final.duration) < 1e-6
    for a, b in zip(shots, shots[1:]):
        assert abs(b.start - a.end) < 1e-6 and a.dur > 0
    # each chapter begins on the first scene of its own segment
    for n, mark in enumerate(p.final.chapters):
        first = next(x for x in shots if x.start >= mark.start - 1e-6)
        assert first.seg == n and first.scene == 0
    # captions are the spoken sentences, in order
    spoken = " ".join(t.text for seg in p.segments for t in seg.version().turns).split()
    shown = " ".join(x.text for x in shots if x.text).split()
    assert shown == spoken
    # the jingle and pauses carry no caption, and reveals never go backwards inside a scene
    assert shots[0].text == "" and shots[-1].text == ""
    seen: dict = {}
    for x in shots:
        assert x.reveal >= seen.get((x.seg, x.scene), 0)
        seen[(x.seg, x.scene)] = x.reveal
    no_caps = build_shots(p, sbs, total=p.final.duration, captions=False)
    assert not any(x.text for x in no_caps)


# ------------------------------------------------------------------------------------------------ figures
def test_extract_figures_finds_the_picture_and_its_caption(tmp_path):
    pdf = make_chapter_pdf(TEXT, tmp_path / "c.pdf")
    units = [SourceUnit(id=i, kind="para", text=t) for i, t in enumerate(TEXT.split("\n\n"), 1) if not t.startswith("#")]
    figs = extract_figures(pdf, tmp_path / "figs", units, project_dir=tmp_path)
    assert len(figs) == 1 and figs[0].caption.startswith("Fig. 2.1") and figs[0].unit_id is not None
    im = Image.open(tmp_path / figs[0].file)
    assert im.size[0] >= 120 and (tmp_path / figs[0].file).stat().st_size > 1000
    assert extract_figures(tmp_path / "missing.pdf", tmp_path / "f2", units, project_dir=tmp_path) == []  # never breaks the video


# ------------------------------------------------------------------------------------------------ rendering
@need_browser
def test_every_layout_renders_without_clipping_text(tmp_path):
    scenes = [
        Scene(layout="title", title="The Water Cycle: How Water Travels Around Our Planet", text="Episode 1"),
        Scene(layout="bullets", title="Why water moves", items=["The sun heats the oceans", "Vapour rises and cools", "Clouds form"], reveal=[0, 1, 2]),
        Scene(layout="steps", title="Steps", items=["Evaporation", "Condensation", "Precipitation", "Collection"], reveal=[0, 1, 2, 3]),
        Scene(layout="cycle", title="Water cycle", items=["Evaporation", "Condensation", "Precipitation", "Runoff"], reveal=[0, 1, 2, 3]),
        Scene(layout="definition", term="Transpiration", text="Plants release water vapour through tiny pores in their leaves."),
        Scene(layout="compare", title="A vs B", columns=["Evaporation", "Condensation"], rows=[["Liquid to gas", "Gas to liquid"], ["Needs heat", "Releases heat"]], reveal=[0, 1]),
        Scene(layout="keyfact", term="97%", text="of Earth's water is in the oceans"),
        Scene(layout="recap", title="Recap", items=["Water keeps moving", "The Sun powers it"], reveal=[0, 1]),
        Scene(layout="bullets", title="जल चक्र", items=["सूर्य की गर्मी से पानी भाप बनता है", "बादल बनते हैं"], reveal=[0, 1]),
    ]
    p = Project(id="x", title="t", settings=ProjectSettings(language="en"))
    from podcast_studio.models import Segment
    p.segments = [Segment(index=0, title="Intro")]
    cfg = VideoCfg(resolution="480p")
    with render.FrameRenderer(p, cfg, cache_dir=tmp_path / "cache", project_dir=tmp_path) as fr:
        for i, sc in enumerate(scenes):
            n = len(sc.rows) if sc.layout == "compare" else len(sc.items)
            st = (n, "A", "Kavya", "Water evaporates from oceans, lakes and rivers when the Sun heats it.")
            png = fr.render_scene(0, i, sc, [st], {})[st]
            im = Image.open(png)
            assert im.size == (854, 480)
            assert len(set(im.convert("L").resize((64, 36)).getdata())) > 20, f"scene {i} looks blank"
        assert fr.warnings == []
        assert fr.rendered == len(scenes)
    # the same frames again come from the cache and need no browser
    with render.FrameRenderer(p, cfg, cache_dir=tmp_path / "cache", project_dir=tmp_path) as fr2:
        sc = scenes[1]
        fr2.render_scene(0, 1, sc, [(3, "A", "Kavya", "Water evaporates from oceans, lakes and rivers when the Sun heats it.")], {})
        assert fr2.rendered == 0 and fr2.reused == 1 and fr2._page is None


@need_browser
def test_overflowing_text_is_shrunk_and_reported_when_it_cannot_fit(tmp_path):
    from podcast_studio.models import Segment
    p = Project(id="x", title="t", settings=ProjectSettings())
    p.segments = [Segment(index=0, title="Intro")]
    wall = " ".join(["supercalifragilisticexpialidocious"] * 60)
    sc = Scene(layout="definition", term="Term", text=wall)
    with render.FrameRenderer(p, VideoCfg(resolution="480p"), cache_dir=tmp_path / "c", project_dir=tmp_path) as fr:
        fr.render_scene(0, 0, sc, [(0, "", "", "")], {})
        assert fr.warnings and "does not fit" in fr.warnings[0]


# ------------------------------------------------------------------------------------------------ the MP4
@need_browser
@need_ffmpeg
def test_video_is_a_valid_mp4_in_sync_with_the_audio(vid):
    s, store, orch, pid = vid
    p = store.load(pid)
    v = p.video.final
    assert p.video.status == "ready" and v and not v.stale and not is_stale(p)
    f = store.dir(pid) / v.mp4
    pr = mux.probe(f)
    assert pr["video"] and pr["audio"] and pr["vcodec"] == "h264" and pr["acodec"] == "aac"
    assert (pr["width"], pr["height"]) == (854, 480) == (v.width, v.height)
    assert abs(pr["duration"] - p.final.duration) <= 0.3
    assert abs(pr["fps"] - 25) < 0.5
    assert (store.dir(pid) / v.poster).exists()
    assert v.warnings == []
    # every segment has its own scene thumbnails
    for sb in p.video.storyboards:
        for i in range(len(sb.scenes)):
            assert (store.dir(pid) / "video" / "scenes" / f"s{sb.index}_{i}.jpg").exists()
    # the chapter's own figure was used, with its original caption
    assert [f.caption for f in p.video.figures] == ["Fig. 2.1 Evaporation of water from a puddle"]
    assert any(sc.layout == "figure" for sb in p.video.storyboards for sc in sb.scenes)


@need_browser
@need_ffmpeg
def test_picture_really_changes_with_the_story(vid, tmp_path):
    """Frames taken at different moments differ (not one frozen picture), and the caption area is not empty while someone speaks."""
    s, store, orch, pid = vid
    p = store.load(pid)
    f = store.dir(pid) / p.video.final.mp4
    import subprocess
    stamps = [0.3] + [p.final.chapters[k].start + d for k in (1, 2) for d in (1.5, 3.0, 4.5, 6.0)]
    frames = []
    for t in stamps:
        out = tmp_path / f"f{t:.1f}.png"
        subprocess.run([audio.ffmpeg_path(), "-v", "error", "-y", "-ss", f"{t}", "-i", str(f), "-frames:v", "1", str(out)], check=True)
        with Image.open(out) as im:
            frames.append(im.convert("L").resize((96, 54)))
    assert len({bytes(x.tobytes()) for x in frames}) >= 5          # not one frozen picture
    # captions: the lower band holds the dark caption bar whenever someone is speaking (checked at 8 moments inside two segments)
    with_caption = [x for x in frames[1:] if min(bytes(x.crop((0, 40, 96, 52)).tobytes())) < 80]
    assert len(with_caption) >= 3


@need_browser
@need_ffmpeg
def test_validate_checks_the_video(vid, tmp_path):
    s, store, orch, pid = vid
    rep = validate_project(store, pid)
    vids = {c["name"]: c for c in rep["checks"] if c["area"] == "video"}
    assert {"Video file", "Video length matches audio", "Video up to date", "Visuals planned for every segment", "On-screen text comes from the script",
            "Every moment has a picture", "Text fits on screen"} <= set(vids)
    assert all(c["status"] == "pass" for c in vids.values()), vids
    s2, store2, _ = clone(store, tmp_path)
    (store2.dir(pid) / "video" / "episode.mp4").write_bytes(b"not a video")
    bad = {c["name"]: c for c in validate_project(store2, pid)["checks"] if c["area"] == "video"}
    assert bad["Video file"]["status"] == "fail"
    (store2.dir(pid) / "video" / "episode.mp4").unlink()
    assert any(c["status"] == "fail" and "missing" in c["detail"] for c in validate_project(store2, pid)["checks"] if c["area"] == "video")


@need_browser
@need_ffmpeg
def test_a_changed_segment_only_replans_and_redraws_that_segment(vid, tmp_path):
    s, store, orch, pid = vid
    s2, store2, orch2 = clone(store, tmp_path)
    before = {b.index: b.created_at for b in store2.load(pid).video.storyboards}
    orch2.run_regenerate(pid, 2, "add a real-life example with a kitchen", "rewrite", noprog)
    p = store2.load(pid)
    assert p.video.final.stale and is_stale(p)                      # the old video is flagged
    assert storyboard_state(p, 2) in {"stale", "ok"} and all(storyboard_state(p, i) == "ok" for i in (0, 1, 3, 4))
    llm_before = p.usage.llm_calls
    orch2.run_video(pid, noprog)
    q = store2.load(pid)
    assert not q.video.final.stale and not is_stale(q) and q.video.status == "ready"
    after = {b.index: b.created_at for b in q.video.storyboards}
    for i in (0, 1, 3, 4):
        assert after[i] == before[i], f"segment {i + 1} was replanned although its script did not change"
    assert all(storyboard_state(q, i) == "ok" for i in range(5))
    assert q.usage.llm_calls >= llm_before
    assert abs(mux.probe(store2.dir(pid) / q.video.final.mp4)["duration"] - q.final.duration) <= 0.3


@need_browser
@need_ffmpeg
def test_a_new_recording_with_the_same_words_keeps_the_visuals(vid, tmp_path):
    s, store, orch, pid = vid
    s2, store2, orch2 = clone(store, tmp_path)
    before = {b.index: b.created_at for b in store2.load(pid).video.storyboards}
    orch2.run_regenerate(pid, 1, None, "revoice", noprog)
    assert store2.load(pid).segments[1].current > 1
    calls = store2.load(pid).usage.llm_calls
    orch2.run_video(pid, noprog)
    q = store2.load(pid)
    assert {b.index: b.created_at for b in q.video.storyboards} == before   # no new storyboards
    assert q.usage.llm_calls == calls                                          # no model calls at all
    assert all(storyboard_state(q, i) == "ok" for i in range(5)) and not is_stale(q)


@need_browser
@need_ffmpeg
def test_reviewer_can_redo_one_segments_visuals_with_an_instruction(vid, tmp_path):
    s, store, orch, pid = vid
    s2, store2, orch2 = clone(store, tmp_path)
    before = {b.index: (b.created_at, [x.layout for x in b.scenes]) for b in store2.load(pid).video.storyboards}
    assert "cycle" not in [x for x in before[2][1]]
    orch2.run_video(pid, noprog, None, segments={2}, instruction="show this as a cycle", redo_visuals=True)
    q = store2.load(pid)
    now = {b.index: (b.created_at, [x.layout for x in b.scenes]) for b in q.video.storyboards}
    assert "cycle" in now[2][1] and now[2][0] != before[2][0]
    assert q.video.storyboards[2].instruction == "show this as a cycle"
    for i in (0, 1, 3, 4):
        assert now[i] == before[i]
    assert q.video.status == "ready" and not q.video.final.stale


@need_browser
@need_ffmpeg
def test_changing_theme_or_size_redraws_but_keeps_the_plan(vid, tmp_path):
    s, store, orch, pid = vid
    s2, store2, orch2 = clone(store, tmp_path)
    before = {b.index: b.created_at for b in store2.load(pid).video.storyboards}
    orch2.run_video(pid, noprog, VideoCfg(resolution="480p", theme="dark", captions=False))
    q = store2.load(pid)
    assert {b.index: b.created_at for b in q.video.storyboards} == before
    assert q.video.cfg.theme == "dark" and q.video.cfg.captions is False
    shots = build_shots(q, {b.index: b for b in q.video.storyboards}, total=q.final.duration, captions=False)
    assert not any(x.text for x in shots)


@need_browser
@need_ffmpeg
def test_new_episode_form_option_makes_the_video_right_after_the_audio(tmp_path):
    s = Settings.from_env(mock=True, data_dir=tmp_path / "d")
    store = ProjectStore(s.data_dir)
    orch = Orchestrator(s, store)
    p = orch.create_project(title="", settings=ProjectSettings(minutes=4, make_video=True), text=TEXT)
    orch.run_generate(p.id, noprog)
    q = store.load(p.id)
    assert q.status == "ready" and q.video.status == "ready" and q.video.final and (store.dir(p.id) / q.video.final.mp4).exists()
    # review-first flow: the audio step is run later and the video follows it
    p2 = orch.create_project(title="", settings=ProjectSettings(minutes=4, make_video=True, auto_voice=False), text=TEXT)
    orch.run_generate(p2.id, noprog)
    assert store.load(p2.id).video.final is None
    orch.run_voice(p2.id, noprog)
    assert store.load(p2.id).video.final is not None
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert "makevideo" in js and "make_video" in js


# ------------------------------------------------------------------------------------------------ failure modes
def test_video_needs_the_audio_first(tmp_path):
    s = Settings.from_env(mock=True, data_dir=tmp_path / "d")
    store = ProjectStore(s.data_dir)
    orch = Orchestrator(s, store)
    p = orch.create_project(title="", settings=ProjectSettings(minutes=3, auto_voice=False), text=TEXT)
    orch.run_generate(p.id, noprog)
    with pytest.raises(VideoError):
        orch.run_video(p.id, noprog)
    q = store.load(p.id)
    assert q.video.status == "error" and "audio" in q.video.message.lower() and q.status == "script_ready"


def test_missing_browser_gives_install_instructions_and_keeps_the_episode(tmp_path, monkeypatch):
    pytest.importorskip("playwright")
    s, store, orch, pid = make_episode(tmp_path / "d")
    if not (audio.ffmpeg_path() and mux.has_x264()):
        pytest.skip("ffmpeg needed to get as far as drawing")

    def none(pw):
        raise render.RenderError(render.INSTALL_HELP)

    monkeypatch.setattr(render, "_launch", none)
    with pytest.raises(render.RenderError):
        orch.run_video(pid, noprog, VideoCfg(resolution="480p"))
    q = store.load(pid)
    assert q.video.status == "error" and "playwright install chromium" in q.video.message
    assert q.status == "ready" and q.final and q.final.wav          # the audio episode is untouched
    assert len(q.video.storyboards) == 5                            # the planned visuals are kept for the retry


def test_missing_ffmpeg_stops_before_any_model_call(tmp_path, monkeypatch):
    s, store, orch, pid = make_episode(tmp_path / "d")
    calls = store.load(pid).usage.llm_calls
    monkeypatch.setattr(audio, "ffmpeg_path", lambda: None)
    with pytest.raises(mux.MuxError) as ei:
        orch.run_video(pid, noprog)
    assert "ffmpeg" in str(ei.value)
    q = store.load(pid)
    assert q.video.status == "error" and q.usage.llm_calls == calls and q.video.storyboards == []


def test_doctor_reports_the_video_tools_as_optional(tmp_path, capsys):
    from podcast_studio.cli import cmd_doctor
    from podcast_studio.doctor import is_optional, run_doctor
    s = Settings.from_env(mock=True, data_dir=tmp_path / "d")
    rows = {r["name"]: r for r in run_doctor(s)}
    assert "Video renderer" in rows and is_optional(rows["Video renderer"])
    assert cmd_doctor(None, s) == 0


# ------------------------------------------------------------------------------------------------ other languages
def test_native_script_slides_for_a_native_script_episode(ep):
    """With Devanagari lines the slides must be Devanagari too (checked with the same rules the director enforces)."""
    from podcast_studio.models import Turn
    s, store, orch, pid = ep
    p, units = store.load(pid), store.load_units(pid)
    p.settings.language = "hi"
    HI = ["जल चक्र एक बहुत सुंदर प्रक्रिया है।", "सूर्य की गर्मी से समुद्र का पानी भाप बनता है।", "भाप ऊपर उठकर ठंडी होती है और बादल बनते हैं।",
          "बादलों से वर्षा होती है और पानी नदियों में बहता है।", "यही चक्र हमेशा चलता रहता है।", "क्या आपने कभी पोखर को सूखते देखा है?"]
    for seg in p.segments:
        v = seg.version()
        v.turns = [Turn(speaker="A" if i % 2 == 0 else "B", text=HI[(i + seg.index) % len(HI)]) for i in range(len(v.turns))]
        seg.key_points = HI[:3]
    d = Director(make_clients(s)[0], say=lambda m: None)
    for seg in p.segments:
        sb = d.storyboard(p, seg, units, [])
        assert not sb.issues, sb.issues
        shown = " ".join(sc.visible_text() for sc in sb.scenes if sc.layout != "title")
        for name in brand_for(p):  # show, host and Latin plan titles are names, allowed in any script
            shown = shown.replace(name, " ")
        if shown.strip():
            letters = [c for c in shown if c.isalpha() and "\u0900" <= c <= "\u097f"]
            assert len(letters) / max(1, len([c for c in shown if c.isalpha()])) > 0.9, shown
    # an English slide for a Hindi episode is rejected...
    bad = Scene(layout="bullets", title="Water cycle", items=["Sun heats water", "Vapour rises"], from_turn=0, to_turn=1, reveal=[0, 1])
    corpus, nums = corpus_for(p, p.segments[1], units)
    assert any("script" in m[2] for m in check_scenes([bad], n_turns=2, corpus=corpus | {"water", "sun", "heats", "vapou"}, numbers_source=nums,
                                                       figure_ids=set(), language="hi", translate=False) if m[1] == "major")


@need_browser
@need_ffmpeg
def test_hindi_episode_builds_a_video_and_reports_honestly(tmp_path):
    """The mock 'Hindi' script mixes English plan sentences into Devanagari lines. Slides must still only show what is said, and the video
    must still be produced."""
    s, store, orch, pid = make_episode(tmp_path / "d", lang="hi")
    orch.run_video(pid, noprog, VideoCfg(resolution="480p"))
    q = store.load(pid)
    assert q.video.status == "ready" and q.video.final.mp4
    rep = {c["name"]: c for c in validate_project(store, pid)["checks"] if c["area"] == "video"}
    assert rep["On-screen text comes from the script"]["status"] != "fail", rep["On-screen text comes from the script"]
    assert rep["Video file"]["status"] == "pass"


# ------------------------------------------------------------------------------------------------ CLI
@need_browser
@need_ffmpeg
def test_cli_generate_video_then_regenerate_visuals(tmp_path, monkeypatch, capsys):
    from podcast_studio import cli
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    src = tmp_path / "chapter.txt"
    src.write_text(TEXT, encoding="utf-8")
    out = tmp_path / "out"
    assert cli.main(["--mock", "generate", str(src), "--minutes", "4", "--video", "--res", "480p", "--theme", "warm", "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert "Video:" in text and (out / "episode.mp4").exists() and (out / "podcast.mp3").exists()
    store = ProjectStore(tmp_path / "data")
    pid = store.list()[0]["id"]
    assert store.load(pid).video.cfg.theme == "warm"
    assert cli.main(["--mock", "vregen", pid, "2", "-i", "show it as numbered steps"]) == 0
    assert "steps" in [sc.layout for sc in store.load(pid).video.storyboards[1].scenes]
    assert cli.main(["--mock", "video", pid, "--res", "480p", "--no-captions", "--no-figures"]) == 0
    c = store.load(pid).video.cfg
    assert c.captions is False and c.use_figures is False
    assert cli.main(["--mock", "validate", pid]) in (0, 1)  # prints the video area
    assert "-- video" in capsys.readouterr().out


# ------------------------------------------------------------------------------------------------ API
def _wait(client, job_id, timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] in {"done", "error", "cancelled"}:
            return j
        time.sleep(0.3)
    raise AssertionError("job timed out")


@need_browser
@need_ffmpeg
def test_api_video_flow(tmp_path):
    s = Settings.from_env(mock=True, data_dir=tmp_path / "d")
    with TestClient(create_app(s)) as c:
        r = c.post("/api/projects", data={"text": TEXT, "settings_json": json.dumps({"minutes": 4})})
        pid = r.json()["project"]["id"]
        assert _wait(c, r.json()["job"]["id"])["status"] == "done"
        # validation of the request
        assert c.post(f"/api/projects/{pid}/video", json={"resolution": "8k"}).status_code == 422
        assert c.post(f"/api/projects/{pid}/video/segments/99/regenerate", json={}).status_code == 404
        j = c.post(f"/api/projects/{pid}/video", json={"resolution": "480p", "theme": "dark"}).json()["job"]
        assert _wait(c, j["id"])["status"] == "done"
        p = c.get(f"/api/projects/{pid}").json()["project"]
        v = p["video"]
        assert v["status"] == "ready" and v["cfg"]["theme"] == "dark" and v["final"]["mp4"] == "video/episode.mp4"
        full = c.get(f"/api/projects/{pid}/files/video/episode.mp4")
        assert full.status_code == 200 and full.headers["content-type"] == "video/mp4" and full.content[4:8] == b"ftyp"
        part = c.get(f"/api/projects/{pid}/files/video/episode.mp4", headers={"Range": "bytes=0-99"})
        assert part.status_code == 206 and len(part.content) == 100        # the player can seek
        assert "attachment" in c.get(f"/api/projects/{pid}/files/video/episode.mp4?download=1").headers["content-disposition"]
        assert c.get(f"/api/projects/{pid}/files/video/poster.jpg").status_code == 200
        assert c.get(f"/api/projects/{pid}/files/video/scenes/s0_0.jpg").status_code == 200
        for secret in ("source/units.json", "project.json", "video/../project.json", "../x"):
            assert c.get(f"/api/projects/{pid}/files/{secret}").status_code == 404
        # redo one segment's visuals
        j2 = c.post(f"/api/projects/{pid}/video/segments/1/regenerate", json={"instruction": "show it as a cycle"}).json()["job"]
        assert _wait(c, j2["id"])["status"] == "done"
        sb = c.get(f"/api/projects/{pid}").json()["project"]["video"]["storyboards"][1]
        assert sb["instruction"] == "show it as a cycle" and "cycle" in [x["layout"] for x in sb["scenes"]]
        # the UI ships the video controls
        js = c.get("/static/app.js").text
        assert "renderVideo" in js and "redo-visuals" in js and "scene_words" in js


def test_tuning_knows_the_video_settings():
    from podcast_studio import tuning
    t = tuning.current()
    assert 0 <= t.director_temperature <= 1 and 25 <= t.scene_words <= 140
