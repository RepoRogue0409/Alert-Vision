@echo off
title Alert Vision - Crowd Safety Analytics Platform
cd /d "%~dp0"

echo :: Alert Vision Crowd Safety Platform - launch sequence initiated
echo :: Mounting dashboard context inside lightweight python http daemon
start /min /b python -m http.server 8000
echo :: Dashboard endpoint active at http://localhost:8000

echo :: Opening authorized dashboard in default browser
explorer "http://localhost:8000"

echo :: Booting asynchronous analytics backend
python -u alert_vision.py

echo :: Alert Vision backend process terminated
pause
