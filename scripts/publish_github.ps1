# TradeDesk-AI —— 一键创建 GitHub 仓库并推送（绕过网页的可用性检查）
#
# 用法：
#   & "D:\桌面\AIagent_project\外贸项目\scripts\publish_github.ps1" -Owner <你的GitHub用户名>
#   加 -RepoName 改名，加 -Public 建公开仓库（默认私有），加 -DryRun 只做本地自检不联网
#
# 前置：到 https://github.com/settings/tokens/new 建一个 Token（classic 勾 repo 权限即可）
# Token 只在本机内存中使用：不写进 .git/config、不进命令行历史、不进聊天记录。

param(
  [string]$Owner = "",
  [string]$RepoName = "tradedesk-ai-workbench",
  [string]$Description = "外贸业务员的 AI 数字员工工作台 —— TradeDesk-AI（PRD + 多 Agent Harness + 多语种 RAG）",
  [switch]$Public,
  [switch]$DryRun,
  [string]$Remote = "origin"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) -Parent
Set-Location $Root

function Get-GitExe {
  $cands = @(
    "C:\Users\DELL\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\git\cmd\git.exe",
    "C:\Program Files\Git\cmd\git.exe"
  )
  foreach ($c in $cands) { if (Test-Path -LiteralPath $c) { return $c } }
  $g = Get-Command git -ErrorAction SilentlyContinue
  if ($g) { return $g.Source }
  throw "找不到 git.exe"
}

$git = Get-GitExe
$vis = "私有"
if ($Public) { $vis = "公开" }

if ($DryRun) {
  Write-Host "=== DryRun 自检（不联网、不需要 Token）===" -ForegroundColor Cyan
  Write-Host "  项目根目录 : $Root"
  Write-Host "  git        : $git"
  Write-Host "  目标仓库   : $Owner/$RepoName（$vis）"
  $branches = @("main", "agent-a", "agent-b")
  foreach ($b in $branches) {
    & $git rev-parse --verify $b 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Host "  分支 $b : 存在" -ForegroundColor Green }
    else { Write-Host "  分支 $b : 缺失" -ForegroundColor Red }
  }
  $tracked = & $git ls-files
  $bad = $tracked | Where-Object { $_ -match '^\.venv/|^data/|^\.worktrees/|\.db$|^\.env$' }
  if ($bad) {
    Write-Host "  [警告] 以下文件不应入库：" -ForegroundColor Red
    $bad | ForEach-Object { Write-Host "    - $_" -ForegroundColor Red }
  } else {
    Write-Host "  入库文件检查：干净（无 .venv / data / .db / .env）" -ForegroundColor Green
  }
  Write-Host "  待推送文件数：$($tracked.Count)"
  exit 0
}

Write-Host "=== 1/5 读取 GitHub Token（输入不回显，粘贴后回车）===" -ForegroundColor Cyan
$sec = Read-Host "Personal Access Token" -AsSecureString
$ptr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
$token = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
[System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
if (-not $token) { throw "没有拿到 Token，终止。" }

$headers = @{
  "Authorization" = "Bearer $token"
  "User-Agent"    = "TradeDesk-AI-Publisher"
  "Accept"        = "application/vnd.github+json"
  "X-GitHub-Api-Version" = "2022-11-28"
}

try {
  Write-Host "=== 2/5 校验 Token ===" -ForegroundColor Cyan
  $me = Invoke-RestMethod -Uri "https://api.github.com/user" -Headers $headers -TimeoutSec 30
  Write-Host "  已登录：$($me.login)" -ForegroundColor Green
  if ($Owner -and ($Owner -ne $me.login)) {
    Write-Host "  [提示] 指定 Owner=$Owner，实际登录=$($me.login)，以登录账号为准" -ForegroundColor Yellow
  }
  $Owner = $me.login

  Write-Host "=== 3/5 创建仓库 $Owner/$RepoName（$vis）===" -ForegroundColor Cyan
  $body = @{
    name        = $RepoName
    description = $Description
    private     = (-not $Public)
    auto_init   = $false
    has_issues  = $true
    has_wiki    = $false
  } | ConvertTo-Json
  $repoUrl = "https://github.com/$Owner/$RepoName.git"
  try {
    Invoke-RestMethod -Uri "https://api.github.com/user/repos" -Method Post -Headers $headers `
      -Body ([System.Text.Encoding]::UTF8.GetBytes($body)) -ContentType "application/json; charset=utf-8" -TimeoutSec 40 | Out-Null
    Write-Host "  创建成功" -ForegroundColor Green
  } catch {
    $msg = [string]$_.ErrorDetails.Message
    if ($msg -match 'name_exists|already exists|has already been taken') {
      Write-Host "  名字被占用，自动追加日期后缀重试..." -ForegroundColor Yellow
      $RepoName = "$RepoName-$(Get-Date -Format 'MMdd')"
      $body = @{
        name = $RepoName; description = $Description; private = (-not $Public)
        auto_init = $false; has_issues = $true; has_wiki = $false
      } | ConvertTo-Json
      Invoke-RestMethod -Uri "https://api.github.com/user/repos" -Method Post -Headers $headers `
        -Body ([System.Text.Encoding]::UTF8.GetBytes($body)) -ContentType "application/json; charset=utf-8" -TimeoutSec 40 | Out-Null
      $repoUrl = "https://github.com/$Owner/$RepoName.git"
      Write-Host "  已创建 $RepoName" -ForegroundColor Green
    } else {
      throw
    }
  }

  Write-Host "=== 4/5 配置 remote 并推送 main / agent-a / agent-b ===" -ForegroundColor Cyan
  $existing = & $git remote 2>&1
  if ($existing -contains $Remote) { & $git remote set-url $Remote $repoUrl }
  else { & $git remote add $Remote $repoUrl }
  Write-Host "  remote $Remote -> $repoUrl"

  # GIT_ASKPASS 传凭据：Token 不进 URL、不进 .git/config、不进命令历史
  $askpass = Join-Path $env:TEMP ("askpass_tradedesk_{0}.cmd" -f (Get-Random))
  $askpassBody = @(
    "@echo off",
    "echo %1 | findstr /i username >nul",
    "if errorlevel 1 (echo %TRADEDESK_TOKEN%) else (echo x-access-token)"
  ) -join "`r`n"
  Set-Content -LiteralPath $askpass -Value $askpassBody -Encoding ASCII
  $env:GIT_ASKPASS = $askpass
  $env:GIT_TERMINAL_PROMPT = "0"
  $env:TRADEDESK_TOKEN = $token
  $pushed = @()
  $failed = @()
  $ErrorActionPreference = "Continue"   # 原生命令的 stderr 不要把它变成终止性错误
  try {
    foreach ($b in @("main", "agent-a", "agent-b")) {
      Write-Host "  推送 $b ..." -ForegroundColor DarkGray
      $out = & $git push -u $Remote $b 2>&1
      $code = $LASTEXITCODE
      $out | ForEach-Object { "    $_" }
      if ($code -eq 0) {
        $pushed += $b
        Write-Host "    ✓ $b 推送成功" -ForegroundColor Green
      } else {
        $failed += $b
        Write-Host "    ✗ $b 推送失败（exit=$code）" -ForegroundColor Red
      }
    }
  } finally {
    Remove-Item -LiteralPath $askpass -Force -ErrorAction SilentlyContinue
    $env:TRADEDESK_TOKEN = $null
    $env:GIT_ASKPASS = $null
  }

  Write-Host "=== 5/5 核验远端分支 ===" -ForegroundColor Cyan
  $ErrorActionPreference = "Continue"
  $ls = & $git ls-remote --heads $Remote 2>&1
  $ls | ForEach-Object { "  $_" }
  $remoteBranches = @($ls | Where-Object { $_ -match "refs/heads/" } | ForEach-Object { ($_ -split "refs/heads/")[1] })
  Write-Host "  远端实际分支：$($remoteBranches -join ', ')" -ForegroundColor Green
  $missing = @("main", "agent-a", "agent-b") | Where-Object { $remoteBranches -notcontains $_ }
  if ($missing.Count -gt 0) {
    Write-Host "  ✗ 以下分支未成功推送：$($missing -join ', ')" -ForegroundColor Red
    Write-Host "  仓库地址：https://github.com/$Owner/$RepoName" -ForegroundColor Yellow
    exit 2
  }
  Write-Host "  ✓ main / agent-a / agent-b 均已在远端" -ForegroundColor Green
  Write-Host "  仓库地址：https://github.com/$Owner/$RepoName" -ForegroundColor Green
  Write-Host "  网页可能仍打不开（网络问题），用 git 确认：" -ForegroundColor Yellow
  Write-Host "    git ls-remote $repoUrl" -ForegroundColor Yellow
}
finally {
  $token = $null
}