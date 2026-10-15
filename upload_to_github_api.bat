@echo off
cd /d "%~dp0"
set PY=python
if exist "python-portable\python.exe" set PY=python-portable\python.exe
echo.
echo If paste in console does not work, do this instead:
echo   1. Open notepad and paste your token
echo   2. Save as:  github_token.txt  in this folder
echo   3. Run this bat again
echo.
"%PY%" upload_to_github_api.py
echo.
pause
