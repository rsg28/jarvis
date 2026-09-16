@echo off
REM Jarvis launcher — activates the venv and runs jarvis.py
REM Usage:  launch.bat              (text mode + boot routine)
REM         launch.bat --voice      (voice mode)
REM         launch.bat --no-greet   (skip greeting/apps)

setlocal
cd /d "%~dp0"
title Jarvis

if not exist ".venv\Scripts\python.exe" (
    echo [jarvis] Virtual env not found. Creating one...
    py -m venv .venv || goto :error
    .\.venv\Scripts\python.exe -m pip install --quiet -r requirements.txt || goto :error
)

.\.venv\Scripts\python.exe jarvis.py %*
set exit_code=%errorlevel%

echo.
echo [jarvis] session ended. Press any key to close.
pause >nul
exit /b %exit_code%

:error
echo [jarvis] setup failed. Press any key to close.
pause >nul
exit /b 1
