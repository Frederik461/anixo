@echo off
setlocal
cd /d "%~dp0"

rem Fast launcher: run.bat handles setup and dependency installs; this skips
rem the pip step on every normal launch.
if not exist ".venv\Scripts\python.exe" (
    call "%~dp0run.bat" %*
    exit /b %errorlevel%
)

".venv\Scripts\python.exe" main.py %*
endlocal
