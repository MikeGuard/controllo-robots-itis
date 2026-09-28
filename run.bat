@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0"

echo ========================================================
echo     Universal Robots Lab Platform - Starting Up
echo ========================================================
echo Instructor Dashboard: http://localhost:8000/admin
echo.

:: Detect and activate virtual environment if present
if exist ".venv\Scripts\activate.bat" (
    call .venv\Scripts\activate.bat
    set "PYTHON_EXE=.venv\Scripts\python.exe"
) else if exist "..\.venv\Scripts\activate.bat" (
    call ..\.venv\Scripts\activate.bat
    set "PYTHON_EXE=..\.venv\Scripts\python.exe"
) else (
    set "PYTHON_EXE=python"
)

:: Verify python is reachable
"%PYTHON_EXE%" --version >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python was not found!
    echo Please install Python 3.10+ and make sure it is added to your PATH,
    echo or run setup_windows.bat first.
    pause
    exit /b 1
)

echo Share one of these URLs with your students on the LAN:
powershell -NoProfile -Command "Get-NetIPAddress -AddressFamily IPv4 2>$null | Where-Object { $_.InterfaceAlias -notmatch 'Loopback' -and $_.IPAddress -notmatch '^169\.254\.' -and $_.IPAddress -notmatch '^127\.' } | ForEach-Object { Write-Host (' -> http://' + $_.IPAddress + ':8000') }"
echo ========================================================
echo.

set "RELOAD_FLAG="
if "%1"=="--reload" set "RELOAD_FLAG=--reload"
if "%DEV_MODE%"=="1" set "RELOAD_FLAG=--reload"

%PYTHON_EXE% -m uvicorn app:app --host 0.0.0.0 --port 8000 %RELOAD_FLAG%

if errorlevel 1 (
    echo.
    echo [Platform stopped with an error]
    pause
)
