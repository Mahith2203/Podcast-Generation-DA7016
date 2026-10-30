#!/usr/bin/env bash
# macOS/Linux: creates the venv, installs, waits for keys in .env, checks them, starts the app.
set -e
cd "$(dirname "$0")"
[ -d .venv ] || [ -d venv ] || python3 -m venv .venv
[ -d .venv ] && . .venv/bin/activate || . venv/bin/activate
python -m pip install -q -r requirements.txt
[ -f .env ] || cp .env.example .env
until python -m podcast_studio.cli keys --check; do
  echo; echo "Paste your keys into .env (opening it now), save, then press Enter."
  ${EDITOR:-nano} .env; read -r _
done
python -m podcast_studio.cli doctor || true
python -m podcast_studio.cli serve
