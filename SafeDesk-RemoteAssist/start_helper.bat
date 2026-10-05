@echo off
REM SafeDesk Remote Assist - HELPER side (the support / admin PC)
title SafeDesk Remote Assist - Helper
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 helper.py
) else (
    python helper.py
)
if errorlevel 1 (
    echo.
    echo SafeDesk helper exited with an error.
    echo If it says GUI libraries are missing, run install_deps.bat first.
    pause
)
