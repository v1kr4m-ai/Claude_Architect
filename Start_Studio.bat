@echo off
rem Double-click to start Architectural Studio and open the dashboard.
rem Close this window (or press Ctrl+C) to stop: the server exits and port 8777 is freed.
title Architectural Studio - close this window to stop
cd /d "%~dp0"
python -m studio_core serve --open
if errorlevel 1 (
    echo.
    echo Studio did not start. See the message above.
    pause
)
