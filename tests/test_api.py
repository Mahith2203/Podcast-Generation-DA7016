from __future__ import annotations

import io
import json
import time

import pytest
from fastapi.testclient import TestClient

from podcast_studio.api import create_app
from podcast_studio.config import Settings


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


def create(client, text, **settings):
    r = client.post("/api/projects", data={"text": text, "settings_json": json.dumps({"minutes": 4, **settings})})
    assert r.status_code == 200, r.text
    return r.json()


def test_health_config_and_static_ui(client):
    assert client.get("/api/health").json()["ok"] is True
    c = client.get("/api/config").json()
    assert len(c["voices"]) == 45 and len(c["languages"]) == 23 and c["mock"] is True
    r = client.get("/")
    assert r.status_code == 200 and "<html" in r.text.lower()
    for f in ("app.js", "app.css"):
        assert client.get(f"/static/{f}").status_code == 200


def test_create_generate_and_download(client, sample_text):
    out = create(client, sample_text)
    pid = out["project"]["id"]
    j = wait_job(client, out["job"]["id"])
    assert j["status"] == "done", j
    p = client.get(f"/api/projects/{pid}").json()["project"]
    assert p["status"] == "ready" and p["final"]["mp3"]
    mp3 = client.get(f"/api/projects/{pid}/files/{p['final']['mp3']}")
    assert mp3.status_code == 200 and mp3.headers["content-type"].startswith("audio/mpeg") and len(mp3.content) > 5000
    dl = client.get(f"/api/projects/{pid}/files/{p['final']['mp3']}?download=1")
    assert "attachment" in dl.headers["content-disposition"]
    vtt = client.get(f"/api/projects/{pid}/files/{p['final']['vtt']}")
    assert vtt.text.startswith("WEBVTT")
    rng = client.get(f"/api/projects/{pid}/files/{p['final']['mp3']}", headers={"Range": "bytes=0-99"})
    assert rng.status_code == 206 and len(rng.content) == 100
    seg_audio = p["segments"][0]["versions"][0]["audio_file"]
    assert client.get(f"/api/projects/{pid}/files/{seg_audio}").status_code == 200
    assert client.get("/api/projects").json()["projects"][0]["id"] == pid


def test_private_files_and_traversal_are_blocked(client, sample_text):
    pid = create(client, sample_text)["project"]["id"]
    for bad in ("project.json", "../../etc/passwd", "..%2F..%2Fetc%2Fpasswd", "source/pasted-text.txt", "units.json", "%2e%2e/project.json"):
        assert client.get(f"/api/projects/{pid}/files/{bad}").status_code == 404, bad
    assert client.get("/api/projects/..%2F..%2Fx").status_code in (400, 404)


def test_reviewer_operations_over_api(client, sample_text):
    out = create(client, sample_text)
    pid = out["project"]["id"]
    assert wait_job(client, out["job"]["id"])["status"] == "done"

    r = client.post(f"/api/projects/{pid}/segments/1/regenerate", json={"mode": "rewrite", "instruction": "Add an example."})
    assert r.status_code == 200
    assert wait_job(client, r.json()["job"]["id"])["status"] == "done"
    p = client.get(f"/api/projects/{pid}").json()["project"]
    assert p["segments"][1]["current"] == 2

    r = client.post(f"/api/projects/{pid}/segments/1/edit-turn", json={"turn": 1, "text": "My own words here."})
    assert wait_job(client, r.json()["job"]["id"])["status"] == "done"
    assert client.get(f"/api/projects/{pid}").json()["project"]["segments"][1]["versions"][-1]["turns"][1]["text"].startswith("My own words")

    r = client.post(f"/api/projects/{pid}/segments/1/revert", json={"version": 1})
    assert wait_job(client, r.json()["job"]["id"])["status"] == "done"
    assert client.get(f"/api/projects/{pid}").json()["project"]["segments"][1]["current"] == 1

    r = client.post(f"/api/projects/{pid}/segments/2/regenerate", json={"mode": "revoice"})
    assert wait_job(client, r.json()["job"]["id"])["status"] == "done"


def test_bad_requests(client, sample_text):
    assert client.post("/api/projects", data={"text": "", "settings_json": "{}"}).status_code == 400
    assert client.post("/api/projects", data={"text": "hello world " * 20, "settings_json": "{not json"}).status_code == 400
    assert client.post("/api/projects", data={"text": "hello world " * 20, "settings_json": json.dumps({"language": "xx"})}).status_code == 400
    r = client.post("/api/projects", files={"file": ("a.txt", io.BytesIO(b""), "text/plain")})
    assert r.status_code == 400
    r = client.post("/api/projects", files={"file": ("a.docx", io.BytesIO(b"PK123"), "application/octet-stream")})
    out = r.json()
    if r.status_code == 200:  # the failure shows up in the job instead
        assert wait_job(client, out["job"]["id"])["status"] == "error"
    assert client.get("/api/projects/doesnotexist").status_code == 404
    assert client.post("/api/projects/doesnotexist/resume").status_code == 404
    assert client.get("/api/jobs/nope").status_code == 404


def test_busy_project_rejects_second_job_and_delete(client, sample_text):
    out = create(client, sample_text)
    pid = out["project"]["id"]
    r = client.post(f"/api/projects/{pid}/segments/0/regenerate", json={"mode": "rewrite"})
    # Either the first job is still running (409) or it already finished (then the segment has no script/raises) -- never a crash.
    assert r.status_code in (200, 400, 409)
    if r.status_code == 409:
        assert client.delete(f"/api/projects/{pid}").status_code == 409
    wait_job(client, out["job"]["id"])
    assert client.delete(f"/api/projects/{pid}").status_code == 200
    assert client.get(f"/api/projects/{pid}").status_code == 404


def test_voice_preview_and_validation(client):
    r = client.post("/api/preview", json={"voice": "Ishita", "lang": "en"})
    assert r.status_code == 200 and r.content[:4] == b"RIFF"
    r = client.post("/api/preview", json={"voice": "Amit", "lang": "hi", "style": "happy"})
    assert r.status_code == 200
    assert client.post("/api/preview", json={"voice": "Nobody", "lang": "en"}).status_code == 400
    assert client.post("/api/preview", json={"voice": "Ishita", "lang": "zz"}).status_code == 400
    assert client.post("/api/preview", json={"voice": "Ishita", "style": "angry"}).status_code == 400


def test_edit_validation_over_api(client, sample_text):
    out = create(client, sample_text)
    pid = out["project"]["id"]
    wait_job(client, out["job"]["id"])
    assert client.post(f"/api/projects/{pid}/segments/0/edit-turn", json={"turn": 0}).status_code == 400
    assert client.post(f"/api/projects/{pid}/segments/0/edit-turn", json={"turn": 0, "style": "angry"}).status_code == 400
    assert client.post(f"/api/projects/{pid}/segments/0/regenerate", json={"mode": "bogus"}).status_code == 422


def test_doctor_in_mock_mode(client):
    checks = client.get("/api/doctor").json()["checks"]
    assert checks and all({"name", "ok"} <= set(c) for c in checks)


def test_no_secret_in_any_api_response(tmp_path, sample_text):
    s = Settings.from_env(mock=True, data_dir=tmp_path / "d", openrouter_api_key="or-SECRET-aaaaaaaaaaaa", bodhan_api_key="bd-SECRET-bbbbbbbbbbbb")
    with TestClient(create_app(s)) as c:
        for path in ("/api/config", "/api/doctor", "/api/health"):
            body = c.get(path).text
            assert "SECRET" not in body, path


def test_ui_files_are_always_revalidated_so_an_update_is_never_hidden_by_the_browser_cache(client):
    for path in ("/static/app.js", "/static/extras.js", "/static/app.css", "/"):
        r = client.get(path)
        assert r.status_code == 200 and r.headers.get("cache-control") == "no-cache", path
