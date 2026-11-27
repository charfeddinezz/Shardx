@echo off
REM SHADOW AI - start the dashboard, or pass a goal on the command line.
setlocal
cd /d "%~dp0"
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

if "%~1"=="" goto dashboard
"%PY%" -m shadow %*
exit /b %errorlevel%

:dashboard
echo   SHADOW AI dashboard on http://127.0.0.1:40330
start "" "http://127.0.0.1:40330"
"%PY%" -m shadow serve --port 40330
