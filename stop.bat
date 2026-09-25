@echo off
:: NOTE: Pure ASCII only - cmd codepage safety.
title data.tool - Stop Server
setlocal enabledelayedexpansion
set "PORT=5000"

echo ================================================================
echo   data.tool - Stopping server on port %PORT%
echo ================================================================
echo.

:: Ensure we have a port; allow override: stop.bat 8080
if not "%~1"=="" set "PORT=%~1"

set "FOUND="

:: ---- Pass 1: netstat + taskkill (most reliable) ----
echo [1/3] Finding process(es) listening on TCP port %PORT%...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /C:":!PORT! " 2^>nul ^| findstr "LISTENING"') do (
    set "FOUND=1"
    echo   killing PID %%p
    taskkill /F /PID %%p >nul 2>&1
)
if "%FOUND%"=="1" (
    echo.
    echo   Waiting for port to release...
    call :_wait_release
    goto :_verify
)

:: ---- Pass 1 (alt): retry to catch TIME_WAIT leftovers ----
set "FOUND="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /C:":!PORT! " 2^>nul ^| findstr "LISTENING"') do (
    set "FOUND=1"
    echo   killing PID %%p
    taskkill /F /PID %%p >nul 2>&1
)
if "%FOUND%"=="1" (
    echo.
    echo   Waiting for port to release...
    call :_wait_release
)

:: ---- Pass 2: PowerShell Get-NetTCPConnection as backup ----
echo [2/3] Backup: PowerShell port scan...
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { netstat -ano | findstr \":%PORT%\" | findstr \"LISTENING\" >nul; taskkill /F /PID $_ 2>$null }"
if errorlevel 1 echo   (no additional process found via PowerShell)

:: ---- Pass 3: kill stray python serving the project (wmic is deprecated; use PowerShell) ----
echo [3/3] Killing leftover python processes for this project...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'run_server\.py|app\.py|start\.py|launcher\.py|watchdog_server\.py' } | ForEach-Object { Write-Host ('  killing python PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

:: ---- Verify ----
:_verify
echo.
echo ================================================================
call :_check_port
echo ================================================================
if "%PORTFREE%"=="1" (
    echo   Server is fully stopped. Port %PORT% is free.
) else (
    echo   WARNING: Port %PORT% is still in use.
    echo   A NEW server may have been started just now.
)
echo.
pause
exit /b 0

:: ---------------------------------------------------------------
:: subroutines
:: ---------------------------------------------------------------
:_wait_release
set /a "_guard=0"
:_retry
set "chk="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /C:":!PORT! " 2^>nul ^| findstr "LISTENING"') do set "chk=1"
if not defined chk (
    set "PORTFREE=1"
    exit /b
)
set /a "_guard+=1"
if !_guard! gtr 15 (
    echo   port still busy after 15 retries, giving up.
    set "PORTFREE="
    exit /b
)
ping -n 2 127.0.0.1 >nul
goto :_retry

:_check_port
set "PORTFREE=1"
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /C:":!PORT! " 2^>nul ^| findstr "LISTENING"') do (
    set "PORTFREE="
)
exit /b