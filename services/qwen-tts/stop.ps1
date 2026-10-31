#!/usr/bin/env pwsh
# 停止 Qwen3-TTS 服务（默认 7862）
$Port = 7862
if ($args.Count -ge 1) { $Port = [int]$args[0] }
$conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $conn) {
    Write-Host "端口 $Port 没有在监听。"
    exit 0
}
$proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
if ($proc) {
    $proc | Stop-Process -Force
    Write-Host "已停止 $($proc.ProcessName)（PID $($proc.Id)）"
}
