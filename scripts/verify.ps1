# FLLBot 本地验证：静态检查 + 测试
#   .\scripts\verify.ps1
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "未找到虚拟环境：$python" -ForegroundColor Red
    Write-Host "请先执行：python -m venv .venv; .\.venv\Scripts\python.exe -m pip install -e `".[dev]`""
    exit 1
}

Push-Location $root
try {
    Write-Host "==> ruff check" -ForegroundColor Cyan
    & $python -m ruff check .
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Write-Host "==> pytest" -ForegroundColor Cyan
    & $python -m pytest -q
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
finally {
    Pop-Location
}

Write-Host "验证通过。" -ForegroundColor Green
