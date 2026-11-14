# 清理测试临时目录：.local\tmp-tests 下的历史残留
#   .\scripts\clean_test_tmp.ps1 -List          # 只看有哪些、占多大，不删除
#   .\scripts\clean_test_tmp.ps1                # 删除 3 天前的
#   .\scripts\clean_test_tmp.ps1 -Days 0        # 删除全部
param(
    [int]$Days = 3,
    [switch]$List
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$store = Join-Path $root ".local\tmp-tests"

if (-not (Test-Path -LiteralPath $store)) {
    Write-Host "没有需要清理的目录：$store" -ForegroundColor Green
    exit 0
}

$cutoff = (Get-Date).AddDays(-$Days)
$all = Get-ChildItem -LiteralPath $store -Force -Directory
$stale = $all | Where-Object { $_.LastWriteTime -lt $cutoff }

function Get-DirSize([System.IO.DirectoryInfo]$dir) {
    $sum = (Get-ChildItem -LiteralPath $dir.FullName -Recurse -Force -File -ErrorAction SilentlyContinue |
        Measure-Object -Property Length -Sum).Sum
    if ($null -eq $sum) { 0 } else { $sum }
}

if ($List) {
    Write-Host "共 $($all.Count) 个目录，位于 $store" -ForegroundColor Cyan
    $total = 0
    $rows = foreach ($d in $all) {
        $size = Get-DirSize $d
        $total += $size
        [pscustomobject]@{
            名称     = $d.Name
            修改时间 = $d.LastWriteTime.ToString("yyyy-MM-dd HH:mm")
            大小MB   = [math]::Round($size / 1MB, 1)
        }
    }
    $rows | Sort-Object 修改时间 -Descending | Format-Table -AutoSize
    Write-Host ("合计 {0} MB" -f [math]::Round($total / 1MB, 1)) -ForegroundColor Cyan
    exit 0
}

if (-not $stale) {
    Write-Host "没有超过 $Days 天的临时目录，无需清理。" -ForegroundColor Green
    exit 0
}

$freed = 0
$removed = 0
$failed = @()
foreach ($d in $stale) {
    $size = Get-DirSize $d
    try {
        Remove-Item -LiteralPath $d.FullName -Recurse -Force -ErrorAction Stop
        $freed += $size
        $removed++
    }
    catch {
        $failed += $d.Name
    }
}

Write-Host ("已删除 {0} 个目录，释放 {1} MB" -f $removed, [math]::Round($freed / 1MB, 1)) -ForegroundColor Green
if ($failed.Count -gt 0) {
    Write-Host ("{0} 个目录被占用或无法删除（多为正在运行的测试/服务占用）：" -f $failed.Count) -ForegroundColor Yellow
    $failed | Select-Object -First 10 | ForEach-Object { Write-Host "  $_" -ForegroundColor Yellow }
}
