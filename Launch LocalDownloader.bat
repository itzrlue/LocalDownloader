@echo off
title LocalDownloader
cd /d "%~dp0"
start "" /min cmd /c "python app.py"
timeout /t 2 /nobreak >nul
start "" "http://127.0.0.1:7331"
exit /b 0
