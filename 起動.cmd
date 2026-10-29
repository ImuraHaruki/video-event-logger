@echo off
cd /d "%~dp0"
python video_event_logger.py
if errorlevel 1 (
  echo.
  echo Python 3.12 or later is required. See README.txt.
  pause
)
