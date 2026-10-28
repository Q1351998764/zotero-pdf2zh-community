@echo off
setlocal EnableExtensions
chcp 65001 >nul
where uv >nul 2>&1
if errorlevel 1 (
  echo uv was not found. Install uv and reopen the terminal.
  exit /b 1
)
rem The shared lifecycle resolves paths from this script, never from %%CD%%.
uv run --python 3.12 --with packaging "%~dp0install_managed.py" --env-tool uv %*
exit /b %errorlevel%
