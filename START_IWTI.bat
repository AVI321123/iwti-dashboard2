@echo off
cd /d "%~dp0"
start "" "http://127.0.0.1:8765/iwti_dashboard.html"
py -3 iwti_server.py
if errorlevel 1 python iwti_server.py
pause
