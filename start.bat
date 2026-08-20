@echo off
chcp 65001 >nul 2>&1
title WorkTime Tracker

cd /d "%~dp0"

:: Check venv exists
if not exist ".venv\Scripts\python.exe" (
    echo [Setup] Creating virtual environment...
    py -m venv .venv
    echo [Setup] Installing dependencies...
    ".venv\Scripts\pip.exe" install -r requirements.txt -q
)

:: Launch independently without attaching the tracker to this console window.
:: The tracker remains available in the system tray; use its Quit menu item to stop it.
start "" /D "%~dp0" ".venv\Scripts\pythonw.exe" main.py
exit /b
