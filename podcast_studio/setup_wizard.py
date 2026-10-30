"""Guided first-time setup:  python -m podcast_studio.cli setup

Explains where each key comes from, asks for it with hidden input (nothing is echoed or logged), saves it to the local
.env file, tests it against the real service, lets you pick the models, and finishes with the full doctor check.
"""
from __future__ import annotations

import getpass
import os
from pathlib import Path
from typing import Callable

from .config import ROOT, Settings
from .doctor import is_optional
from .keys import split_keys
from .util import update_env_file

ENV_PATH = ROOT / ".env"

OPENROUTER_HELP = """
STEP 1 of 3 - OpenRouter key (the language model that writes the script)
  * This is the key from the TA e-mail "[DA7016] - Access to OpenRouter Credits".
  * It is private and tied to your e-mail address. It is only stored in the file:  {env}
  * Paste it below. You will not see it as you type/paste (that is normal), then press Enter.
  * Have several keys? Paste them separated by commas; the next one is used automatically when a key runs out.
"""
BODHAN_HELP = """
STEP 2 of 3 - Bodhan key (the voices and the translator)
  * Sign in at https://console.bodhan.ai with the SAME e-mail as the course (credits are added there).
  * Open the API keys section of the console and create a key whose model is  indic-speak  (the voices). Copy it.
  * Paste it below (hidden), then press Enter.
"""


def _ask_secret(prompt: str, ask: Callable[[str], str]) -> str:
    return (ask(prompt) or "").strip().strip('"').strip("'")


def run_wizard(settings: Settings, *, ask_secret: Callable[[str], str] = getpass.getpass, ask: Callable[[str], str] = input,
               out: Callable[[str], None] = print, env_path: Path = ENV_PATH, test: bool = True) -> int:
    """Walk through the keys one by one, save them to `.env`, optionally test them with `doctor`, and return an exit
    code. The prompt and output functions are parameters so the wizard can be tested.
    """
    from .doctor import run_doctor

    out("=" * 66)
    out(" Podcast Studio - setup")
    out("=" * 66)
    existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    updates: dict[str, str] = {}

    def have(name: str) -> bool:
        names = {name, "BODHAN_API_KEY"} if name == "BODHAN_TTS_API_KEY" else {name}  # the old single-key setting still counts
        return any(ln.split("=", 1)[0].strip() in names and ln.split("=", 1)[1].split("#")[0].strip() for ln in existing.splitlines() if "=" in ln and not ln.lstrip().startswith("#"))

    for name, label, helptext in (("OPENROUTER_API_KEY", "OpenRouter key(s)", OPENROUTER_HELP), ("BODHAN_TTS_API_KEY", "Bodhan TTS key(s)", BODHAN_HELP)):
        out(helptext.format(env=env_path))
        if have(name):
            keep = (ask(f"A {label} is already saved. Keep it? [Y/n]: ") or "y").strip().lower()
            if keep.startswith("y"):
                continue
        for _ in range(3):
            v = _ask_secret(f"{label}: ", ask_secret)
            parts = split_keys(v)
            if parts and all(len(x) >= 10 for x in parts):
                updates[name] = ",".join(parts)
                break
            out("  That does not look like a key (too short). Try again, or press Ctrl+C to stop.")
        else:
            out("  Skipping; you can run setup again any time.")

    out("""
  Optional: translation key (Bodhan key with model indic-translate). Only the 'translate' writing mode needs it;
  Hindi and the other languages work in 'direct' mode without it. Press Enter to skip.""")
    if not have("BODHAN_MT_API_KEY"):
        parts = split_keys(_ask_secret("Bodhan translation key(s) (optional, Enter to skip): ", ask_secret))
        if parts and all(len(x) >= 10 for x in parts):
            updates["BODHAN_MT_API_KEY"] = ",".join(parts)

    out("\nSTEP 3 of 3 - models")
    out("  Press Enter to accept the defaults. Defaults are a strong writer and a cheap, fast checker.")
    for name, label, cur in (("LLM_MODEL_WRITER", "Writer model", settings.model_writer), ("LLM_MODEL_CHECKER", "Fact-checker model", settings.model_checker)):
        v = (ask(f"{label} [{cur}]: ") or "").strip()
        if v:
            updates[name] = v
    mock = (ask("Use offline demo mode instead of the real services? [y/N]: ") or "n").strip().lower().startswith("y")
    updates["MOCK_APIS"] = "1" if mock else "0"

    if not env_path.exists() and (ROOT / ".env.example").exists():
        env_path.write_text((ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
    update_env_file(env_path, updates)
    out(f"\nSaved to {env_path} (keys are never printed or sent anywhere except the two services).")

    # make the new values effective for the check below without restarting
    for k, v in updates.items():
        os.environ[k] = v
    fresh = Settings.from_env(**({"mock": True} if mock else {}))
    if not test:
        return 0
    out("\nTesting everything with tiny calls...\n")
    ok = True
    for r in run_doctor(fresh):
        out(f"[{'OK ' if r['ok'] else 'FAIL'}] {r['name']}: {r['detail']}")
        if r["hint"] and not r["ok"]:
            out(f"        -> {r['hint']}")
        ok &= r["ok"] or is_optional(r)
    out("\nAll set. Start the app with:  python -m podcast_studio.cli serve" if ok else
        "\nSomething needs fixing (see FAIL lines). Fix it and run:  python -m podcast_studio.cli setup   (or just `doctor`)")
    return 0 if ok else 1
