@echo off
setlocal EnableExtensions
chcp 65001 >nul
where conda >nul 2>&1
if errorlevel 1 (
  echo Conda was not found. Open an Anaconda/Miniconda prompt and retry.
  exit /b 1
)
rem Conda's base Python already provides packaging; no base packages are changed.
call conda run --no-capture-output -n base python "%~dp0install_managed.py" --env-tool conda %*
exit /b %errorlevel%
