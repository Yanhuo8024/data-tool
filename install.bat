@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

echo ============================================
echo   data.tool  一键安装依赖
echo ============================================
echo.

rem ---- 选择解释器：优先本机 Python，有 python-portable 则装进便携环境 ----
set "PY="
if exist "python-portable\python.exe" (
  set "PY=python-portable\python.exe"
  echo [1/3] 使用便携 Python: %PY%
) else (
  where python >nul 2>&1
  if not errorlevel 1 (
    for /f "delims=" %%I in ('where python') do (
      if not defined PY set "PY=%%I"
    )
    echo [1/3] 使用系统 Python: %PY%
  ) else (
    echo [错误] 未找到 Python。请安装 Python 3.10+ 或把 python-portable\python.exe 放在本目录。
    pause
    exit /b 1
  )
)

"%PY%" -m pip --version >nul 2>&1
if errorlevel 1 (
  echo [提示] 当前解释器没有 pip，尝试 ensurepip ...
  "%PY%" -m ensurepip --upgrade
)

echo.
echo [2/3] 升级 pip（可选）...
"%PY%" -m pip install --upgrade pip

echo.
echo [3/3] 安装依赖清单 requirements.txt ...
if exist "requirements.lock.txt" (
  echo      （若 lock 安装失败，自动回退 requirements.txt）
  "%PY%" -m pip install -r requirements.lock.txt
  if errorlevel 1 (
    echo [回退] 使用 requirements.txt ...
    "%PY%" -m pip install -r requirements.txt
  )
) else (
  "%PY%" -m pip install -r requirements.txt
)

if errorlevel 1 (
  echo.
  echo ============================================
  echo   安装失败。可尝试：
  echo   1) 切换网络/镜像:  %PY% -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
  echo   2) 使用官方源:     %PY% -m pip install -r requirements.txt -i https://pypi.org/simple
  echo ============================================
  pause
  exit /b 1
)

echo.
echo ============================================
echo   安装完成。启动方式：
echo     一键启动.bat
echo     或 %PY% app.py
echo.
echo   提示：
echo   - 上传 GitHub 请勿提交 python-portable/、output/、data/ 密钥
echo   - 本脚本只安装依赖，不会改动你的业务代码或已有数据
echo ============================================
pause
exit /b 0
