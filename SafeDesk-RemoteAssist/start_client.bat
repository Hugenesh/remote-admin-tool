@echo off
REM SafeDesk Remote Assist - CLIENT side (the PC that gets viewed)
title SafeDesk Remote Assist - Client
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 client.py
) else (
    python client.py
)
if errorlevel 1 (
    echo.
    echo SafeDesk client exited with an error.
    echo If it says GUI libraries are missing, run install_deps.bat first.
    pause
)
