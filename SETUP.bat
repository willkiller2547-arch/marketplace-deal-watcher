@echo off
title Marketplace Deal Watcher - Setup
cd /d "%~dp0"

echo ============================================
echo  Marketplace Deal Watcher v2 - Setup
echo ============================================
echo.

where py >nul 2>nul
if errorlevel 1 (
    echo Python was not found.
    echo Install Python 3 from python.org, then run this file again.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating Python environment...
    py -m venv .venv
    if errorlevel 1 goto :error
)

echo Installing Python packages...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :error

".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :error

echo Installing Chromium for Playwright...
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 goto :error

echo.
echo Setup complete.
echo Double-click START.bat to open the app.
pause
exit /b 0

:error
echo.
echo Setup failed. Copy the error above and send it to ChatGPT.
pause
exit /b 1
