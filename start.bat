@echo off
title Alert Vision - Crowd Safety Analytics Platform
cd /d "%~dp0"

echo :: Alert Vision Crowd Safety Platform - launch sequence initiated
echo :: [1/6] Checking Python 3.11
where python >nul 2>nul
if errorlevel 1 (
  echo :: Python interpreter not found on PATH. Install Python 3.11 and re-run start.bat.
  pause
  exit /b 1
)
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
  echo :: Python 3.11+ is required. Current interpreter is older.
  pause
  exit /b 1
)

echo :: [2/6] Ensuring virtual environment
if not exist ".venv\Scripts\python.exe" (
  echo :: Creating .venv ...
  python -m venv .venv
)
call ".venv\Scripts\activate.bat"

echo :: [3/6] Installing core requirements only when missing
python -c "import cv2, numpy, websockets, ultralytics" >nul 2>nul
if errorlevel 1 (
  echo :: Installing requirements.txt ...
  python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo :: PIP_INSTALL_FAILED - aborting startup. See package error above. Retry with:
    echo ::   python -m pip install -r requirements.txt
    taskkill /f /im python.exe >nul 2>nul
    pause
    exit /b 1
  )
)
python -c "import cv2, numpy, websockets, ultralytics" >nul 2>nul
if errorlevel 1 (
  echo :: IMPORT_CHECK_FAILED - aborting startup. Install with:
  echo ::   python -m pip install -r requirements.txt
  taskkill /f /im python.exe >nul 2>nul
  pause
  exit /b 1
)

echo :: [4/6] Checking ports 8000 / 8765 / 8766 are free
python -c "import socket;s=socket.socket();r=s.connect_ex(('127.0.0.1',8765));s.close();raise SystemExit(1 if r==0 else 0)" >nul 2>nul
if errorlevel 1 (
  echo :: Port 8765 busy - another instance may already be running.
  pause
  exit /b 1
)
python -c "import socket;s=socket.socket();r=s.connect_ex(('127.0.0.1',8766));s.close();raise SystemExit(1 if r==0 else 0)" >nul 2>nul
if errorlevel 1 (
  echo :: Port 8766 busy - another instance may already be running.
  pause
  exit /b 1
)

echo :: [5/6] Starting whitelist static dashboard server on http://127.0.0.1:8000/
start /min /b python serve_static.py

echo :: [6/6] Launching browser-helper & booting backend
:: -- Launch hidden background PowerShell helper that polls backend health
::    and opens the dashboard browser on first success.
set PS_SCRIPT=%TEMP%\av_browser_helper.ps1
echo $i = 0 > "%PS_SCRIPT%"
echo :retry >> "%PS_SCRIPT%"
echo $i = $i + 1 >> "%PS_SCRIPT%"
echo if $i -ge 90 (>> "%PS_SCRIPT%"
echo :: Timeout: print manual URL and exit >> "%PS_SCRIPT%"
echo echo :: Timeout opening browser. Open http://127.0.0.1:8000/ manually >> "%PS_SCRIPT%"
echo exit 1 >> "%PS_SCRIPT%"
echo ) >> "%PS_SCRIPT%"
echo $r = Invoke-WebRequest http://127.0.0.1:8766/health -UseBasicParsing >> "%PS_SCRIPT%"
echo if ($r.StatusCode -eq 200) { >> "%PS_SCRIPT%"
echo :: On success, open dashboard and exit >> "%PS_SCRIPT%"
echo powershell -ExecutionPolicy Bypass -Command "Start-Process http://127.0.0.1:8000" >> "%PS_SCRIPT%"
echo exit 0 >> "%PS_SCRIPT%"
echo } else { >> "%PS_SCRIPT%"
echo :: Wait 1 second then retry >> "%PS_SCRIPT%"
echo Start-Sleep -Seconds 1 >> "%PS_SCRIPT%"
echo goto retry >> "%PS_SCRIPT%"
echo } >> "%PS_SCRIPT%"
powershell -ExecutionPolicy Bypass -File "%PS_SCRIPT%" >nul 2>&1
del "%PS_SCRIPT%"

echo :: Waiting for backend, browser will open automatically

echo :: [6/6] Booting asynchronous analytics backend
python -u alert_vision.py

echo :: Alert Vision backend process terminated

:: Cleanup any leftover PowerShell process
taskkill /f /im powershell.exe >nul 2>nul

echo :: Stopping static dashboard server

pause