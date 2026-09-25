@echo off
:: ================================================================
:: 同步到U盘.bat - Sync updated project files to USB
:: Usage: 同步到U盘.bat [drive-letter]
::   e.g. 同步到U盘.bat E   ->  syncs to E:\数据采集分析工具
:: Only NEWER files are copied. python-portable\ is never re-copied.
:: ================================================================
setlocal enabledelayedexpansion
chcp 65001 >nul
title data.tool - Sync to USB
color 0B

set "SRC=%~dp0"
if "%SRC:~-1%"=="\" set "SRC=%SRC:~0,-1%"

:: ---------- Target resolution ----------
if not "%~1"=="" (
    set "DRIVE=%~1"
    goto :have_drive
)

:: Auto-detect: first removable drive
set "DRIVE="
for /f "delims=" %%d in ('powershell -NoProfile -Command "Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=2' | Select-Object -First 1 -ExpandProperty DeviceID" 2^>nul') do set "DRIVE=%%d"

if not defined DRIVE (
    echo [ERROR] No USB drive auto-detected.
    echo.
    echo Usage: 同步到U盘.bat E
    echo   where E is your USB drive letter.
    echo.
    pause
    exit /b 1
)
:have_drive
:: Normalize to bare drive letter (strip trailing colon if present)
if "%DRIVE:~-1%"==":" set "DRIVE=%DRIVE:~0,-1%"
set "DST=%DRIVE%:\数据采集分析工具"

echo ================================================================
echo   data.tool Sync: project files -^> USB
echo ================================================================
echo   From: %SRC%
echo   To:   %DST%
echo.
echo   Skipped dirs: python-portable output data downloads venv etc.
echo   Synced: __pycache__ (pre-compiled bytecode for fast USB cold start)
echo   Rule: only newer files are copied (no deletions on USB)
echo ================================================================
echo.

if not exist "%DST%" (
    echo [INFO] Target folder does not exist yet - creating...
    mkdir "%DST%" 2>nul
)

:: ---------- 0. Pre-compile bytecode ----------
:: MUST use the bundled runtime (same version as the USB side; .pyc magic must match).
:: Output goes to __pycache__, which is synced to the USB below.
:: checked-hash invalidation compares source content hashes, so it also survives
:: FAT32 timestamp truncation on the USB drive.
:: NOTE: keep comments ASCII-only here - long CJK comment runs make cmd miscompute
:: line offsets, and the tail of such a line gets executed as a command.
echo [INFO] Pre-compiling .py to __pycache__ with bundled runtime...
set "PY_PRE=%SRC%\python-portable\python.exe"
if not exist "%PY_PRE%" (
    echo [WARN] Bundled runtime not found - skipping pre-compile.
    echo        ^(A foreign interpreter would emit bytecode the USB runtime cannot load.^)
) else (
    "%PY_PRE%" -m compileall -q --invalidation-mode checked-hash -x "python-portable|__pycache__|workbuddy|mimocode|pytest_cache|downloads|venv" "%SRC%" 2>nul
    if errorlevel 1 (
        echo [WARN] compileall returned non-zero - some files may not have .pyc.
    ) else (
        echo [OK] Pre-compiled into __pycache__ ^(synced to USB in the next step^).
    )
)
echo.

:: ---------- robocopy (single line - no ^ continuation issues) ----------
:: __pycache__ MUST be synced: it is the only valid carrier of pre-compiled bytecode
:: (legacy .pyc files sitting next to .py are never loaded by Python 3).
robocopy "%SRC%" "%DST%" /E /XO /XD "python-portable" "output" "data" "downloads" ".workbuddy" ".mimocode" ".git" "venv" ".pytest_cache" ".secure" /XF "*.log" "python-embed.zip" /R:2 /W:2 /NFL /NDL /NJH /NP
set "RC=%ERRORLEVEL%"

echo.
if %RC% LSS 8 (
    echo [OK] Sync finished. robocopy code: %RC%
    echo      1-3 = files copied; 0 = already up to date
    echo.
    echo      On the USB, double-click start.bat to run.
) else (
    echo [WARN] robocopy returned %RC% - check messages above.
    echo        Common causes: USB unplugged, drive letter changed.
)
echo.

:: ---------- Runtime dependency drift check (warn only) ----------
:: python-portable\ is excluded from robocopy on purpose: it is large and changes
:: rarely. Warn when the USB copy is behind, so ModuleNotFoundError is not a
:: surprise at the next USB launch. Never auto-sync it here - that would add
:: hundreds of MB and a lot of time to every run.
:: The comparison is delegated to a Python helper: cmd's own file tests are
:: unreliable when a for-variable modifier such as %%~nx is interpolated into a
:: path that contains CJK characters - every test then reports "not found".
set "DT_TOOL=%SRC%\output\_sync_runtime_deps.py"
if not exist "%DT_TOOL%" goto :no_drift_check
if not exist "%PY_PRE%" goto :no_drift_check
"%PY_PRE%" -X utf8 "%DT_TOOL%" --check
if errorlevel 1 (
    echo        Repair command:
    echo            python output\_sync_runtime_deps.py --apply
)
goto :drift_done
:no_drift_check
echo [INFO] Drift check skipped - helper or bundled runtime not found.
:drift_done
echo.
pause
