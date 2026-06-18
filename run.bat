@echo off
REM Cron / Task Scheduler entry point. Logs to results\run.log.
cd /d "%~dp0"
if not exist "results" mkdir "results"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m jobscrape.run >> "results\run.log" 2>&1
) else (
  python -m jobscrape.run >> "results\run.log" 2>&1
)
