@echo off
REM Double-click this file to start the Tonebridge server (Windows).
cd /d "%~dp0backend"
echo Installing what Tonebridge needs (first time only)...
python -m pip install -r requirements.txt
echo.
python server.py
pause
