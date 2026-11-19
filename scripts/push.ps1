# TradeDesk-AI —— 推送分支到 GitHub（git 优先，失败自动切 API）
#
# 用法：
#   & ".\scripts\push.ps1"                       # 推送当前所在分支
#   & ".\scripts\push.ps1" -Branches main,agent-a,agent-b
#   & ".\scripts\push.ps1" -ApiOnly              # 强制走 GitHub API
#
# 为什么要有重试与兜底
#   本机 DNS 对 github.com 会轮询到不同节点，部分节点会把 git 流量重置
#   （Recv failure: Connection was reset）。实测同一时刻：坏节点 21s 超时、好节点 1.5s 成功。
#   因此：git push 最多重试 3 次（指数退避），仍失败则自动改走 Git Database API
#   （scripts/push_via_api.py，走 api.github.com，该通道稳定）。
#
# Token：交互式输入（不回显），只在本进程内存中使用。

param(
  [string[]]$Branches = @(),
  [string]$Owner = "",
  [string]$Repo = "",
  [switch]$ApiOnly,
  [int]$Retries = 3
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

$git = Get-GitExe
$py = Get-PythonExe

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
if (-not $Owner -or -not $Repo) {
  throw "无法确定仓库（Owner/Repo）。请用 -Owner/-Repo 指定，或先 git remote add origin <url>"
}

Write-Host "=== 推送目标：$Owner/$Repo ===" -ForegroundColor Cyan
Write-Host "    分支：$($Branches -join ', ')" -ForegroundColor Cyan

$token = $env:TRADEDESK_GITHUB_TOKEN
if (-not $token) {
  $sec = Read-Host "GitHub Token（不回显）" -AsSecureString
  $ptr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
  $token = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
  [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
}
if (-not $token) { throw "没有 Token，终止。" }

# GIT_ASKPASS：Token 不进 URL、不进 .git/config、不进命令历史
$askpass = Join-Path $env:TEMP ("askpass_td_{0}.cmd" -f (Get-Random))
$askpassBody = @(
  "@echo off",
  "echo %1 | findstr /i username >nul",
  "if errorlevel 1 (echo %TRADEDESK_TOKEN%) else (echo x-access-token)"
) -join "`r`n"
Set-Content -LiteralPath $askpass -Value $askpassBody -Encoding ASCII
$env:GIT_ASKPASS = $askpass
$env:GIT_TERMINAL_PROMPT = "0"
$env:TRADEDESK_TOKEN = $token
$ErrorActionPreference = "Continue"

$okBranches = @()
$apiUsed = $false
try {
  foreach ($b in $Branches) {
    $pushed = $false
    if (-not $ApiOnly) {
      for ($i = 1; $i -le $Retries; $i++) {
        Write-Host "  [$b] git push 第 $i/$Retries 次 ..." -ForegroundColor DarkGray
        $out = & $git -c http.sslVersion=tlsv1.2 -c http.version=HTTP/1.1 push -u origin $b 2>&1
        $code = $LASTEXITCODE
        if ($code -eq 0) {
          $out | Select-Object -Last 2 | ForEach-Object { "      $_" }
          Write-Host "    ✓ $b 推送成功" -ForegroundColor Green
          $pushed = $true; $okBranches += $b; break
        }
        $lastMsg = ($out | Select-Object -First 1)
        Write-Host "    ✗ 失败：$lastMsg" -ForegroundColor Yellow
        if ($i -lt $Retries) { Start-Sleep -Seconds (2 * $i) }
      }
    }
    if (-not $pushed) {
      Write-Host "  [$b] 改走 GitHub API 通道 ..." -ForegroundColor DarkGray
      $env:TRADEDESK_GITHUB_TOKEN = $token
      & $py (Join-Path $Root "scripts\push_via_api.py") --owner $Owner --repo $Repo --branches $b
      if ($LASTEXITCODE -eq 0) { $apiUsed = $true; $okBranches += $b }
    }
  }

  Write-Host "=== 核验远端分支 ===" -ForegroundColor Cyan
  $ls = & $git ls-remote --heads origin 2>&1
  $remote = @($ls | Where-Object { $_ -match "refs/heads/" } | ForEach-Object { ($_ -split "refs/heads/")[1] })
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
      $remoteSha = ($line -split "\s")[0]
      if ($remoteSha -eq $local) { Write-Host "  ✓ $b 远端与本地一致（$($local.Substring(0,10))）" -ForegroundColor Green }
      else { Write-Host "  ! $b 远端 $($remoteSha.Substring(0,10)) / 本地 $($local.Substring(0,10))" -ForegroundColor Yellow }
    }
  }
  if ($apiUsed) { Write-Host "  （本次部分分支走了 API 通道）" -ForegroundColor Yellow }
  exit 0
}
finally {
  Remove-Item -LiteralPath $askpass -Force -ErrorAction SilentlyContinue
  $env:TRADEDESK_TOKEN = $null
  $env:TRADEDESK_GITHUB_TOKEN = $null
  $env:GIT_ASKPASS = $null
}