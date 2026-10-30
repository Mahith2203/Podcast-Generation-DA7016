# Contributing

Thanks for taking a look. This is a small project, so the rules are short.

## Set up

```bash
git clone https://github.com/YOUR-USERNAME/podcast-studio.git
cd podcast-studio
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
```

You do **not** need any API keys to develop: the test-suite and `--mock` demo mode run entirely offline.

```bash
python -m podcast_studio.cli --mock serve      # the whole app with fake models and beep voices
```

## Before you open a pull request

```bash
ruff check .                                   # must be clean (config in pyproject.toml)
pytest --ignore=tests/test_video.py            # fast tests; the same command runs in CI
pytest tests/test_video.py                     # only if you touched podcast_studio/video/ (needs Chromium and ffmpeg)
python scripts/export_docs.py                  # only if you changed models.py, an API route or its docstring
```

- **Add a test** for behaviour you add or change. Look at `tests/test_pipeline.py` (whole flows) and `tests/test_text_audio.py` (small pure functions) for the style.
- **Write a docstring** for every new module, class and function: say what it does and what it returns or raises, not how the lines work.
- **Keep the safeguards.** Anything a model writes that a listener will hear or read must pass the number rule and the fact-checker (or be clearly marked as not checked). See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#the-one-rule-everything-is-built-around).
- **Network access lives in `podcast_studio/clients/` only.** Everything else talks to the `LLMBase` / `MockBodhan`-style interfaces, so it can be tested offline.
- **Never commit** `.env`, `data/`, `tuning.json`, API keys or generated audio. They are git-ignored; check `git status` before committing.

## Rebuilding the 3D hosts

The browser bundle `static/stage3d.js` is generated from `web3d/src/stage3d.js` and committed, so nobody needs Node.js just to run the app. If you change the source:

```bash
cd web3d
npm ci
npm run build        # writes ../static/stage3d.js (esbuild, target es2019)
```

Commit both the source and the rebuilt bundle.

## Project layout

See the [codebase overview in the README](README.md#codebase-overview). A good first read is `podcast_studio/models.py`, then `pipeline/orchestrator.py` and `pipeline/writer.py`.

## Reporting a problem

Open an issue with what you did, what you expected and what happened. Paste error text, but **never your keys** (the app redacts them in errors, but check before you paste).
