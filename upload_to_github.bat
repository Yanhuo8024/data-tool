@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo   打包 GitHub 上传目录（保留完整结构）
echo ============================================
echo.

set "PY="
if exist "python-portable\python.exe" (
  set "PY=python-portable\python.exe"
) else (
  set "PY=python"
)

"%PY%" prepare_github_upload.py
if errorlevel 1 (
  echo [失败] 打包出错，请看上方错误信息。
  pause
  exit /b 1
)

echo.
echo ============================================
echo   上传步骤：
echo   1. 打开 GitHub 仓库网页
echo   2. Add file - Upload files
echo   3. 把文件夹 github_upload 里的「全部内容」拖进去
echo      （含 routes/ templates/ static/ 等子文件夹）
echo   4. Commit changes
echo.
echo   已同时生成 github_upload.zip 可作备份
echo ============================================
echo.
start "" explorer.exe "%cd%\github_upload"
pause
exit /b 0
