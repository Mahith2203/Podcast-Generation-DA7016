"""Local HTTP fakes of OpenRouter and Bodhan, built from the documented request/response shapes.
They let the *real* client classes be tested end to end (headers, JSON bodies, retries, error handling)."""
from __future__ import annotations

import json
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from podcast_studio.audio import array_to_wav_bytes, synth_babble
from podcast_studio.clients.mock import MockBrain
from podcast_studio.voices import LANGS, STYLES, VOICE_NAMES

OR_KEY = "or-test-key-1234567890"
BD_KEY = "bd-test-key-0987654321"
BD_MT_KEY = "bd-mt-key-1122334455"
OR_KEY2 = "or-test-key-2222222222"         # a second valid OpenRouter key
OR_DEAD, OR_BROKE = "or-DEAD-key-0000000000", "or-BROKE-key-0000000000"      # rejected (401) / out of credit (402)
BD_KEY2 = "bd-test-key-2222222222"         # a second valid TTS key
BD_DEAD, BD_BROKE = "bd-DEAD-key-0000000000", "bd-BROKE-key-0000000000"


def _auth_problem(header: str | None, valid: set[str]):
    """None if the bearer token is valid, else (status, message) the way the real services answer."""
    tok = (header or "").replace("Bearer ", "", 1)
    if tok in valid:
        return None
    if "BROKE" in tok:
        return 402, "Insufficient credits"
    return 401, "Invalid API key"


class Backend:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.or_requests: list[dict] = []
        self.tts_requests: list[dict] = []
        self.mt_requests: list[dict] = []
        self.or_script: deque = deque()   # forced behaviours for the next OpenRouter calls
        self.tts_script: deque = deque()  # forced behaviours for the next TTS calls
        self.reject_json_mode = False
        self.bad_styles: set[str] = set()
        self.bad_models = {"bad/model"}
        self.split_keys = False  # True: TTS accepts only BD_KEY and translation only BD_MT_KEY (keys tied to one model)
        self.brain = MockBrain()

    def reset_counts(self) -> None:
        with self.lock:
            self.or_requests.clear(); self.tts_requests.clear(); self.mt_requests.clear()


def _make_handler(be: Backend, kind: str):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # silence
            pass

        def _send(self, code: int, body: bytes, ctype: str = "application/json", headers: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj, headers: dict | None = None):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), headers=headers)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")

        def do_GET(self):
            if kind != "or":
                return self._json(404, {"detail": "not found"})
            bad = _auth_problem(self.headers.get("Authorization"), {OR_KEY, OR_KEY2})
            if bad:
                return self._json(bad[0], {"error": {"message": "No auth credentials found", "code": bad[0]}})
            if self.path.endswith("/auth/key"):
                return self._json(200, {"data": {"label": "test", "usage": 1.5, "limit": 10, "limit_remaining": 8.5, "is_free_tier": False}})
            if self.path.endswith("/models"):
                return self._json(200, {"data": [{"id": "good/model", "name": "Good", "context_length": 1000, "pricing": {"prompt": "0.000001", "completion": "0.000002"}}]})
            self._json(404, {"detail": "not found"})

        def do_POST(self):
            body = self._body()
            if kind == "or":
                return self._openrouter(body)
            if self.path.endswith("/audio/speech"):
                return self._tts(body)
            if self.path.endswith("/chat/completions"):
                return self._mt(body)
            self._json(404, {"detail": "not found"})

        # ---------------------------------------------------------------- OpenRouter
        def _openrouter(self, body: dict):
            with be.lock:
                be.or_requests.append({"headers": dict(self.headers), "body": body, "path": self.path})
                forced = be.or_script.popleft() if be.or_script else None
            bad = _auth_problem(self.headers.get("Authorization"), {OR_KEY, OR_KEY2})
            if bad:
                return self._json(bad[0], {"error": {"message": f"Invalid key {self.headers.get('Authorization')}", "code": bad[0]}})
            if forced:
                if forced[0] == "status":
                    return self._json(forced[1], {"error": {"message": "forced failure", "code": forced[1]}}, headers={"Retry-After": "0"})
                if forced[0] == "error200":
                    return self._json(200, {"error": {"message": "Provider returned error", "code": forced[1]}})
                if forced[0] == "empty":
                    return self._json(200, {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}], "usage": {}})
                if forced[0] == "content":
                    return self._json(200, {"choices": [{"message": {"content": forced[1]}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 5, "completion_tokens": 5}})
            if body.get("model") in be.bad_models:
                return self._json(404, {"error": {"message": f"No endpoints found for {body.get('model')}", "code": 404}})
            if "response_format" in body and be.reject_json_mode:
                return self._json(400, {"error": {"message": "response_format json_object is not supported by this model", "code": 400}})
            content = be.brain.respond(body["messages"])
            self._json(200, {"choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                             "usage": {"prompt_tokens": 100, "completion_tokens": 50, "cost": 0.001}})

        # ---------------------------------------------------------------- Bodhan TTS
        def _tts(self, body: dict):
            with be.lock:
                be.tts_requests.append({"headers": dict(self.headers), "body": body})
                forced = be.tts_script.popleft() if be.tts_script else None
            bad = _auth_problem(self.headers.get("Authorization"), {BD_KEY, BD_KEY2})
            if bad:
                return self._json(bad[0], {"detail": bad[1]})
            if forced:
                if forced[0] == "status":
                    return self._json(forced[1], {"detail": "forced failure"}, headers={"Retry-After": "0"})
                if forced[0] == "notwav":
                    return self._send(200, b"this is not audio", "audio/wav")
                if forced[0] == "json200":
                    return self._json(200, {"error": "boom"})
            if body.get("model") != "indic-speak":
                return self._json(422, {"detail": f"Unknown model {body.get('model')}"})
            if not str(body.get("input", "")).strip():
                return self._json(422, {"detail": "input was empty"})
            voice = body.get("voice")
            if voice not in VOICE_NAMES:
                return self._json(422, {"detail": f"Unknown voice '{voice}'. Accepted: {', '.join(VOICE_NAMES)}"})
            instr = body.get("instructions")
            try:
                ins = json.loads(instr) if isinstance(instr, str) else None
            except json.JSONDecodeError:
                ins = None
            if not isinstance(ins, dict) or "lang" not in ins:
                return self._json(422, {"detail": "instructions had no lang"})
            if ins["lang"] not in LANGS:
                return self._json(422, {"detail": f"Unknown lang '{ins['lang']}'"})
            st = ins.get("style")
            if st is not None and (st not in STYLES or st in be.bad_styles):
                return self._json(422, {"detail": f"Unknown style '{st}'. Accepted: {', '.join(STYLES)}"})
            wav = array_to_wav_bytes(synth_babble(body["input"], voice, st))
            self._send(200, wav, "audio/wav")

        # ---------------------------------------------------------------- Bodhan MT
        def _mt(self, body: dict):
            with be.lock:
                be.mt_requests.append({"headers": dict(self.headers), "body": body})
            bad = _auth_problem(self.headers.get("Authorization"), {BD_MT_KEY} if be.split_keys else {BD_KEY, BD_KEY2, BD_MT_KEY})
            if bad:
                return self._json(bad[0], {"detail": bad[1]})
            if body.get("model") != "indic-translate" or "target_language_code" not in body:
                return self._json(422, {"detail": "bad translate request"})
            text = body["messages"][-1]["content"]
            self._json(200, {"choices": [{"message": {"role": "assistant", "content": f"«{body['target_language_code']}» {text}"}}]})

    return H


class FakeServers:
    def __init__(self) -> None:
        self.backend = Backend()
        self._servers = []
        self.or_url = self.bd_url = ""

    def __enter__(self):
        for kind in ("or", "bd"):
            srv = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self.backend, kind))
            srv.daemon_threads = True
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self._servers.append(srv)
        self.or_url = f"http://127.0.0.1:{self._servers[0].server_address[1]}/api/v1"
        self.bd_url = f"http://127.0.0.1:{self._servers[1].server_address[1]}/v1"
        return self

    def __exit__(self, *exc):
        for s in self._servers:
            s.shutdown()
            s.server_close()
