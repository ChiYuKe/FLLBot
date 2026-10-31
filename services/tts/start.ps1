#!/usr/bin/env pwsh
# 启动 IndexTTS-2.5 本地服务
#
# 注意：wetext 依赖的 kaldifst 在 Windows 上打不开含中文的路径
# （会报 "Error opening input stream ... traditional_to_simple.fst"）。
# 如果本目录不在纯 ASCII 路径下，这里自动建一个 junction 再从那边启动。
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

if ($here -match '[^\x00-\x7F]') {
    $link = Join-Path $env:LOCALAPPDATA 'fllbot-tts'
    if (-not (Test-Path $link)) {
        cmd /c mklink /J "$link" "$here" | Out-Null
        Write-Host "已创建 ASCII 路径链接：$link -> $here"
    } elseif (-not (Test-Path (Join-Path $link 'server.py'))) {
        throw "ASCII 路径链接已存在但无效：$link，请手动删除后重试。"
    }
    $here = $link
}

$python = Join-Path $here 'vendor\IndexTTS\.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    throw "找不到 venv：$python`n先执行 services\tts\README.md 里的安装步骤。"
}
& $python (Join-Path $here 'server.py') @args
