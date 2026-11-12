@echo off
setlocal
cd /d "%~dp0"
where pythonw.exe >nul 2>nul
if not errorlevel 1 (
    start "" pythonw.exe "%~dp0brake_calculator.py"
    exit /b 0
)
where pyw.exe >nul 2>nul
if not errorlevel 1 (
    start "" pyw.exe -3 "%~dp0brake_calculator.py"
    exit /b 0
)
where python.exe >nul 2>nul
if not errorlevel 1 (
    python.exe "%~dp0brake_calculator.py"
    if errorlevel 1 pause
    exit /b
)
echo Python 3.10 or later with Tkinter is required.
echo Install Python from python.org, then run this launcher again.
pause
exit /b 1
