@echo off
REM Launch the config UI at http://127.0.0.1:5000
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m jobscrape.app
) else (
  python -m jobscrape.app
)
