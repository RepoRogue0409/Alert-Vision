@echo off
setlocal EnableDelayedExpansion
title Alert Vision - Crowd Safety Analytics Platform
cd /d "%~dp0"

set STATIC_PID_FILE=%TEMP%\alert_vision_static.pid
set HELPER_PID_FILE=%TEMP%\alert_vision_helper.pid
set PS_SCRIPT=%TEMP%\alert_vision_browser_helper.ps1

echo :: Alert Vision Crowd Safety Platform - launch sequence initiated

echo :: [1/7] Checking Python 3.11
where python >nul 2>nul
if errorlevel 1 (
  echo :: PYTHON_NOT_FOUND - install Python 3.11+ and re-run start.bat.
  pause
  exit /b 1
)
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
  echo :: PYTHON_VERSION_FAILED - Python 3.11+ is required.
  pause
  exit /b 1
)

echo :: [2/7] Ensuring virtual environment
if not exist ".venv\Scripts\python.exe" (
  echo :: Creating .venv ...
  python -m venv .venv
  if errorlevel 1 (
    echo :: VENV_FAILED - aborting startup.
    pause
    exit /b 1
  )
)
call ".venv\Scripts\activate.bat"

echo :: [3/7] Installing core requirements only when missing
python -c "import cv2, numpy, websockets, ultralytics" >nul 2>nul
if errorlevel 1 (
  echo :: Installing requirements.txt ...
  python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo :: PIP_INSTALL_FAILED - aborting startup. Retry with:
    echo ::   python -m pip install -r requirements.txt
    pause
    exit /b 1
  )
)
python -c "import cv2, numpy, websockets, ultralytics" >nul 2>nul
if errorlevel 1 (
  echo :: IMPORT_CHECK_FAILED - combined import still fails. Aborting startup.
  echo ::   python -m pip install -r requirements.txt
  pause
  exit /b 1
)

echo :: [4/7] Checking ports 8000 / 8765 / 8766 are free
for %%P in (8000 8765 8766) do (
  python -c "import socket,sys;s=socket.socket();r=s.connect_ex(('127.0.0.1',int(sys.argv[1])));s.close();raise SystemExit(1 if r==0 else 0)" %%P >nul 2>nul
  if errorlevel 1 (
    echo :: Port %%P is busy - another instance may already be running.
    pause
    exit /b 1
  )
)

echo :: [5/7] Starting whitelist static dashboard server on http://127.0.0.1:8000/
set ALERT_VISION_STATIC_PID_FILE=%STATIC_PID_FILE%
start "" /b python serve_static.py
python -c "import os,sys,time;p=sys.argv[1];deadline=time.monotonic()+5;[time.sleep(0.05) for _ in range(100) if not os.path.exists(p) and time.monotonic()<deadline];sys.exit(0 if os.path.exists(p) else 1)" "%STATIC_PID_FILE%" >nul 2>nul
if errorlevel 1 (
  echo :: STATIC_SERVER_FAILED - dashboard server did not start.
  goto :cleanup_failed
)

echo :: [6/7] Preparing hidden browser helper
echo [System.IO.File]::WriteAllText("%HELPER_PID_FILE%", [string]$PID) > "%PS_SCRIPT%"
echo $i = 0 >> "%PS_SCRIPT%"
echo while ($i -lt 90) { >> "%PS_SCRIPT%"
echo   $i++ >> "%PS_SCRIPT%"
echo   try { >> "%PS_SCRIPT%"
echo     $r = Invoke-WebRequest -Uri "http://127.0.0.1:8766/health" -UseBasicParsing -TimeoutSec 2 >> "%PS_SCRIPT%"
echo   } catch { >> "%PS_SCRIPT%"
echo     $r = $null >> "%PS_SCRIPT%"
echo   } >> "%PS_SCRIPT%"
echo   if ($r -and $r.StatusCode -eq 200) { >> "%PS_SCRIPT%"
echo     Start-Process "http://127.0.0.1:8000/" >> "%PS_SCRIPT%"
echo     exit 0 >> "%PS_SCRIPT%"
echo   } >> "%PS_SCRIPT%"
echo   Start-Sleep -Seconds 1 >> "%PS_SCRIPT%"
echo } >> "%PS_SCRIPT%"
echo Write-Host ":: TIMEOUT opening browser. Open http://127.0.0.1:8000/ manually" >> "%PS_SCRIPT%"

rem I-D1: helper runs in the BACKGROUND - the backend boots concurrently.
start "" /b powershell -NoProfile -ExecutionPolicy Bypass -File "%PS_SCRIPT%"
echo :: Waiting for backend, browser will open automatically

echo :: [7/7] Booting asynchronous analytics backend
python -u alert_vision.py
set BACKEND_EXIT=!ERRORLEVEL!
echo :: Alert Vision backend terminated (exit !BACKEND_EXIT!)

call :stop_helper
call :stop_static
if not "!BACKEND_EXIT!"=="0" pause
exit /b !BACKEND_EXIT!

:cleanup_failed
call :stop_helper
call :stop_static
pause
exit /b 1

:stop_helper
if exist "%HELPER_PID_FILE%" (
  set /p HELPER_PID=<"%HELPER_PID_FILE%"
  if defined HELPER_PID taskkill /f /pid !HELPER_PID! >nul 2>nul
  del "%HELPER_PID_FILE%" >nul 2>nul
)
if exist "%PS_SCRIPT%" del "%PS_SCRIPT%" >nul 2>nul
exit /b 0

:stop_static
if exist "%STATIC_PID_FILE%" (
  set /p STATIC_PID=<"%STATIC_PID_FILE%"
  if defined STATIC_PID taskkill /f /pid !STATIC_PID! >nul 2>nul
  del "%STATIC_PID_FILE%" >nul 2>nul
)
exit /b 0
