@echo off
setlocal
set "PYTHONUTF8=1"
cd /d "%~dp0"
where uv >nul 2>&1
if errorlevel 1 (
  echo uv is required. Install it from https://docs.astral.sh/uv/getting-started/installation/
  echo Then close this window and run start-server.bat again.
  pause
  exit /b 1
)
uv run --python 3.12 --with-requirements "%~dp0requirements.txt" "%~dp0server.py"
set "result=%errorlevel%"
if not "%result%"=="0" (
  echo Server stopped with exit code %result%. Check the message above and server/logs.
  pause
)
exit /b %result%
