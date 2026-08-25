<#
.SYNOPSIS
  一键启动同声传译服务（Windows）。

.DESCRIPTION
  - 自动选择 Python 解释器：优先项目自带 .doubao / .venv 虚拟环境（依赖已装好且版本匹配），
    找不到时回退系统 python
  - 默认端口从 config/config.json 的 server.port 读取，可用 -Port 覆盖
  - 端口被占用时自动关闭占用进程后启动（-KeepPort 可关闭该行为）

  如果在其它电脑上提示"无法执行脚本/被系统阻止"，常见原因：
  - 脚本带下载标记（Zone.Identifier），需要解除阻止（Unblock）
  - ExecutionPolicy 限制（建议对当前用户放开到 RemoteSigned）

.PARAMETER Port
  服务端口（默认：读取 config/config.json 的 server.port，读取失败用 15677）

.PARAMETER Http
  使用 HTTP 启动（默认使用 HTTPS）

.PARAMETER Install
  启动前用选定的解释器安装 requirements.txt

.PARAMETER KeepPort
  端口被占用时不自动清理，直接报错退出（默认自动关闭占用端口的进程）

.EXAMPLE
  .\start.ps1

.EXAMPLE
  .\start.ps1 -Port 15677

.EXAMPLE
  .\start.ps1 -Http -Install
#>

[CmdletBinding()]
param(
  [int]$Port = 0,          # 0 = 从 config/config.json 读取
  [switch]$Http,
  [switch]$Install,
  [switch]$KeepPort
)

$ErrorActionPreference = "Stop"

# Windows 判定（$IsWindows 仅 PowerShell 7+ 存在，5.1 需用环境变量兜底）
$OnWindows = ($env:OS -eq "Windows_NT")

function Write-Info([string]$msg) { Write-Host "[start.ps1] $msg" -ForegroundColor Cyan }
function Write-Warn([string]$msg) { Write-Host "[start.ps1] $msg" -ForegroundColor Yellow }
function Write-Err([string]$msg)  { Write-Host "[start.ps1] $msg" -ForegroundColor Red }

# 返回占用指定端口的 LISTEN 进程 PID 列表（只匹配监听状态，不误杀客户端连接）
function Get-PortListeners([int]$port) {
  try {
    # Windows 8+ / Server 2012+ 内置
    $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($conns) { return @($conns | Select-Object -ExpandProperty OwningProcess -Unique) }
  } catch { }
  try {
    # 兜底：netstat 解析（- ano 输出最后一列为 PID）
    $lines = netstat -ano -p tcp | Select-String ":$port\s.*LISTENING"
    $pids = @()
    foreach ($line in $lines) {
      $parts = ($line.ToString() -split '\s+') | Where-Object { $_ }
      $p = [int]$parts[-1]
      if ($p -gt 0) { $pids += $p }
    }
    return @($pids | Select-Object -Unique)
  } catch { return @() }
}

function Clear-Port([int]$port) {
  $listeners = Get-PortListeners $port
  if (-not $listeners -or $listeners.Count -eq 0) { return }

  if ($KeepPort) {
    throw "端口 $port 已被占用（PID: $($listeners -join ', ')），且指定了 -KeepPort 不自动清理"
  }

  Write-Warn "端口 $port 被占用（PID: $($listeners -join ', ')），自动关闭..."
  foreach ($procId in $listeners) {
    try { Stop-Process -Id $procId -ErrorAction Stop } catch { }
  }
  # 最多等 15 秒优雅退出，超时强杀
  for ($i = 0; $i -lt 15; $i++) {
    if (-not (Get-PortListeners $port)) { break }
    Start-Sleep -Seconds 1
  }
  $remaining = Get-PortListeners $port
  if ($remaining -and $remaining.Count -gt 0) {
    Write-Warn "优雅关闭超时，强制结束（taskkill /F）..."
    foreach ($procId in $remaining) {
      try { taskkill /F /PID $procId | Out-Null } catch { }
    }
    Start-Sleep -Seconds 1
  }
  # 再兜底等端口完全释放
  for ($i = 0; $i -lt 10; $i++) {
    if (-not (Get-PortListeners $port)) { break }
    Start-Sleep -Seconds 1
  }
  Write-Info "端口 $port 已释放，继续启动"
}

try {
  # --- Encoding and Policy Compatibility ---
  $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
  $OutputEncoding = $utf8NoBom
  [Console]::OutputEncoding = $utf8NoBom

  # Force Python to use UTF-8 output
  $env:PYTHONUTF8 = "1"
  $env:PYTHONIOENCODING = "utf-8"

  # Switch to UTF-8 codepage on older Windows PowerShell
  if ($OnWindows) {
    try { cmd /c "chcp 65001 >nul" | Out-Null } catch { }
  }

  # Unblock file if it has Zone.Identifier
  try {
    if ($OnWindows -and $PSCommandPath) {
      $zone = Get-Item -LiteralPath $PSCommandPath -Stream Zone.Identifier -ErrorAction SilentlyContinue
      if ($zone) {
        Write-Warn "Detected Zone.Identifier on script, attempting to unblock..."
        try { Unblock-File -LiteralPath $PSCommandPath -ErrorAction Stop } catch { }
      }
    }
  } catch { }

  $projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
  Set-Location $projectRoot

  if (-not (Test-Path ".\start_server.py")) {
    throw "start_server.py not found (please run from project root)"
  }
  if (-not (Test-Path ".\config\config.json")) {
    throw "config/config.json 不存在！请复制 config.example.json 为 config.json 并填入配置"
  }

  # --- 选择 Python 解释器（Windows venv 布局是 Scripts\python.exe）---
  $Python = $null
  foreach ($candidate in @(".\.doubao\Scripts\python.exe", ".\.venv\Scripts\python.exe")) {
    if (Test-Path $candidate) { $Python = $candidate; break }
  }
  if (-not $Python) {
    if (Get-Command python -ErrorAction SilentlyContinue) {
      $Python = "python"
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
      $Python = "py"
    } else {
      throw "未找到 Python：请安装 Python 3.11+ 或创建虚拟环境（.doubao / .venv）"
    }
  }
  Write-Info "使用解释器：$Python"

  # --- 可选：安装依赖 ---
  if ($Install) {
    Write-Info "安装 Python 依赖..."
    & $Python -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "依赖安装失败（exit $LASTEXITCODE）" }
  }

  # --- 端口：-Port 优先，其次读 config/config.json，否则 15677 ---
  if ($Port -le 0) {
    $code = @'
import json
try:
    with open("config/config.json", "r", encoding="utf-8") as f:
        cfg = json.load(f)
    print(int(cfg.get("server", {}).get("port", 15677)))
except Exception:
    print(15677)
'@
    try {
      $Port = [int](& $Python -c $code | Select-Object -First 1)
    } catch { $Port = 15677 }
    if ($Port -le 0) { $Port = 15677 }
  }

  # --- 端口占用自动清理 ---
  if ($OnWindows) {
    Clear-Port $Port
  }

  # --- Start service ---
  $argsList = @(".\start_server.py", "--port", "$Port")
  if (-not $Http) { $argsList += "--https" }

  if ($Http) {
    Write-Warn "Starting with HTTP (mobile devices usually require HTTPS)"
  } else {
    Write-Info "Starting with HTTPS"
  }

  Write-Info ("Start command: $Python " + ($argsList -join " "))
  & $Python @argsList
  exit $LASTEXITCODE
}
catch {
  Write-Err $_.Exception.Message
  Write-Err "Startup failed. You can try:"
  Write-Host "  1) Right-click file -> Properties -> Unblock (if available)" -ForegroundColor Gray
  Write-Host "  2) Run: pwsh -ExecutionPolicy Bypass -File .\start.ps1" -ForegroundColor Gray
  Write-Host "  3) Set execution policy (recommended):" -ForegroundColor Gray
  Write-Host "     - Set-ExecutionPolicy -Scope CurrentUser RemoteSigned" -ForegroundColor Gray
  Write-Host "     - Or Set-ExecutionPolicy -Scope CurrentUser Bypass" -ForegroundColor Gray
  exit 1
}
