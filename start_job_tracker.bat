@echo off
title Start Job Tracker
REM Starts ONLY the tracker UI (Flask) + the sqlite-web DB viewer.
REM No scrape, no ngrok, no LLM checks - just the dashboard.
set "ROOT=%~dp0"
set "PY=%ROOT%.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo  Restarting tracker services...
powershell -NoProfile -Command "5050,8081 | ForEach-Object { Get-NetTCPConnection -LocalPort $_ -EA SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { Stop-Process -Id $_ -Force -EA SilentlyContinue } }"
ping -n 2 127.0.0.1 >nul

powershell -NoProfile -Command "Start-Process '%PY%' -ArgumentList '-m','jobscrape.app' -WorkingDirectory '%ROOT%.' -WindowStyle Hidden"
powershell -NoProfile -Command "Start-Process '%PY%' -ArgumentList '-m','sqlite_web','--host','127.0.0.1','--port','8081','results/jobs.db' -WorkingDirectory '%ROOT%.' -WindowStyle Hidden"
ping -n 4 127.0.0.1 >nul

echo.
echo  Tracker   http://127.0.0.1:5050/admin
echo  DB view   http://127.0.0.1:8081
start "" http://127.0.0.1:5050/admin
exit /b 0
