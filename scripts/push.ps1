# TradeDesk-AI —— 推送分支到 GitHub（git 优先，失败自动切 API）
#
# 用法：
#   & ".\scripts\push.ps1"                                  # 推送当前分支
#   & ".\scripts\push.ps1" -Branches main,agent-a,agent-b   # 推送多个分支
#   & ".\scripts\push.ps1" -ApiOnly                         # 强制走 GitHub API
#
# 本机踩过的三个坑（都已在脚本里处理）
#   1. git(libcurl) 不读 Windows 系统代理 → 直连 github.com 被重置
#      → 自动探测代理端口是否在监听，在就把地址写给 git（与"系统代理"开关无关）
#   2. 凭据助手（wincred/GCM/askpass）在本机会崩或找不到 cmd，git 静默退出 128
#      → 禁用全部凭据助手，改用 GIT_CONFIG_* 传 Authorization 头（token 不上命令行）
#   3. git 的 stderr 在 PowerShell 里会变成空 ErrorRecord，看不到原因
#      → 用 cmd.exe 重定向到临时日志再读取，错误原因可见
#
# Token：交互式输入（不回显），只在本进程内存中使用。

param(
  [string[]]$Branches = @(),
  [string]$Owner = "",
  [string]$Repo = "",
  [switch]$ApiOnly,
  [int]$Retries = 2
)

$ErrorActionPreference = "Stop"
$Root = Split-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) -Parent
Set-Location $Root

function Get-GitExe {
  $c = "C:\Users\DELL\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\git\cmd\git.exe"
  if (Test-Path -LiteralPath $c) { return $c }
  $g = Get-Command git -ErrorAction SilentlyContinue
  if ($g) { return $g.Source }
  throw "找不到 git.exe"
}
function Get-PythonExe {
  $dir = $Root
  for ($i = 0; $i -lt 3; $i++) {
    $venv = Join-Path $dir ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venv) { return $venv }
    $parent = Split-Path -Parent $dir
    if (-not $parent -or $parent -eq $dir) { break }
    $dir = $parent
  }
  $f = "C:\Users\DELL\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
  if (Test-Path -LiteralPath $f) { return $f }
  throw "找不到 python.exe"
}
function Get-UsableProxy {
  $override = $env:TRADEDESK_PROXY
  if ($override) { return $override }
  try {
    $k = Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings" -ErrorAction Stop
    $server = [string]$k.ProxyServer
    if (-not $server) { return $null }
    $first = ($server -split ";")[0]
    if ($first -notmatch "://") { $first = "http://$first" }
    $hostPort = $first -replace "^[a-z]+://", ""
    if ($hostPort -match "^(?<h>[^:]+):(?<p>\d+)$") {
      $h = $Matches["h"]; $p = [int]$Matches["p"]
      if ($h -eq "127.0.0.1" -or $h -eq "localhost") {
        $listen = Get-NetTCPConnection -State Listen -LocalPort $p -ErrorAction SilentlyContinue
        if (-not $listen) { return $null }
      }
    }
    return $first
  } catch { return $null }
}

$git = Get-GitExe
$py = Get-PythonExe
$cmdExe = "$env:SystemRoot\System32\cmd.exe"

if (-not $Branches) {
  $cur = (& $git rev-parse --abbrev-ref HEAD).Trim()
  if ($cur -eq "HEAD") { throw "当前处于 detached HEAD，请用 -Branches 指定分支" }
  $Branches = @($cur)
}
if (-not $Owner -or -not $Repo) {
  $url = (& $git remote get-url origin 2>$null)
  if ($url -match "github\.com[:/](?<o>[^/]+)/(?<r>[^/\.]+)") {
    if (-not $Owner) { $Owner = $Matches["o"] }
    if (-not $Repo) { $Repo = $Matches["r"] }
  }
}
if (-not $Owner -or -not $Repo) { throw "无法确定仓库，请用 -Owner/-Repo 指定" }

Write-Host "=== 推送目标：$Owner/$Repo ===" -ForegroundColor Cyan
Write-Host "    分支：$($Branches -join ', ')" -ForegroundColor Cyan

# ---------- 代理 ----------
$proxy = Get-UsableProxy
if ($proxy) {
  # 只写进仓库级 git 配置，不设环境变量 —— 否则 Python 走代理会连不上 api.github.com（它直连是通的）
  & $git config --local http.proxy $proxy 2>$null
  Write-Host "    git 代理：$proxy（已写入本仓库 git 配置）" -ForegroundColor DarkGray
} else {
  Write-Host "    [提示] 没有可用代理，将直连（本机直连 github.com 通常不通，会自动切 API）" -ForegroundColor DarkGray
}

# ---------- Token ----------
$token = $env:TRADEDESK_GITHUB_TOKEN
if (-not $token) {
  $sec = Read-Host "GitHub Token（不回显）" -AsSecureString
  $ptr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
  $token = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
  [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
}
if (-not $token) { throw "没有 Token，终止。" }

# ---------- 认证：Authorization 头 + 禁用所有凭据助手 ----------
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("x-access-token:$token"))
$env:GIT_CONFIG_COUNT = "1"
$env:GIT_CONFIG_KEY_0 = "http.https://github.com/.extraheader"
$env:GIT_CONFIG_VALUE_0 = "Authorization: Basic $b64"
$env:GIT_TERMINAL_PROMPT = "0"
Remove-Item Env:GIT_ASKPASS -ErrorAction SilentlyContinue

$ErrorActionPreference = "Continue"
$logFile = Join-Path $env:TEMP ("td_push_{0}.log" -f (Get-Random))

function Invoke-GitCapture {
  param([string[]]$GitArgs)
  # 用 cmd.exe 重定向，确保 stderr 能被读到（PowerShell 5.1 会把原生 stderr 变成空 ErrorRecord）
  & $cmdExe /c "`"$git`" $($GitArgs -join ' ') > `"$logFile`" 2>&1"
  return $LASTEXITCODE
}

try {
  $okBranches = @()
  $apiUsed = $false
  # 代理不可用时，github.com 的 git 流量根本出不去 —— 直接走 API 通道，不做无谓重试
  $proxyOk = $false
  if ($proxy) {
    $env:http_proxy = $proxy; $env:https_proxy = $proxy
    & $cmdExe /c "`"$git`" -c http.proxy=$proxy ls-remote --exit-code https://github.com/octocat/Hello-World.git > NUL 2>&1"
    $proxyOk = ($LASTEXITCODE -eq 0)
    if (-not $proxyOk) {
      Write-Host "    [代理不可用] $proxy 连不通 —— 跳过 git 通道，改走 GitHub API" -ForegroundColor Yellow
    }
  }
  foreach ($b in $Branches) {
    $pushed = $false
    if (-not $ApiOnly -and $proxyOk) {
      for ($i = 1; $i -le $Retries; $i++) {
        Write-Host "  [$b] git push 第 $i/$Retries 次 ..." -ForegroundColor DarkGray
        $code = Invoke-GitCapture @("-c", "http.sslVersion=tlsv1.2", "-c", "credential.helper=", "push", "-u", "origin", $b)
        $log = @()
        if (Test-Path -LiteralPath $logFile) { $log = @(Get-Content -LiteralPath $logFile -ErrorAction SilentlyContinue | Where-Object { $_.Trim() -ne "" }) }
        if ($code -eq 0) {
          $log | Select-Object -Last 2 | ForEach-Object { Write-Host "      $($_.Trim())" }
          Write-Host "    ✓ $b 推送成功" -ForegroundColor Green
          $pushed = $true; $okBranches += $b; break
        }
        if ($log.Count -gt 0) { $log | Select-Object -First 3 | ForEach-Object { Write-Host "    ✗ $($_.Trim())" -ForegroundColor Yellow } }
        else { Write-Host "    ✗ 失败（无输出，exit=$code）" -ForegroundColor Yellow }
        if ($i -lt $Retries) { Start-Sleep -Seconds (2 * $i) }
      }
    }
    if (-not $pushed) {
      Write-Host "  [$b] 走 GitHub API 通道（逐个复刻提交，远端 SHA 与本地一致）..." -ForegroundColor DarkGray
      $env:TRADEDESK_GITHUB_TOKEN = $token
      & $py (Join-Path $Root "scripts\push_via_api.py") --owner $Owner --repo $Repo --branches $b
      if ($LASTEXITCODE -eq 0) { $apiUsed = $true; $okBranches += $b }
    }
  }

  Write-Host "=== 核验远端分支 ===" -ForegroundColor Cyan
  # 用 API 读远端 ref（不依赖代理）
  $remote = @()
  $ls = @()
  $pyApi = Get-PythonExe
  $env:TRADEDESK_GITHUB_TOKEN = $token
  $raw = & $pyApi (Join-Path $Root "scripts\remote_refs.py") --owner $Owner --repo $Repo
  if ($LASTEXITCODE -eq 0 -and $raw) {
    $ls = @($raw -split "`r?`n" | Where-Object { $_ -match "refs/heads/" })
    $remote = @($ls | ForEach-Object { ($_ -split "refs/heads/")[1] })
  }
  Write-Host "  远端现有分支：$($remote -join ', ')" -ForegroundColor Green
  $missing = @($Branches | Where-Object { $remote -notcontains $_ })
  if ($missing.Count -gt 0) {
    Write-Host "  ✗ 未推上去：$($missing -join ', ')" -ForegroundColor Red
    exit 2
  }
  foreach ($b in $Branches) {
    $local = (& $git rev-parse $b).Trim()
    $line = @($ls | Where-Object { $_ -match "refs/heads/$b$" })[0]
    if ($line) {
      $rs = ($line -split "\s")[0]
      if ($rs -eq $local) { Write-Host "  ✓ $b 远端与本地一致（$($local.Substring(0,10))）" -ForegroundColor Green }
      else { Write-Host "  ! $b 远端 $($rs.Substring(0,10)) / 本地 $($local.Substring(0,10))" -ForegroundColor Yellow }
    }
  }
  if ($apiUsed) { Write-Host "  （部分分支走了 API 通道）" -ForegroundColor Yellow }
  exit 0
}
finally {
  Remove-Item -LiteralPath $logFile -Force -ErrorAction SilentlyContinue
  $env:GIT_CONFIG_COUNT = $null; $env:GIT_CONFIG_KEY_0 = $null; $env:GIT_CONFIG_VALUE_0 = $null
  $env:GIT_TERMINAL_PROMPT = $null
  $env:TRADEDESK_GITHUB_TOKEN = $null
}