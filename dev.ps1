# TradeDesk-AI 开发脚本 —— 在任意目录都能调用（自动定位项目根）
# 用法：
#   & "D:\桌面\AIagent_project\外贸项目\dev.ps1" up        # 起 db + redis
#   & "D:\桌面\AIagent_project\外贸项目\dev.ps1" verify    # 双后端验证（S1）
#   & "D:\桌面\AIagent_project\外贸项目\dev.ps1" test      # pytest
#   & "D:\桌面\AIagent_project\外贸项目\dev.ps1" status    # 看容器与端口
#   & "D:\桌面\AIagent_project\外贸项目\dev.ps1" down      # 停容器
param(
  [Parameter(Position = 0)][string]$Command = "help",
  [string]$Backend = "both",
  [string]$Dsn = ""            # 指定数据库（两个 agent 用不同库，避免互相清表）
)

# ⚠ 本文件必须保存为 **UTF-8 with BOM**：Windows PowerShell 5.1 会把无 BOM 的脚本按 GBK 读取，
#   中文乱码会导致整段语法解析失败。PowerShell 7 则无所谓。为兼容两端，这里统一带 BOM。
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Get-DockerExe {
  $cands = @(
    "C:\Users\DELL\AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe",
    "C:\Program Files\Docker\Docker\resources\bin\docker.exe"
  )
  foreach ($c in $cands) { if (Test-Path -LiteralPath $c) { return $c } }
  $onPath = Get-Command docker -ErrorAction SilentlyContinue
  if ($onPath) { return $onPath.Source }
  throw "找不到 docker.exe：Docker Desktop 未安装，或路径不在上面两个位置。"
}

function Get-PythonExe {
  # worktree 里没有自己的 .venv，向上找主仓库的（worktree 位于 <repo>\.worktrees\<name>）
  $dir = $Root
  for ($i = 0; $i -lt 3; $i++) {
    $venv = Join-Path $dir ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venv) { return $venv }
    $parent = Split-Path -Parent $dir
    if (-not $parent -or $parent -eq $dir) { break }
    $dir = $parent
  }
  $fallback = "C:\Users\DELL\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
  if (Test-Path -LiteralPath $fallback) {
    Write-Host "[提示] 项目虚拟环境不存在，先创建 .venv" -ForegroundColor Yellow
    & $fallback -m venv (Join-Path $Root ".venv")
    $env:PYTHONNOUSERSITE = "1"
    & (Join-Path $Root ".venv\Scripts\python.exe") -m pip install -q -r (Join-Path $Root "requirements.txt")
    return (Join-Path $Root ".venv\Scripts\python.exe")
  }
  throw "找不到可用 Python：既没有 .venv，也没有 Codex 自带解释器。"
}

switch ($Command.ToLower()) {
  "up" {
    $d = Get-DockerExe
    Write-Host "[up] 启动 db(PostgreSQL 16) + redis ..." -ForegroundColor Cyan
    & $d compose up -d db redis
    & $d ps --format "{{.Names}} | {{.Status}} | {{.Ports}}"
  }
  "down" {
    $d = Get-DockerExe
    Write-Host "[down] 停止 db + redis ..." -ForegroundColor Cyan
    & $d compose down
  }
  "verify" {
    $py = Get-PythonExe
    Write-Host "[verify] 运行 S1 验证（后端：$Backend）" -ForegroundColor Cyan
    if ($Dsn) { & $py (Join-Path $Root "scripts\verify_s1.py") --backend $Backend --dsn $Dsn }
    else { & $py (Join-Path $Root "scripts\verify_s1.py") --backend $Backend }
    exit $LASTEXITCODE
  }
  "test" {
    $py = Get-PythonExe
    Write-Host "[test] 运行 pytest ..." -ForegroundColor Cyan
    & $py -m pytest -q
    exit $LASTEXITCODE
  }
  "status" {
    $d = Get-DockerExe
    & $d ps --format "{{.Names}} | {{.Status}} | {{.Ports}}"
    Write-Host "---- 本地监听 ----"
    Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
      Where-Object { $_.LocalPort -in 5433, 6380 } |
      Select-Object LocalAddress, LocalPort | Format-Table -AutoSize
  }
  "check" {
    $DsnArgs = @()
    if ($Dsn) { $DsnArgs = @("--dsn", $Dsn) }
    Write-Host "==============================================" -ForegroundColor Cyan
    Write-Host "[check 1/3] Python 语法检查（src / tests / scripts）" -ForegroundColor Cyan
    $py = Get-PythonExe
    & $py -m compileall -q (Join-Path $Root "src") (Join-Path $Root "tests") (Join-Path $Root "scripts")
    if ($LASTEXITCODE -ne 0) { Write-Host "  ✗ 语法检查未通过" -ForegroundColor Red; exit 1 }
    Write-Host "  ✓ 语法检查通过" -ForegroundColor Green

    Write-Host "[check 2/3] S1 验证（SQLite + PostgreSQL 双后端）" -ForegroundColor Cyan
    if ($DsnArgs.Count -gt 0) { & $py (Join-Path $Root "scripts\verify_s1.py") --backend both @DsnArgs }
    else { & $py (Join-Path $Root "scripts\verify_s1.py") --backend both }
    if ($LASTEXITCODE -ne 0) { Write-Host "  ✗ 验证未通过" -ForegroundColor Red; exit 1 }
    Write-Host "  ✓ 验证通过" -ForegroundColor Green

    Write-Host "[check 3/3] pytest 单元测试" -ForegroundColor Cyan
    & $py -m pytest -q
    if ($LASTEXITCODE -ne 0) { Write-Host "  ✗ 测试未通过" -ForegroundColor Red; exit 1 }
    Write-Host "  ✓ 测试通过" -ForegroundColor Green

    Write-Host "==============================================" -ForegroundColor Cyan
    Write-Host "全部检查通过。项目状态良好。" -ForegroundColor Green
    exit 0
  }  "push" {
    Write-Host "[push] 推送当前分支到 GitHub（git 优先，失败自动切 API）" -ForegroundColor Cyan
    & (Join-Path $Root "scripts\push.ps1")
    exit $LASTEXITCODE
  }
  "clean" {
    Write-Host "[clean] 清理生成物（验证临时文件 / 缓存 / __pycache__）..." -ForegroundColor Cyan
    $targets = @(
      (Join-Path $Root "data\tmp"),
      (Join-Path $Root ".pytest_cache"),
      (Join-Path $Root ".pytest_tmp")
    )
    foreach ($t in $targets) {
      if (Test-Path -LiteralPath $t) { Remove-Item -LiteralPath $t -Recurse -Force -ErrorAction SilentlyContinue; Write-Host "  - removed $t" }
    }
    # 只清理项目自己的代码目录，不动 .venv 里的依赖缓存
    $codeDirs = @("src", "tests", "scripts") | ForEach-Object { Join-Path $Root $_ }
    $n = 0
    foreach ($d in $codeDirs) {
      if (Test-Path -LiteralPath $d) {
        Get-ChildItem -Path $d -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
          ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue; $n++ }
      }
    }
    if ($n -gt 0) { Write-Host "  - removed $n 个 __pycache__（src/tests/scripts）" }
    Get-ChildItem -Path (Join-Path $Root "data") -File -Filter "*.db*" -ErrorAction SilentlyContinue |
      ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue; Write-Host "  - removed $($_.Name)" }
    Write-Host "[clean] 完成" -ForegroundColor Cyan
  }  "shell" {
    $py = Get-PythonExe
    & $py
  }
  default {
    Write-Host @"
TradeDesk-AI 开发脚本（项目根：$Root）

  up       启动 PostgreSQL 16(5433) + Redis 7(6380)
  down     停止这两个容器
  verify   跑 S1 验证（可选参数 -Backend sqlite|postgres|both，默认 both）
  test     跑 pytest
  status   查看容器与端口
  check    交付前自检：语法 + 双后端验证 + pytest
  push     推送当前分支到 GitHub（git 优先，失败自动切 API）
  clean    清理验证临时文件、缓存与 __pycache__
  # 说明：verify 跑完会自动清理临时物，clean 只是手动扫尾
  shell    进入项目 Python 交互环境

示例：
  & "$($MyInvocation.MyCommand.Path)" verify
  & "$($MyInvocation.MyCommand.Path)" verify -Backend postgres
"@ -ForegroundColor Cyan
  }
}