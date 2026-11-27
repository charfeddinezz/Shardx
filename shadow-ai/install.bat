@echo off
REM SHADOW AI - SHARDX AUTOPILOT ULTIMATE : Windows installer
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title SHADOW AI install

echo.
echo   SHADOW AI  -  SHARDX AUTOPILOT ULTIMATE
echo   local install, nothing leaves this computer
echo.

set "PY="
for %%C in (py python) do (
  if not defined PY (
    %%C -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3,8) else 1)" >nul 2>&1
    if not errorlevel 1 set "PY=%%C"
  )
)
if not defined PY (
  echo Python 3.8 or newer was not found.
  echo Install it from https://www.python.org/downloads/windows/ and tick
  echo "Add python.exe to PATH", then run this file again.
  echo.
  pause
  exit /b 1
)

echo   python:  %PY%
%PY% -c "import sys; print('   version:', sys.version.split()[0])"

echo   install: creating a private environment in .venv
%PY% -m venv .venv
if errorlevel 1 (
  echo   could not create .venv
  pause
  exit /b 1
)

call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip --quiet
python -m pip install --quiet -e .
if errorlevel 1 (
  echo   pip install failed
  pause
  exit /b 1
)

if not exist shadow.json (
  copy shadow.json.example shadow.json >nul
  echo   config:  shadow.json created from the example
)

echo   check:   running the self test
python run_tests.py >nul 2>&1
if errorlevel 1 (
  echo   some tests failed - run:  .venv\Scripts\python.exe run_tests.py -v
) else (
  echo   check:   ok
)

echo   check:   environment
.venv\Scripts\shadow.exe doctor

echo.
echo   installed. Start it with run.bat
echo.
pause
