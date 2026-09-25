@echo off
:: ================================================================
:: data.tool Universal Launcher (portable / USB friendly)
:: Pure ASCII - cmd codepage safety.
::
:: Python resolution order:
::   1. .\python-portable\python.exe  (bundled embeddable runtime)
::   2. .\venv\Scripts\python.exe     (local venv, auto-created on first run)
::   3. system python / py launcher   (creates .\venv on first run)
:: ================================================================
setlocal enabledelayedexpansion
title data.tool
color 0B

set "PORT=5000"
set "FLASK_HOST=0.0.0.0"
set "ROOT=%~dp0"
cd /d "%ROOT%"

:: ---------- 1. Find Python ----------
set "PYEXE="

if exist "%ROOT%python-portable\python.exe" (
    set "PYEXE=%ROOT%python-portable\python.exe"
    echo [OK] Using bundled portable Python.
    goto :found_python
)

if exist "%ROOT%venv\Scripts\python.exe" (
    set "PYEXE=%ROOT%venv\Scripts\python.exe"
    echo [OK] Using local venv Python.
    goto :found_python
)

:: System python on PATH?
where python >nul 2>&1
if not errorlevel 1 (
    for /f "delims=" %%p in ('where python') do (
        if not defined PYEXE set "PYEXE=%%p"
    )
)
if defined PYEXE (
    echo [OK] Using system Python: !PYEXE!
    goto :create_venv
)

:: py launcher?
where py >nul 2>&1
if not errorlevel 1 (
    set "PYEXE=py"
    echo [OK] Using py launcher.
    goto :create_venv
)

echo.
echo [ERROR] No Python found.
echo.
echo   This PC has no Python. Choose ONE:
echo     A^) On a PC with internet, run setup_portable.bat in this folder
echo        once, then copy the whole folder (incl. python-portable\) to USB.
echo     B^) Install Python 3.11+ from https://www.python.org/downloads/
echo        (check "Add python.exe to PATH"), then run this script again.
echo.
pause
exit /b 1

:: ---------- 2. First run: create venv + install deps ----------
:create_venv
echo.
echo [INFO] First run on this machine - creating local venv...
"%PYEXE%" -m venv "%ROOT%venv" 2>nul
if not exist "%ROOT%venv\Scripts\python.exe" (
    echo [WARN] venv creation failed - will try pip --user fallback.
    set "PYEXE_FALLBACK=1"
    goto :install_deps
)
set "PYEXE=%ROOT%venv\Scripts\python.exe"
echo [OK] venv created.

:install_deps
echo [INFO] Installing dependencies (one-time, needs internet)...
"%PYEXE%" -m pip install --upgrade pip -q 2>nul
"%PYEXE%" -m pip install -r "%ROOT%requirements.txt" -q
if errorlevel 1 (
    echo [WARN] Some packages failed to install. The server may still start
    echo        with reduced features. See messages above.
) else (
    echo [OK] Dependencies ready.
)

:: ---------- 3. Free port ----------
:found_python
echo.
echo [INFO] Checking port %PORT%...
set "FOUND_PID="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /C:":!PORT! " 2^>nul ^| findstr "LISTENING"') do (
    set "FOUND_PID=%%p"
    echo [INFO] Killing stale listener PID %%p...
    taskkill /F /PID %%p >nul 2>&1
)
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -match 'watchdog_server\.py|run_server\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1
if defined FOUND_PID (
    ping -n 3 127.0.0.1 >nul
)
echo [OK] Port ready.

:: ---------- 4. LAN URL ----------
set "LAN_IP="
for /f "delims=" %%i in ('powershell -NoProfile -Command "(Get-NetIPAddress -AddressFamily IPv4 | Where-Object {$_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*'} | Select-Object -First 1).IPAddress" 2^>nul') do set "LAN_IP=%%i"

echo.
echo ================================================================
echo   data.tool - Data Collection ^& Analysis Tool
echo ================================================================
echo   Local:  http://127.0.0.1:%PORT%
if defined LAN_IP (
    echo   Wi-Fi:  http://!LAN_IP!:%PORT%
) else (
    echo   Wi-Fi:  ^(could not detect LAN IP^)
echo   Note:   Windows Firewall may ask - click "Allow access"
echo           so other devices on the same Wi-Fi can connect.
)
echo ================================================================
echo.
echo Starting server... Press Ctrl+C to stop.
echo.

start "" cmd /c "ping -n 3 127.0.0.1 >nul & start http://127.0.0.1:%PORT%/"

"%PYEXE%" -X utf8 "%ROOT%run_server.py"

echo.
echo Server stopped.
pause
