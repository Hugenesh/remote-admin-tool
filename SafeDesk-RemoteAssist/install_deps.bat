@echo off
REM SafeDesk Remote Assist - install dependencies
echo ============================================
echo  SafeDesk Remote Assist - installing deps
echo ============================================
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 -m pip install --upgrade customtkinter mss pillow pynput
) else (
    python -m pip install --upgrade customtkinter mss pillow pynput
)
echo.
echo Done. You can now run start_client.bat or start_helper.bat
pause
