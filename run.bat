@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating Python virtual environment...
    where py >nul 2>&1
    if not errorlevel 1 (
        py -3.12 -m venv .venv
    ) else (
        python -m venv .venv
    )
    if not exist ".venv\Scripts\python.exe" (
        echo Python 3.12 is required. Install it from https://www.python.org/downloads/
        exit /b 1
    )
)

if not exist ".venv\Scripts\pip.exe" (
    ".venv\Scripts\python.exe" -m ensurepip --upgrade
)

".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 exit /b 1

".venv\Scripts\python.exe" main.py %*
endlocal
