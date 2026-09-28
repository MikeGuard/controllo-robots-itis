@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0"

echo ========================================================
echo    Universal Robots Lab Platform - Windows Setup
echo ========================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python is not installed or not in PATH!
    echo Please install Python 3.10, 3.11, or 3.12 from python.org or Microsoft Store.
    echo Ensure "Add Python to PATH" is checked during installation.
    pause
    exit /b 1
)

echo [1/4] Checking Python version...
python --version

echo.
echo [2/4] Creating virtual environment (.venv)...
if not exist ".venv" (
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
) else (
    echo Virtual environment .venv already exists.
)

echo.
echo [3/4] Upgrading pip and installing dependencies...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies.
    pause
    exit /b 1
)

echo.
echo [4/4] Purging legacy enum34 conflict...
python -m pip uninstall -y enum34 >nul 2>&1

echo.
echo ========================================================
echo Setup completed successfully!
echo To start the server, double-click "run.bat" or run:
echo     run.bat
echo ========================================================
echo.
pause
