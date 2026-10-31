#!/usr/bin/env pwsh
# 启动 Qwen3-TTS VoiceDesign 服务（默认 127.0.0.1:7862）
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $here '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    throw "找不到 venv：$python"
}
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
& $python (Join-Path $here 'server.py') @args
