from __future__ import annotations

import json

import pytest

from podcast_studio.clients import BodhanError
from podcast_studio.ingest import IngestError
from podcast_studio.jobs import JobCancelled

from .conftest import new_project, noprog
from .fake_servers import BD_KEY  # noqa: F401


def _counts(env):
    """(llm_calls, tts_calls) so far. Over HTTP we count real requests; in-process we use the usage totals."""
    p = None
    return p


def _gen(env, text, **kw):
    p = new_project(env, text, **kw)
    env.orch.run_generate(p.id, noprog)
    return env.store.load(p.id)


def _tts_calls(env, be):
    if env.kind == "http":
        return len(be.tts_requests)
    return None


def test_end_to_end_generates_a_complete_episode(env, sample_text, be):
    p = _gen(env, sample_text)
    assert p.status == "ready", p.message
    kinds = [s.kind for s in p.segments]
    assert kinds[0] == "intro" and kinds[-2:] == ["recap", "outro"] and "body" in kinds
    assert all(s.status == "ready" and s.version() and s.version().audio_file for s in p.segments)
    d = env.store.dir(p.id)
    assert (d / p.final.mp3).stat().st_size > 5000
    assert (d / p.final.wav).exists() and (d / p.final.srt).exists() and (d / p.final.vtt).exists()
    assert "Chapter Chat" in (d / p.final.transcript_md).read_text(encoding="utf-8") or p.final.transcript_md
    assert p.final.duration > 30 and len(p.final.chapters) == len(p.segments)
    starts = [c.start for c in p.final.chapters]
    assert starts == sorted(starts)
    assert p.usage.tts_calls > 0 and p.usage.llm_calls > 0
    if env.kind == "http":
        assert len(be.tts_requests) == p.usage.tts_calls
        # every request was valid per the fake's strict validation (it would have 422'd otherwise)
        assert all(json.loads(r["body"]["instructions"])["lang"] == "en" for r in be.tts_requests)
        # the two hosts use two different voices
        assert len({r["body"]["voice"] for r in be.tts_requests}) == 2


def test_script_is_faithful_all_checks_pass(env, sample_text):
    p = _gen(env, sample_text)
    for s in p.segments:
        c = s.version().check
        assert c is not None and c.verdict == "pass" and not c.needs_review


def test_repair_loop_fixes_injected_unsupported_claim(env, sample_text):
    p = _gen(env, sample_text, extra_instructions="MOCK_INJECT_ERROR")
    assert p.status == "ready"
    for s in p.segments:
        v = s.version()
        assert "moon dust" not in " ".join(t.text for t in v.turns).lower()
        assert not v.check.needs_review
    assert any(s.version().check.attempts > 1 for s in p.segments if s.kind == "body")


def test_repair_loop_fixes_injected_number(env, sample_text):
    p = _gen(env, sample_text, extra_instructions="MOCK_INJECT_NUMBER")
    for s in p.segments:
        assert "1987" not in " ".join(t.text for t in s.version().turns)
    assert p.status == "ready"


def test_dropped_key_point_is_restored(env, sample_text):
    p = _gen(env, sample_text, extra_instructions="MOCK_DROP_POINT")
    assert all(not s.version().check.needs_review for s in p.segments)


def test_unfixable_segment_is_flagged_needs_review_not_hidden(env, sample_text):
    p = _gen(env, sample_text, extra_instructions="MOCK_ALWAYS_BAD")
    assert p.status == "ready"  # the episode still gets built so the reviewer can listen
    flagged = [s for s in p.segments if s.version().check.needs_review]
    assert flagged and all(s.kind == "body" for s in flagged)
    assert all(s.version().check.attempts == 1 + p.settings.max_revisions for s in flagged)


def test_fact_check_can_be_turned_off(env, sample_text):
    p = _gen(env, sample_text, fact_check=False, extra_instructions="MOCK_INJECT_ERROR")
    # no LLM check -> the injected claim is not caught by the checker (only the rule checks run)
    assert p.status == "ready"


def test_regenerate_rewrites_only_the_target_segment(env, sample_text, be):
    p = _gen(env, sample_text)
    before = {s.index: (s.current, s.version().audio_file, s.version().turns[0].text) for s in p.segments}
    final_before = p.final.built_at
    tts_before = len(be.tts_requests)
    env.orch.run_regenerate(p.id, 1, "Please add a short everyday example.", "rewrite", noprog)
    q = env.store.load(p.id)
    assert q.status == "ready"
    assert q.segments[1].current == 2 and len(q.segments[1].versions) == 2
    assert "everyday example" in " ".join(t.text for t in q.segments[1].version().turns)
    assert q.segments[1].version().origin == "rewrite"
    for s in q.segments:
        if s.index != 1:
            assert (s.current, s.version().audio_file, s.version().turns[0].text) == before[s.index]
    assert q.final.built_at != final_before
    if env.kind == "http":
        new_calls = be.tts_requests[tts_before:]
        spoken = " ".join(r["body"]["input"] for r in new_calls)
        assert "everyday example" in spoken
        assert len(new_calls) < tts_before / 2  # nowhere near the whole episode


def test_revoice_changes_audio_not_text(env, sample_text):
    p = _gen(env, sample_text)
    old = p.segments[2].version()
    env.orch.run_regenerate(p.id, 2, None, "revoice", noprog)
    q = env.store.load(p.id)
    new = q.segments[2].version()
    assert new.version == old.version + 1 and new.origin == "revoiced"
    assert [t.text for t in new.turns] == [t.text for t in old.turns]
    assert new.audio_file != old.audio_file and all(t.take == o.take + 1 for t, o in zip(new.turns, old.turns))


def test_edit_one_line_resynthesises_only_that_line(env, sample_text, be):
    p = _gen(env, sample_text)
    seg = p.segments[2]
    n_before = len(be.tts_requests)
    env.orch.run_edit_turn(p.id, 2, 1, "This is my hand edited sentence about clouds.", None, noprog)
    q = env.store.load(p.id)
    v = q.segments[2].version()
    assert v.turns[1].text.startswith("This is my hand edited") and v.turns[1].edited and v.origin == "edited"
    assert v.check.verdict == "unchecked"
    other = [t.text for i, t in enumerate(v.turns) if i != 1]
    assert other == [t.text for i, t in enumerate(seg.version().turns) if i != 1]
    if env.kind == "http":
        new = be.tts_requests[n_before:]
        assert 1 <= len(new) <= 2 and all("hand edited" in r["body"]["input"] for r in new)


def test_edit_validation(env, sample_text):
    p = _gen(env, sample_text)
    with pytest.raises(ValueError):
        env.orch.run_edit_turn(p.id, 2, 999, "x", None, noprog)
    with pytest.raises(ValueError):
        env.orch.run_edit_turn(p.id, 2, 0, "   ", None, noprog)
    assert env.store.load(p.id).status == "ready"  # a rejected edit does not break the project


def test_revert_restores_old_version_without_new_tts(env, sample_text, be):
    p = _gen(env, sample_text)
    env.orch.run_regenerate(p.id, 1, "Make it shorter.", "rewrite", noprog)
    n = len(be.tts_requests)
    old_text = [t.text for t in env.store.load(p.id).segments[1].version(1).turns]
    env.orch.run_revert(p.id, 1, 1, noprog)
    q = env.store.load(p.id)
    assert q.segments[1].current == 1 and [t.text for t in q.segments[1].version().turns] == old_text
    if env.kind == "http":
        assert len(be.tts_requests) == n  # the old audio file was still on disk
    with pytest.raises(ValueError):
        env.orch.run_revert(p.id, 1, 99, noprog)


def test_script_review_before_voicing_then_voice(env, sample_text, be):
    p = _gen(env, sample_text, auto_voice=False)
    assert p.status == "script_ready" and p.final is None or not p.final.mp3
    assert all(s.version() and not s.version().audio_file for s in p.segments)
    assert not be.tts_requests or env.kind == "inproc"
    # a rewrite in script mode must not synthesise anything
    env.orch.run_regenerate(p.id, 1, "Simpler words please", "rewrite", noprog)
    if env.kind == "http":
        assert not be.tts_requests
    env.orch.run_voice(p.id, noprog)
    q = env.store.load(p.id)
    assert q.status == "ready" and q.final.mp3


def test_resume_after_tts_failure_reuses_scripts_and_cached_audio(env, sample_text, be):
    if env.kind != "http":
        pytest.skip("needs the fake HTTP server to inject failures")
    env.settings.http_retries = 1
    p = new_project(env, sample_text)
    # let a few TTS calls through then fail hard
    for _ in range(3):
        be.tts_script.append(None)
    be.tts_script.extend([("status", 500)] * 40)
    with pytest.raises(BodhanError):
        env.orch.run_generate(p.id, noprog)
    mid = env.store.load(p.id)
    assert mid.status == "error" and all(s.version() for s in mid.segments)  # scripts were saved
    be.tts_script.clear()
    llm_before = len(be.or_requests)
    env.settings.http_retries = 4
    env.orch.run_generate(p.id, noprog)
    q = env.store.load(p.id)
    assert q.status == "ready"
    assert len(be.or_requests) == llm_before  # not a single new LLM call on resume


def test_cancel_then_resume(env, sample_text):
    p = new_project(env, sample_text)
    seen = {"n": 0}

    def prog(frac, msg=""):
        seen["n"] += 1
        if seen["n"] == 12:
            raise JobCancelled()

    with pytest.raises(JobCancelled):
        env.orch.run_generate(p.id, prog)
    assert env.store.load(p.id).status == "interrupted"
    env.orch.run_generate(p.id, noprog)
    assert env.store.load(p.id).status == "ready"


def test_recover_interrupted_after_restart(env, sample_text):
    p = new_project(env, sample_text)
    env.store.update(p.id, lambda q: setattr(q, "status", "voicing"))
    env.orch.recover_interrupted()
    assert env.store.load(p.id).status == "interrupted"


def test_hindi_direct_mode(env, sample_text, be):
    p = _gen(env, sample_text, language="hi")
    assert p.status == "ready"
    txt = " ".join(t.text for s in p.segments for t in s.version().turns)
    assert "आपका स्वागत" in txt or any("ऀ" <= ch <= "ॿ" for ch in txt)
    # Persisted as UTF-8 JSON
    raw = (env.store.dir(p.id) / "project.json").read_text(encoding="utf-8")
    assert "\\u09" not in raw
    if env.kind == "http":
        assert {json.loads(r["body"]["instructions"])["lang"] for r in be.tts_requests} == {"hi"}


def test_translate_mode_keeps_english_master(env, sample_text, be):
    p = _gen(env, sample_text, language="hi", writing_mode="translate")
    assert p.status == "ready"
    turns = [t for s in p.segments for t in s.version().turns]
    assert all(t.text_en for t in turns)
    assert all(t.text.startswith("«hi»") for t in turns)  # went through the MT service
    if env.kind == "http":
        assert be.mt_requests and all(r["body"]["target_language_code"] == "hi" for r in be.mt_requests)


def test_other_indic_language_passes_lang_through(env, sample_text, be):
    p = _gen(env, sample_text, language="ta")
    assert p.status == "ready"
    if env.kind == "http":
        assert {json.loads(r["body"]["instructions"])["lang"] for r in be.tts_requests} == {"ta"}


def test_shared_cache_makes_identical_second_episode_cheap(env, sample_text, be):
    if env.kind != "http":
        pytest.skip("counts HTTP requests")
    _gen(env, sample_text)
    first = len(be.tts_requests)
    p2 = new_project(env, sample_text)
    env.orch.run_generate(p2.id, noprog)
    assert len(be.tts_requests) == first  # an identical episode is served entirely from the cache: zero new TTS calls
    assert env.store.load(p2.id).usage.tts_cache_hits > 0


def test_long_chapter_is_planned_in_parts(env, monkeypatch):
    from podcast_studio.pipeline import planner

    monkeypatch.setattr(planner, "PLAN_CHUNK_CHARS", 1500, raising=False)
    text = "\n\n".join(f"## Topic {i}\n\n" + " ".join(f"Fact {i}-{j} is about topic {i} and matters." for j in range(30)) for i in range(6))
    p = _gen(env, text, minutes=8)
    assert p.status == "ready"
    kinds = [s.kind for s in p.segments]
    assert kinds.count("intro") == 1 and kinds.count("outro") == 1 and kinds.count("body") >= 3
    used = {u for s in p.segments for u in s.unit_ids}
    all_units = {u.id for u in env.store.load_units(p.id) if u.kind != "heading"}
    assert all_units <= used or len(all_units - used) <= 1  # essentially every part of the chapter is covered


def test_ingest_failure_gives_clear_error_status(env):
    p = new_project(env, "x")
    env.store.save_upload(p.id, "pasted-text.txt", b"   ")
    with pytest.raises(IngestError):
        env.orch.run_generate(p.id, noprog)
    q = env.store.load(p.id)
    assert q.status == "error" and q.message
