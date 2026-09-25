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

:: ---------- 0. Python path cache (saves ~0.5-1s on every subsequent start) ----------
set "CACHE_DIR=%ROOT%.cache"
set "PY_CACHE=%CACHE_DIR%\python_path.txt"
if exist "%PY_CACHE%" (
    set /p CACHED_PY= < "%PY_CACHE%" 2>nul
    if defined CACHED_PY if exist "!CACHED_PY!" (
        set "PYEXE=!CACHED_PY!"
        echo [OK] Using cached Python: !PYEXE!
        goto :found_python
    )
)

if exist "%ROOT%python-portable\python.exe" (
    set "PYEXE=%ROOT%python-portable\python.exe"
    echo [OK] Using bundled portable Python.
    goto :cache_and_use_python
)

if exist "%ROOT%venv\Scripts\python.exe" (
    set "PYEXE=%ROOT%venv\Scripts\python.exe"
    echo [OK] Using local venv Python.
    goto :cache_and_use_python
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
:cache_and_use_python
:: (legacy label - direct python resolution path already writes cache below)
if not exist "%CACHE_DIR%" mkdir "%CACHE_DIR%" >nul 2>&1
> "%PY_CACHE%" echo !PYEXE!

:found_python
:: 任何路径最终落到这里时也写一次缓存（覆盖 create_venv / system python 路径）
if defined PYEXE if not "%PYEXE%"=="py" (
    if not exist "%CACHE_DIR%" mkdir "%CACHE_DIR%" >nul 2>&1
    > "%PY_CACHE%" echo !PYEXE!
)
echo.
echo [INFO] Checking port %PORT%...
set "FOUND_PID="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /C:":!PORT! " 2^>nul ^| findstr "LISTENING"') do (
    set "FOUND_PID=%%p"
    echo [INFO] Killing stale listener PID %%p...
    taskkill /F /PID %%p >nul 2>&1
)
:: 杀老 python 进程（watchdog_server / run_server）。用 wmic 替代 PowerShell（省 ~1s）
wmic process where "name='python.exe'" get processid,commandline /format:csv 2>nul | findstr /I "run_server.py watchdog_server.py" > "%CACHE_DIR%\old_pids.txt" 2>nul
for /f "tokens=2 delims=," %%p in ('type "%CACHE_DIR%\old_pids.txt" 2^>nul') do (
    taskkill /F /PID %%p >nul 2>&1
)
del "%CACHE_DIR%\old_pids.txt" >nul 2>&1
if defined FOUND_PID (
    ping -n 2 127.0.0.1 >nul
)
echo [OK] Port ready.

:: ---------- 4. LAN URL ----------
:: 用 ipconfig + findstr 替代 PowerShell Get-NetIPAddress（省 ~1.5s PowerShell 冷启动）
set "LAN_IP="
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /R /C:"IPv4"') do (
    call set "CANDIDATE=%%a"
    call set "CANDIDATE=%%CANDIDATE: =%%"
    if not defined LAN_IP (
        if "!CANDIDATE:~0,4!" neq "127." (
            if "!CANDIDATE:~0,8!" neq "169.254." (
                set "LAN_IP=!CANDIDATE!"
            )
        )
    )
)

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
echo (browser will open automatically when the server is ready)

REM Auto-open browser moved to run_server.py - it now waits for the port
REM to be ready instead of using a blind "ping -n 3" delay (saves 3s).
REM Override: set AUTO_OPEN_BROWSER=0 before running this script to disable.

"%PYEXE%" -X utf8 "%ROOT%run_server.py"

echo.
echo Server stopped.
pause
