@echo off
:: ================================================================
:: setup_portable.bat - Prepare a portable Python runtime for USB use
:: Pure ASCII - cmd codepage safety. CRLF line endings required.
::
:: Run this ONCE on a PC with internet. It downloads the official
:: "embeddable" Python package + pip + all dependencies into
:: .\python-portable\ . Then copy the WHOLE project folder
:: (including python-portable\) to the USB drive. On any other PC,
:: just run 一键启动.bat (or start.bat) - no Python install needed.
:: ================================================================
setlocal enabledelayedexpansion
title data.tool - Portable Setup
color 0B

set "ROOT=%~dp0"
cd /d "%ROOT%"
set "PYVER=3.11.9"
set "PYDIR=%ROOT%python-portable"
set "EMBED_URL=https://www.python.org/ftp/python/%PYVER%/python-%PYVER%-embed-amd64.zip"
set "GETPIP_URL=https://bootstrap.pypa.io/get-pip.py"

echo ================================================================
echo   data.tool Portable Runtime Setup
echo   Python %PYVER% embeddable + pip + dependencies
echo ================================================================
echo.
echo Target: %PYDIR%
echo This downloads ~30MB Python + ~500MB packages. Needs internet.
echo.

where powershell >nul 2>&1
if errorlevel 1 (
    echo [ERROR] PowerShell not found - required for download.
    pause
    exit /b 1
)

if exist "%PYDIR%\python.exe" (
    echo [INFO] python-portable already exists. Skip download.
    echo        Delete the folder first if you want a clean rebuild.
    goto :setup_pip
)

:: ---------- 1. Download embeddable Python ----------
echo [1/4] Downloading Python %PYVER% embeddable package...
powershell -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%EMBED_URL%' -OutFile '%ROOT%python-embed.zip' -UseBasicParsing"
if not exist "%ROOT%python-embed.zip" (
    echo [ERROR] Download failed. Check your internet connection.
    pause
    exit /b 1
)

echo [2/4] Extracting...
powershell -NoProfile -Command "Expand-Archive -Path '%ROOT%python-embed.zip' -DestinationPath '%PYDIR%' -Force"
del "%ROOT%python-embed.zip" >nul 2>&1
if not exist "%PYDIR%\python.exe" (
    echo [ERROR] Extraction failed.
    pause
    exit /b 1
)
echo [OK] Python extracted.

:setup_pip
:: ---------- 2. Enable site-packages in the embeddable ._pth file ----------
echo [3/4] Configuring embedded Python...
:: Patch ._pth using the embedded python itself (no cmd var-escaping pitfalls):
::   - uncomment "#import site"
::   - append "Lib\site-packages" so pip-installed packages are visible
"%PYDIR%\python.exe" -c "import glob,os,sys; ps=glob.glob(os.path.join(sys.argv[1],'python*._pth')); sys.exit(1) if not ps else None; p=ps[0]; s=open(p,encoding='utf-8').read(); s=s.replace('#import site','import site'); s=s+chr(10)+'Lib'+os.sep+'site-packages'+chr(10) if 'Lib'+os.sep+'site-packages' not in s else s; open(p,'w',encoding='utf-8',newline='').write(s); print('patched:', os.path.basename(p))" "%PYDIR%"
if errorlevel 1 (
    echo [WARN] Could not patch ._pth file - pip packages may not be visible.
) else (
    echo [OK] site-packages enabled.
)

:: ---------- 3. Install pip (verify by module, not just Scripts\pip.exe) ----------
"%PYDIR%\python.exe" -m pip --version >nul 2>&1
if not errorlevel 1 (
    echo [INFO] pip already working.
    goto :install_deps
)
echo [4/4] Installing pip...
powershell -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%GETPIP_URL%' -OutFile '%PYDIR%\get-pip.py' -UseBasicParsing"
"%PYDIR%\python.exe" "%PYDIR%\get-pip.py" --no-warn-script-location -q
del "%PYDIR%\get-pip.py" >nul 2>&1
"%PYDIR%\python.exe" -m pip --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] pip installation failed.
    pause
    exit /b 1
)
echo [OK] pip installed.

:install_deps
:: ---------- 4. Install project dependencies ----------
echo.
echo ================================================================
echo   Installing project dependencies into python-portable...
echo   ^(This is the slow part - 10~30 minutes depending on network^)
echo ================================================================
echo.
"%PYDIR%\python.exe" -m pip install --upgrade pip -q --no-warn-script-location
"%PYDIR%\python.exe" -m pip install -r "%ROOT%requirements.txt" --no-warn-script-location
if errorlevel 1 (
    echo.
    echo [WARN] Some packages failed. Optional packages ^(prophet, etc.^)
    echo        are degraded gracefully - the server will still start.
    echo        Retry with:
    echo          python-portable\python.exe -m pip install -r requirements.txt
) else (
    echo.
    echo [OK] All dependencies installed.
)

:: ---------- 5. Verify the runtime can actually import the app ----------
echo.
echo [INFO] Verifying runtime...
"%PYDIR%\python.exe" -c "import flask, pandas, numpy; print('verify OK: flask, pandas, numpy importable')"
if errorlevel 1 (
    echo [WARN] Verification failed - dependencies may be incomplete.
    echo        Re-run this script, or check messages above.
) else (
    echo [OK] Runtime verified.
)
"%PYDIR%\python.exe" -m pip cache purge >nul 2>&1

:: ---------- Done ----------
echo.
echo ================================================================
echo   SETUP COMPLETE
echo ================================================================
echo.
echo   1. Copy the ENTIRE project folder (including python-portable\)
echo      to your USB drive.
echo   2. On any other Windows PC: double-click start.bat
echo      (or the launcher named with Chinese chars "yi jian" = one-click)
echo      - No Python installation needed.
echo.
echo   Notes:
echo   - python-portable\ is ~400-700MB after dependencies.
echo   - Windows SmartScreen may warn on first run from USB:
echo     click "More info" then "Run anyway".
echo   - If scripts are blocked, run once:
echo       powershell -ExecutionPolicy Bypass -Command ".\start.bat"
echo.
echo ================================================================
pause
