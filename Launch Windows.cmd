@echo off
cd /d "%~dp0"
where py >nul 2>&1
if not errorlevel 1 (
    py -3 app.py
) else (
    python app.py
)
if errorlevel 1 (
    echo.
    echo Codex Drive Sync needs Python 3.14 or newer with Tk.
    pause
)
