# TradeDesk-AI —— 一键创建 GitHub 仓库并推送
#
# 用法：
#   & "D:\桌面\AIagent_project\外贸项目\scripts\publish_github.ps1" -DryRun     # 本地自检，不联网不要 Token
#   & "D:\桌面\AIagent_project\外贸项目\scripts\publish_github.ps1"              # 建仓库（已存在则跳过）并推送
#   & "D:\桌面\AIagent_project\外贸项目\scripts\publish_github.ps1" -Public      # 建公开仓库
#
# 为什么不用 git push
#   本机到 github.com:443 的 git 流量会被重置（Recv failure: Connection was reset），
#   但 api.github.com 稳定可用 → 改用 Git Database API 推送（scripts/push_via_api.py），
#   内容一致时远端 SHA 与本地完全相同。
#
# Token 从交互式输入读取（不回显），只在本进程内存中使用：不写 .git/config、不进命令历史。

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
    "D:\Git\Git\cmd\git.exe",
    "C:\Program Files\Git\cmd\git.exe"
  )
  foreach ($c in $cands) { if (Test-Path -LiteralPath $c) { return $c } }
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
  $fallback = "C:\Users\DELL\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
  if (Test-Path -LiteralPath $fallback) { return $fallback }
  throw "找不到 python.exe"
}

$git = Get-GitExe
$vis = "私有"
if ($Public) { $vis = "公开" }

# ---------------------------------------------------------------- DryRun
if ($DryRun) {
  Write-Host "=== DryRun 自检（不联网、不需要 Token）===" -ForegroundColor Cyan
  Write-Host "  项目根目录 : $Root"
  Write-Host "  git        : $git"
  Write-Host "  python     : $(Get-PythonExe)"
  Write-Host "  目标仓库   : $Owner/$RepoName（$vis）"
  foreach ($b in @("main", "agent-a", "agent-b")) {
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

# ---------------------------------------------------------------- 主流程
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

  Write-Host "=== 3/5 准备仓库 $Owner/$RepoName（$vis）===" -ForegroundColor Cyan
  $repoUrl = "https://github.com/$Owner/$RepoName.git"
  $body = @{
    name = $RepoName; description = $Description; private = (-not $Public)
    auto_init = $false; has_issues = $true; has_wiki = $false
  } | ConvertTo-Json
  $created = $false
  try {
    Invoke-RestMethod -Uri "https://api.github.com/user/repos" -Method Post -Headers $headers `
      -Body ([System.Text.Encoding]::UTF8.GetBytes($body)) -ContentType "application/json; charset=utf-8" -TimeoutSec 40 | Out-Null
    $created = $true
    Write-Host "  创建成功" -ForegroundColor Green
  } catch {
    $msg = [string]$_.ErrorDetails.Message
    if ($msg -match 'name_exists|already exists|has already been taken') {
      Write-Host "  仓库已存在，跳过创建，直接推送" -ForegroundColor Yellow
    } else {
      throw
    }
  }

  Write-Host "=== 4/5 配置 remote ===" -ForegroundColor Cyan
  $existing = & $git remote 2>&1
  if ($existing -contains $Remote) { & $git remote set-url $Remote $repoUrl }
  else { & $git remote add $Remote $repoUrl }
  Write-Host "  remote $Remote -> $repoUrl"

  Write-Host "=== 5/5 推送 main / agent-a / agent-b（git 优先，失败自动切 API）===" -ForegroundColor Cyan
  $env:TRADEDESK_GITHUB_TOKEN = $token
  $ErrorActionPreference = "Continue"
  & (Join-Path $Root "scripts\push.ps1") -Owner $Owner -Repo $RepoName -Branches @("main", "agent-a", "agent-b")
  $pushCode = $LASTEXITCODE
  $env:TRADEDESK_GITHUB_TOKEN = $null
  if ($pushCode -ne 0) { throw "推送失败（exit=$pushCode）" }

  Write-Host "==============================================" -ForegroundColor Cyan
  Write-Host "  仓库地址：https://github.com/$Owner/$RepoName" -ForegroundColor Green
  Write-Host "  网页可能打不开（网络问题），复核方式：" -ForegroundColor Yellow
  Write-Host "    & `"$py`" `"$Root\scripts\push_via_api.py`" --owner $Owner --repo $RepoName --dry-run" -ForegroundColor Yellow
  Write-Host "==============================================" -ForegroundColor Cyan
}
finally {
  $token = $null
  $env:TRADEDESK_GITHUB_TOKEN = $null
}