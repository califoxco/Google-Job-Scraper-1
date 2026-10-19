@echo off
REM Launch the dedicated autoapply Chrome profile with the DevTools port open.
REM This profile is separate from your daily Chrome: install Simplify and log
REM in to it ONCE, and the automation reuses that forever.
set "PROFILE=%~dp0..\.autoapply-profile"
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --user-data-dir="%PROFILE%" --remote-debugging-port=9222 --no-first-run --no-default-browser-check
