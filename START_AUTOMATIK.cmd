@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
    py -3 start.py
) else (
    python start.py
)
if errorlevel 1 (
    echo.
    echo Python 3.10 oder neuer wird benoetigt. Details siehe LIESMICH.md.
    pause
)
