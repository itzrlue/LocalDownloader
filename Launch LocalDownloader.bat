@echo off
title LocalDownloader
cd /d "%~dp0"

rem If the server is already running, just open the page.
netstat -ano | findstr /r /c:":7331 .*LISTENING" >nul
if not errorlevel 1 (
    start "" http://127.0.0.1:7331
    exit /b 0
)

set "OPENFLAG=--open"
:loop
python app.py %OPENFLAG%
set "OPENFLAG="
echo.
echo Server stopped - restarting in 2 seconds. Close this window to exit.
ping -n 3 127.0.0.1 >nul
goto loop
