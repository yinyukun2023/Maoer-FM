@echo off
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw build_exe.py --ui
) else (
    python build_exe.py --ui
)
