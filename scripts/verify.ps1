# FLLBot 本地验证：静态检查 + 前端 JS 语法门禁 + 测试
#   .\scripts\verify.ps1
# 依次执行：架构约束 → 管理 API 作用域 → 插件包 → ruff → node --check（有 Node 时）
#           → 全量 pytest；任一步失败即退出非零。
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
    Write-Host "==> architecture and plugin packages" -ForegroundColor Cyan
    & $python scripts/check_architecture.py
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $python scripts/check_admin_scope.py
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $python -m fllbot plugin check --root $root --builtin
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Write-Host "==> ruff check" -ForegroundColor Cyan
    & $python -m ruff check fllbot plugins tests scripts
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Write-Host "==> node --check (fllbot/ui/*.js)" -ForegroundColor Cyan
    $node = Get-Command node -ErrorAction SilentlyContinue
    if ($null -eq $node) {
        # Node 是可选依赖（见 README）：缺 Node 时跳过前端语法门禁，保持本地可验证
        Write-Host "未检测到 Node.js，跳过前端 JS 语法检查；架构图渲染相关测试也会因此不可用。" -ForegroundColor Yellow
    }
    else {
        $uiDir = Join-Path $root "fllbot\ui"
        $uiScripts = @(Get-ChildItem -LiteralPath $uiDir -Filter *.js -File -ErrorAction SilentlyContinue | Sort-Object Name)
        if ($uiScripts.Count -eq 0) {
            Write-Host "未找到 fllbot/ui/*.js，跳过前端 JS 语法检查。" -ForegroundColor Yellow
        }
        else {
            foreach ($uiScript in $uiScripts) {
                $relative = $uiScript.FullName.Substring($root.Length + 1)
                & node --check $uiScript.FullName
                if ($LASTEXITCODE -ne 0) {
                    Write-Host "前端 JS 语法检查失败：$relative" -ForegroundColor Red
                    exit $LASTEXITCODE
                }
            }
            Write-Host "前端 JS 语法检查通过：$($uiScripts.Count) 个文件。" -ForegroundColor Gray
        }
    }

    Write-Host "==> pytest" -ForegroundColor Cyan
    # 临时目录收在 .local 下；pytest 每次运行前会清空该基目录，不会越堆越多
    $tmpBase = Join-Path $root ".local\pytest-run"
    & $python -m pytest -q --basetemp $tmpBase
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
finally {
    Pop-Location
}

Write-Host "验证通过。" -ForegroundColor Green
