"""Study pack, Ask the hosts and insights: demo-mode end-to-end through the API, plus the real-model code paths with a scripted fake model."""
from __future__ import annotations

import json
import re
import time

import pytest
from fastapi.testclient import TestClient

from podcast_studio.api import create_app
from podcast_studio.clients import UsageMeter
from podcast_studio.clients.base import LLMBase
from podcast_studio.clients.mock import MockBodhan
from podcast_studio.config import Settings
from podcast_studio.extras import ask as ask_mod
from podcast_studio.extras import common, learn
from podcast_studio.models import ProjectSettings
from podcast_studio.pipeline.orchestrator import Orchestrator
from podcast_studio.store import ProjectStore


# --------------------------------------------------------------------------- helpers
@pytest.fixture
def client(tmp_path):
    s = Settings.from_env(mock=True, data_dir=tmp_path / "d")
    with TestClient(create_app(s)) as c:
        yield c


def wait_job(client, job_id, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] in {"done", "error", "cancelled"}:
            return j
        time.sleep(0.2)
    raise AssertionError("job timed out")


@pytest.fixture
def episode(client, sample_text):
    r = client.post("/api/projects", data={"text": sample_text, "settings_json": json.dumps({"minutes": 4})})
    assert r.status_code == 200, r.text
    pid = r.json()["project"]["id"]
    assert wait_job(client, r.json()["job"]["id"])["status"] == "done"
    return pid


def wait_learn(client, pid, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        d = client.get(f"/api/projects/{pid}/learn").json()
        if d["status"] in {"ready", "error"}:
            return d
        time.sleep(0.2)
    raise AssertionError("study pack timed out")


# --------------------------------------------------------------------------- API (demo mode)
def test_extras_ping_and_404s(client):
    d = client.get("/api/extras").json()
    assert d["ok"] and set(d["features"]) == {"learn", "ask", "insights"}
    for path in ("learn", "ask", "insights"):
        assert client.get(f"/api/projects/260101-abcdef/{path}").status_code == 404


def test_study_pack_flow(client, episode):
    assert client.get(f"/api/projects/{episode}/learn").json() == {"status": "none"}
    r = client.post(f"/api/projects/{episode}/learn")
    assert r.status_code == 200 and r.json()["status"] in {"working", "ready"}
    d = wait_learn(client, episode)
    assert d["status"] == "ready", d
    assert len(d["takeaways"]) >= 3 and len(d["glossary"]) >= 3 and len(d["quiz"]) >= 4
    for q in d["quiz"]:
        assert len(q["options"]) == 4 and len(set(q["options"])) == 4 and 0 <= q["answer"] < 4
        assert q["source"] and q["source"]["section"] is not None
    assert len({q["answer"] for q in d["quiz"]}) > 1  # the right answer is not always in the same place
    # asking again returns the saved pack without another run
    assert client.post(f"/api/projects/{episode}/learn").json()["status"] == "ready"
    sheet = client.get(f"/api/projects/{episode}/learn/sheet")
    assert sheet.status_code == 200 and "Answer key" in sheet.text and "Glossary" in sheet.text
    assert "attachment" in client.get(f"/api/projects/{episode}/learn/sheet?download=1").headers["content-disposition"]


def test_ask_answers_from_the_chapter_and_declines_outside_it(client, episode):
    r = client.post(f"/api/projects/{episode}/ask", json={"question": "What is evaporation?"})
    assert r.status_code == 200, r.text
    a = r.json()
    assert a["grounded"] is True and a["sources"] and a["duration"] > 2 and len(a["turns"]) >= 3
    assert a["turns"][0]["speaker"] == "A" and all(t["end"] > t["start"] for t in a["turns"])
    wav = client.get(f"/api/projects/{episode}/{a['audio']}")
    assert wav.status_code == 200 and wav.content[:4] == b"RIFF" and wav.headers["content-type"] == "audio/wav"

    b = client.post(f"/api/projects/{episode}/ask", json={"question": "Who won the cricket world cup in 2011?"}).json()
    assert b["grounded"] is False and b["sources"] == [] and "not" in b["turns"][1]["text"].lower()

    rows = client.get(f"/api/projects/{episode}/ask").json()["answers"]
    assert [x["id"] for x in rows] == [1, 2]
    p = client.get(f"/api/projects/{episode}").json()["project"]
    assert p["usage"]["tts_calls"] > 0
    assert client.post(f"/api/projects/{episode}/ask", json={"question": "hi"}).status_code == 422  # too short
    assert client.delete(f"/api/projects/{episode}/ask").json()["ok"]
    assert client.get(f"/api/projects/{episode}/ask").json()["answers"] == []
    assert client.get(f"/api/projects/{episode}/ask/1/audio").status_code == 404


def test_insights_and_report(client, episode):
    d = client.get(f"/api/projects/{episode}/insights").json()
    p = client.get(f"/api/projects/{episode}").json()["project"]
    assert len(d["segments"]) == len(p["segments"]) and d["duration_s"] > 30 and d["demo_mode"] is True
    assert d["loop"]["segments"] == len(p["segments"]) and d["loop"]["needs_review"] == 0
    assert d["coverage"]["covered_share"] >= 0.5 and d["grounding"]["grounded_share"] == 1.0
    assert abs(d["speaking"]["A"]["share"] + d["speaking"]["B"]["share"] - 1) < 0.01
    assert d["validation"]["score"] > 50 and d["validation"]["checks"]
    html = client.get(f"/api/projects/{episode}/insights/report")
    assert html.status_code == 200 and "Demo mode" in html.text and d["title"].split()[0] in html.text
    assert "<script" not in html.text.lower()
    assert client.get(f"/api/projects/{episode}/insights?validation=0").json()["validation"] is None


def test_not_ready_projects_are_refused(client):
    r = client.post("/api/projects", data={"text": "x", "settings_json": json.dumps({"auto_voice": False})})
    pid = r.json()["project"]["id"]
    wait_job(client, r.json()["job"]["id"])
    # whatever state it ended in (error / script ready), a project without a complete script cannot be asked
    p = client.get(f"/api/projects/{pid}").json()["project"]
    if not p["segments"] or not all(s["versions"] for s in p["segments"]):
        assert client.post(f"/api/projects/{pid}/ask", json={"question": "What is this?"}).status_code == 409
        assert client.post(f"/api/projects/{pid}/learn").status_code == 409


# --------------------------------------------------------------------------- pure functions
def test_rank_units_finds_the_right_paragraph(sample_text, mock_settings):
    from podcast_studio.ingest import ingest_text
    units = ingest_text(sample_text).units
    top = common.rank_units("How do clouds form from vapour?", units, k=3)
    assert top and "cloud" in top[0][1].text.lower()
    assert common.rank_units("zzzz qqqq", units) == []
    assert common.query_overlap("What is evaporation?", "Evaporation is when water turns to vapour.") > 0.9
    assert common.query_overlap("cricket world cup", "Evaporation is when water turns to vapour.") == 0


def test_validate_pack_is_strict_about_quiz_shape():
    good = {"takeaways": [{"text": "a", "unit_id": 1}] * 3, "glossary": [{"term": "T", "definition": "d", "unit_id": 1}],
            "quiz": [{"question": f"q{i}", "options": ["a", "b", "c", "d"], "answer": "B", "explanation": "e", "unit_id": 1} for i in range(4)]}
    out = learn.validate_pack(good, {1})
    assert out["quiz"][0]["answer"] == 1 and out["quiz"][0]["level"] == "medium"
    bad = json.loads(json.dumps(good))
    bad["quiz"][0]["options"] = ["a", "a", "c", "d"]           # duplicate options
    bad["quiz"][1]["options"] = ["a", "b", "c", "all of the above"]
    bad["quiz"][2]["answer"] = 9
    with pytest.raises(ValueError, match="at least"):
        learn.validate_pack(bad, {1})


def test_ground_filter_drops_items_with_numbers_not_in_the_chapter():
    pack = {"takeaways": [{"text": "Oceans hold about 97 percent of water."}, {"text": "The sea is 4,812 metres deep."}],
            "glossary": [], "quiz": []}
    kept, dropped = learn.ground_filter(pack, "Nearly 97 percent of all that water is in the oceans.")
    assert [t["text"] for t in kept["takeaways"]] == ["Oceans hold about 97 percent of water."]
    assert dropped and "4812" in dropped[0]["why"]


# --------------------------------------------------------------------------- real-model code paths with a scripted model
class ScriptedLLM(LLMBase):
    """Not a MockLLM, so the extras take the same path they take with OpenRouter. Replies come from `plan[task]` (a list consumed in order)."""

    def __init__(self, plan):
        self.plan, self.calls, self.seen = plan, [], []

    def chat_full(self, messages, *, model=None, temperature=0.7, max_tokens=4096, json_mode=False, task=""):
        tag = re.search(r"### TASK:\s*(\w+)", messages[0]["content"]).group(1)
        self.calls.append(tag)
        self.seen.append(messages[1]["content"])
        q = self.plan[tag]
        item = q.pop(0) if len(q) > 1 else q[0]
        return json.dumps(item), "stop"


@pytest.fixture
def finished(tmp_path, sample_text):
    s = Settings.from_env(mock=True, data_dir=tmp_path / "data")
    store = ProjectStore(s.data_dir)
    orch = Orchestrator(s, store)
    p = orch.create_project(title="", settings=ProjectSettings(minutes=4), text=sample_text)
    orch.run_generate(p.id, lambda f, m="": None)
    return s, store, store.load(p.id), store.load_units(p.id)


def _quiz_item(unit, ok_idx=0, extra=""):
    return {"question": f"What does the chapter say here? {extra}", "options": ["right one", "wrong one", "other wrong", "also wrong"], "answer": ok_idx,
            "explanation": "Because the chapter says so.", "unit_id": unit, "level": "easy"}


def test_study_pack_with_a_real_model_checks_numbers_and_uses_the_verifier(finished):
    s, store, p, units = finished
    uid = units[2].id
    raw = {"takeaways": [{"text": f"Takeaway {i}.", "unit_id": uid} for i in range(4)] + [{"text": "It happened in 1987 exactly.", "unit_id": uid}],
           "glossary": [{"term": "Evaporation", "definition": "Water turns to vapour.", "unit_id": uid}],
           "quiz": [_quiz_item(uid, extra=f"({i})") for i in range(5)]}
    verdict = {"results": [{"id": "q1", "ok": False, "reason": "two answers fit"}, {"id": "t0", "ok": True}]}
    llm = ScriptedLLM({"learn": [raw], "verify_learn": [verdict]})
    pack = learn.build_pack(p, units, llm, MockBodhan(), s)
    assert llm.calls == ["learn", "verify_learn"]
    texts = [t["text"] for t in pack["takeaways"]]
    assert not any("1987" in t for t in texts) and len(texts) == 4          # rule check dropped the ungrounded number
    assert len(pack["quiz"]) == 4 and pack["meta"]["verified_by_model"] is True  # verifier dropped q1
    assert {d["kind"] for d in pack["meta"]["dropped"]} == {"takeaway", "quiz"}
    for q in pack["quiz"]:                                                   # options were reshuffled but the right one is still marked
        assert q["options"][q["answer"]] == "right one"


def test_study_pack_survives_a_failed_verifier(finished):
    s, store, p, units = finished
    uid = units[2].id
    raw = {"takeaways": [{"text": f"Takeaway {i}.", "unit_id": uid} for i in range(4)], "glossary": [],
           "quiz": [_quiz_item(uid, extra=f"({i})") for i in range(5)]}
    llm = ScriptedLLM({"learn": [raw], "verify_learn": [{"nonsense": True}]})
    pack = learn.build_pack(p, units, llm, MockBodhan(), s)
    assert pack["meta"]["verified_by_model"] is False and len(pack["quiz"]) == 5


def test_translate_mode_study_pack_is_translated_and_keeps_an_english_master(tmp_path, sample_text):
    s = Settings.from_env(mock=True, data_dir=tmp_path / "data")
    store = ProjectStore(s.data_dir)
    orch = Orchestrator(s, store)
    st = ProjectSettings(minutes=4, language="hi", writing_mode="translate")
    p = orch.create_project(title="", settings=st, text=sample_text)
    orch.run_generate(p.id, lambda f, m="": None)
    p, units = store.load(p.id), store.load_units(p.id)
    uid = units[2].id
    raw = {"takeaways": [{"text": f"Takeaway {i}.", "unit_id": uid} for i in range(4)], "glossary": [],
           "quiz": [_quiz_item(uid, extra=f"({i})") for i in range(5)]}
    llm = ScriptedLLM({"learn": [raw], "verify_learn": [{"results": []}]})
    pack = learn.build_pack(p, units, llm, MockBodhan(), s)
    assert pack["translated"] and pack["takeaways"][0]["text"].startswith("«hi»")  # the mock translator tags what it translated
    assert pack["master_en"]["takeaways"][0]["text"] == "Takeaway 0."
    assert "English" in llm.seen[0] or '"en"' in llm.seen[0]                        # the model was asked to write English


def _ask_reply(text, grounded=True, uid=None):
    return {"grounded": grounded, "unit_ids": [uid] if uid else [], "turns": [
        {"speaker": "A", "text": "A listener asks about this.", "style": None, "reaction": False},
        {"speaker": "B", "text": text, "style": None, "reaction": False},
        {"speaker": "A", "text": "Oh, I see!", "style": None, "reaction": True}]}


def _ask(finished, llm, q="How do clouds form?"):
    s, store, p, units = finished
    return ask_mod.answer_question(p, units, q, llm, MockBodhan(), UsageMeter(), s, store), store, p


OK_CHECK = {"verdict": "pass", "issues": [], "missing_points": [], "notes": ""}


def test_ask_repairs_an_answer_that_fails_the_checks(finished):
    s, store, p, units = finished
    uid = next(u.id for u in units if "cloud" in u.text.lower())
    bad = _ask_reply("Scientists measured this in 1987 and again in 2043.", uid=uid)
    good = _ask_reply("Water vapour cools and turns into tiny droplets that make clouds.", uid=uid)
    llm = ScriptedLLM({"ask": [bad, good], "check": [OK_CHECK]})
    entry, store, p = _ask(finished, llm)
    assert entry["attempts"] == 2 and entry["needs_review"] is False and entry["grounded"] and entry["checked"]
    assert "problems_in_your_previous_answer" in llm.seen[1] and "1987" in llm.seen[1]
    assert llm.calls.count("ask") == 2


def test_ask_falls_back_to_the_chapter_when_repair_fails(finished):
    s, store, p, units = finished
    bad = _ask_reply("It was proven in 1987 by 4,812 experts.", uid=units[2].id)
    llm = ScriptedLLM({"ask": [bad], "check": [OK_CHECK]})
    entry, store, p = _ask(finished, llm)
    assert entry["needs_review"] is True and entry["attempts"] == 2
    assert all("1987" not in t["text"] for t in entry["turns"])


def test_ask_declined_answers_skip_the_fact_checker_and_still_get_voiced(finished):
    declined = _ask_reply("The chapter does not cover that, so we will not guess.", grounded=False)
    llm = ScriptedLLM({"ask": [declined], "check": [OK_CHECK]})
    entry, store, p = _ask(finished, llm, "Who is the prime minister?")
    assert entry["grounded"] is False and "check" not in llm.calls and entry["duration"] > 1
    assert ask_mod.audio_path(store, p.id, entry["id"]) is not None


def test_extras_css_never_restyles_the_main_apps_classes():
    """extras.css is loaded the first time an episode is opened and then stays on the page. If it reused a class the rest of
    the app already styles (it once reused `.stack`), the New episode form collapsed until the page was reloaded."""
    import re
    from pathlib import Path

    static = Path(__file__).resolve().parent.parent / "static"

    def strip(css: str) -> str:
        return re.sub(r"/\*.*?\*/", "", css, flags=re.S)

    app, extra = strip((static / "app.css").read_text("utf-8")), strip((static / "extras.css").read_text("utf-8"))
    app_classes = set(re.findall(r"\.([A-Za-z_][\w-]*)", " ".join(m.group(1) for m in re.finditer(r"([^{}]+)\{", app) if not m.group(1).strip().startswith("@"))))
    roots = set()
    for m in re.finditer(r"([^{}]+)\{", extra):
        if m.group(1).strip().startswith("@"):
            continue
        for part in m.group(1).split(","):
            found = re.match(r"\s*\.([A-Za-z_][\w-]*)", part)
            if found:
                roots.add(found.group(1))
    assert not (roots & app_classes), f"extras.css styles classes that app.css also uses: {sorted(roots & app_classes)}"
    kf = lambda css: set(re.findall(r"@keyframes\s+([\w-]+)", css))
    assert not (kf(app) & kf(extra)), f"same @keyframes name in both files: {sorted(kf(app) & kf(extra))}"


def test_3d_stage_hosts_have_no_css_filter():
    """A CSS `filter` on `.host-card` makes it the containing block of the absolutely positioned name plate and speech bubble,
    which sent the quiet host's name plate flying off to the side while the other host was talking. In 3D mode the cards
    must therefore end up with `filter: none`, also for the `:not(.talking)` state."""
    import re
    from pathlib import Path

    css = (Path(__file__).resolve().parent.parent / "static" / "app.css").read_text("utf-8")
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    assert re.search(r"\.stage\.is3d \.host-card:not\(\.talking\)[^{]*\{[^}]*filter:\s*none", css)
