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

:: Launch
".venv\Scripts\python.exe" main.py
