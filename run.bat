@echo off
REM Cron / Task Scheduler entry point. Logs to results\run.log.
REM 1) scrape new leads  2) auto-apply the top N (config: auto_apply_after_scrape)
cd /d "%~dp0"
if not exist "results" mkdir "results"
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

"%PY%" -m jobscrape.run >> "results\run.log" 2>&1
"%PY%" -u autoapply\after_scrape.py >> "results\run.log" 2>&1
