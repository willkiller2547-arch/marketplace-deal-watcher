@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo The app has not been installed yet.
    echo Run SETUP.bat first.
    pause
    exit /b 1
)

start "" ".venv\Scripts\pythonw.exe" app.py
