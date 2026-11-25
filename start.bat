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
python -c "import cv2" >nul 2>nul
if errorlevel 1 (
  echo :: Installing requirements.txt ...
  python -m pip install -r requirements.txt
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
echo :: [6/6] Booting asynchronous analytics backend
python -u alert_vision.py

echo :: Alert Vision backend process terminated
echo :: Stopping static dashboard server
taskkill /f /im python.exe >nul 2>nul
pause
