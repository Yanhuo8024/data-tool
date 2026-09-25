# data.tool —— 一键安装依赖（Windows PowerShell）
# 用法：右键「使用 PowerShell 运行」，或：
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Resolve-Python {
    $portable = Join-Path $PSScriptRoot "python-portable\python.exe"
    if (Test-Path $portable) { return $portable }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw "未找到 Python，请安装 3.10+ 或放置 python-portable\python.exe"
}

$py = Resolve-Python
Write-Host "使用 Python: $py"
& $py -m pip install --upgrade pip

if (Test-Path "requirements.lock.txt") {
    Write-Host "安装 requirements.lock.txt ..."
    & $py -m pip install -r requirements.lock.txt
    if ($LASTEXITCODE -ne 0) {
        Write-Host "lock 失败，回退 requirements.txt ..."
        & $py -m pip install -r requirements.txt
    }
} else {
    & $py -m pip install -r requirements.txt
}

if ($LASTEXITCODE -ne 0) {
    Write-Host "可换清华镜像重试:" -ForegroundColor Yellow
    Write-Host "  $py -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple"
    exit 1
}

Write-Host "`n安装完成。启动: .\start.bat 或 $py app.py" -ForegroundColor Green
Write-Host "上传 GitHub 时不要提交 python-portable/、output/、data/ 密钥。"
