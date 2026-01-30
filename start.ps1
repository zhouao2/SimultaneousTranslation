<#
.SYNOPSIS
  一键启动同声传译服务。

.DESCRIPTION
  - 启动服务（默认：python .\start_server.py --https）

  如果在其它电脑上提示"无法执行脚本/被系统阻止"，常见原因：
  - 脚本带下载标记（Zone.Identifier），需要解除阻止（Unblock）
  - ExecutionPolicy 限制（建议对当前用户放开到 RemoteSigned）

.PARAMETER Port
  服务端口（默认：15677）

.PARAMETER Http
  使用 HTTP 启动（默认使用 HTTPS）

.EXAMPLE
  .\start.ps1

.EXAMPLE
  .\start.ps1 -Port 15677

.EXAMPLE
  .\start.ps1 -Http
#>

[CmdletBinding()]
param(
  [int]$Port = 15677,
  [switch]$Http
)

$ErrorActionPreference = "Stop"

function Write-Info([string]$msg) { Write-Host "[start.ps1] $msg" -ForegroundColor Cyan }
function Write-Warn([string]$msg) { Write-Host "[start.ps1] $msg" -ForegroundColor Yellow }
function Write-Err([string]$msg)  { Write-Host "[start.ps1] $msg" -ForegroundColor Red }

try {
  # --- Encoding and Policy Compatibility ---
  $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
  $OutputEncoding = $utf8NoBom
  [Console]::OutputEncoding = $utf8NoBom

  # Force Python to use UTF-8 output
  $env:PYTHONUTF8 = "1"
  $env:PYTHONIOENCODING = "utf-8"

  # Switch to UTF-8 codepage on older Windows PowerShell
  if ($IsWindows) {
    try { cmd /c "chcp 65001 >nul" | Out-Null } catch { }
  }

  # Unblock file if it has Zone.Identifier
  try {
    if ($IsWindows -and $PSCommandPath) {
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

  # Start service
  $argsList = @(".\start_server.py", "--port", "$Port")
  if (-not $Http) { $argsList += "--https" }

  if ($Http) {
    Write-Warn "Starting with HTTP (mobile devices usually require HTTPS)"
  } else {
    Write-Info "Starting with HTTPS"
  }

  Write-Info ("Start command: python " + ($argsList -join " "))
  python @argsList
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
