@echo off
rem ANIXO OS-level uninstall wrapper.
rem Replay the data paths recorded at install time before running the guarded
rem uninstaller, so custom ANIXO_* overrides also work from Start Menu / Apps.
setlocal
set "ROOT=%~dp0"
set "INI=%ROOT%uninstall-paths.ini"
if exist "%INI%" (
    for /f "usebackq tokens=1,* delims==" %%A in ("%INI%") do (
        if /I "%%A"=="Data" set "ANIXO_DATA_DIR=%%B"
        if /I "%%A"=="Cache" set "ANIXO_CACHE_DIR=%%B"
        if /I "%%A"=="Log" set "ANIXO_LOG_DIR=%%B"
    )
    rem Delete the generated INI now so Inno can remove an empty {app} folder.
    del /f /q "%INI%" >nul 2>&1
)
"%ROOT%ANIXO-Uninstaller.exe" --mode app-only --yes --exclude app --exclude settings %*
exit /b %ERRORLEVEL%
