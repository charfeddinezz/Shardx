@echo off
REM SHADOW AI - start the dashboard, or pass a verb and a goal.
setlocal
cd /d "%~dp0"
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

if "%~1"=="" goto dashboard
if /I "%~1"=="doctor" goto doctor
if /I "%~1"=="live" goto live
if /I "%~1"=="fleet" goto fleet
goto pass

:pass
"%PY%" -m shadow %*
exit /b %errorlevel%

:doctor
REM Is this machine ready? Checks the launcher, the token and Motion.
"%PY%" -m shadow doctor
exit /b %errorlevel%

:live
REM The only check that proves it: real profile, real page, real goal.
echo   SHADOW AI - live check against this machine
"%PY%" -m shadow doctor --live
exit /b %errorlevel%

:fleet
if "%~2"=="" (
  echo   usage: run.bat fleet "open shop.local and read the title" [profile1,profile2]
  exit /b 2
)
"%PY%" -m shadow fleet "%~2" %3 %4 %5
exit /b %errorlevel%

:dashboard
echo   SHADOW AI dashboard on http://127.0.0.1:40330
start "" "http://127.0.0.1:40330"
"%PY%" -m shadow serve --port 40330
