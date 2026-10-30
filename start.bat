@echo off
setlocal
cd /d "%~dp0"
title Podcast Studio

if not exist venv\Scripts\python.exe if not exist .venv\Scripts\python.exe (
  echo [1/4] Creating the Python environment ^(first time only^)...
  py -m venv .venv || goto :err
)
if exist .venv\Scripts\activate.bat (call .venv\Scripts\activate.bat) else (call venv\Scripts\activate.bat)

echo [2/4] Installing packages...
python -m pip install -q -r requirements.txt || goto :err

if exist .env.txt if not exist .env ren .env.txt .env
if not exist .env copy .env.example .env >nul

:keys
python -m podcast_studio.cli keys --check
if errorlevel 1 (
  echo.
  echo [3/4] Paste your keys into the file that is opening ^(Notepad^), SAVE it ^(Ctrl+S^), close Notepad, then press a key here.
  start /wait notepad .env
  pause
  goto :keys
)

echo [4/4] Checking that the keys really work...
python -m podcast_studio.cli doctor
echo.
start "" http://127.0.0.1:8000
python -m podcast_studio.cli serve
goto :eof

:err
echo.
echo Something went wrong - see the message above.
pause
