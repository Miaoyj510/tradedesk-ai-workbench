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
  [string]$Dsn = "",           # 指定数据库（两个 agent 用不同库，避免互相清表）
  [switch]$Live,              # 模型层评测是否跑真实云端（会产生费用）
  [int]$PerLang = 10          # 模型层评测每语种跑多少条
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
    if ($Dsn) { $env:TRADEDESK_TEST_DSN = $Dsn }
    & $py -m pytest -q --basetemp (Join-Path $Root ".pytest_tmp")
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
    Write-Host "[check 1/11] Python 语法检查（src / tests / scripts）" -ForegroundColor Cyan
    $py = Get-PythonExe
    & $py -m compileall -q (Join-Path $Root "src") (Join-Path $Root "tests") (Join-Path $Root "scripts")
    if ($LASTEXITCODE -ne 0) { Write-Host "  ✗ 语法检查未通过" -ForegroundColor Red; exit 1 }
    Write-Host "  ✓ 语法检查通过" -ForegroundColor Green

    Write-Host "[check 2/11] S1 验证（SQLite + PostgreSQL 双后端）" -ForegroundColor Cyan
    if ($DsnArgs.Count -gt 0) { & $py (Join-Path $Root "scripts\verify_s1.py") --backend both @DsnArgs }
    else { & $py (Join-Path $Root "scripts\verify_s1.py") --backend both }
    if ($LASTEXITCODE -ne 0) { Write-Host "  ✗ 验证未通过" -ForegroundColor Red; exit 1 }
    Write-Host "  ✓ 验证通过" -ForegroundColor Green

      Write-Host "[check 3/11] S2 验证（编排运行时 + HTTP 接口 + 闸门）" -ForegroundColor Cyan
      & $py (Join-Path $Root "scripts\verify_s2.py")
      if ($LASTEXITCODE -ne 0) { Write-Host "  ✗ S2 验证未通过" -ForegroundColor Red; exit 1 }
      Write-Host "  ✓ S2 验证通过" -ForegroundColor Green


    Write-Host "[check 4/11] S3 验证（检索层：多语种 / 时效 / 引用）" -ForegroundColor Cyan
    if ($DsnArgs.Count -gt 0) { & $py (Join-Path $Root "scripts\verify_s3.py") @DsnArgs }
    else { & $py (Join-Path $Root "scripts\verify_s3.py") }
    if ($LASTEXITCODE -ne 0) { Write-Host "  ✗ S3 验证未通过" -ForegroundColor Red; exit 1 }
    Write-Host "  ✓ S3 验证通过" -ForegroundColor Green

    Write-Host "[check 5/11] S2 独立审核（审核方断言，非自证）" -ForegroundColor Cyan
    if ($DsnArgs.Count -gt 0) { & $py (Join-Path $Root "scripts\audit_s2.py") @DsnArgs }
    else { & $py (Join-Path $Root "scripts\audit_s2.py") }
    if ($LASTEXITCODE -ne 0) { Write-Host "  ✗ S2 独立审核未通过" -ForegroundColor Red; exit 1 }
    Write-Host "  ✓ S2 独立审核通过" -ForegroundColor Green

    Write-Host "[check 6/11] pytest 单元测试" -ForegroundColor Cyan
    if ($Dsn) { $env:TRADEDESK_TEST_DSN = $Dsn }
    & $py -m pytest -q --basetemp (Join-Path $Root ".pytest_tmp")
    if ($LASTEXITCODE -ne 0) { Write-Host "  ✗ 测试未通过" -ForegroundColor Red; exit 1 }
    Write-Host "  ✓ 测试通过" -ForegroundColor Green

    Write-Host "[check 7/11] S3 独立审核（审核方断言，非自证）" -ForegroundColor Cyan
    if ($DsnArgs.Count -gt 0) { & $py (Join-Path $Root "scripts\audit_s3.py") @DsnArgs }
    else { & $py (Join-Path $Root "scripts\audit_s3.py") }
    if ($LASTEXITCODE -ne 0) { Write-Host "  ✗ S3 独立审核未通过" -ForegroundColor Red; exit 1 }
    Write-Host "  ✓ S3 独立审核通过" -ForegroundColor Green

    Write-Host "[check 8/11] S4 验证（向量层：嵌入契约 / 向量存储 / 索引 / 余弦检索）" -ForegroundColor Cyan
    if ($DsnArgs.Count -gt 0) { & $py (Join-Path $Root "scripts\verify_s4.py") @DsnArgs }
    else { & $py (Join-Path $Root "scripts\verify_s4.py") }
    if ($LASTEXITCODE -ne 0) { Write-Host "  ✗ S4 验证未通过" -ForegroundColor Red; exit 1 }
    Write-Host "  ✓ S4 验证通过" -ForegroundColor Green

    Write-Host "[check 9/11] S4 独立审核（审核方断言，非自证）" -ForegroundColor Cyan
    if ($DsnArgs.Count -gt 0) { & $py (Join-Path $Root "scripts\audit_s4.py") @DsnArgs }
    else { & $py (Join-Path $Root "scripts\audit_s4.py") }
    if ($LASTEXITCODE -ne 0) { Write-Host "  ✗ S4 独立审核未通过" -ForegroundColor Red; exit 1 }
    Write-Host "  ✓ S4 独立审核通过" -ForegroundColor Green

    Write-Host "[check 10/11] S5 独立审核（审核方断言，非自证；S5 不在门禁里就会回归失控）" -ForegroundColor Cyan
    & $py (Join-Path $Root "scripts\audit_s5.py")
    if ($LASTEXITCODE -ne 0) { Write-Host "  ✗ S5 独立审核未通过" -ForegroundColor Red; exit 1 }
    Write-Host "  ✓ S5 独立审核通过" -ForegroundColor Green

    Write-Host "[check 11/11] S6 独立审核（审核方断言：读模型 / 写操作闸门 / 口径 / 渲染层）" -ForegroundColor Cyan
    # ⚠️ audit_s6 的 --dsn 指向的库会被它**DROP 后重建**（脚本自建暂存库并回收）。
    # 所以这里**绝不能**把开发库（tradedesk_a / tradedesk_b）传进去 ——
    # 传进去等于删开发库。改成从给定 DSN 派生一个独立暂存库名。
    $audit6Args = @()
    if ($Dsn) {
      $head = $Dsn.Substring(0, $Dsn.LastIndexOf("/") + 1)
      $audit6Args = @("--dsn", ($head + "tradedesk_audit_s6"))
    }
    & $py (Join-Path $Root "scripts\audit_s6.py") @audit6Args
    if ($LASTEXITCODE -ne 0) { Write-Host "  ✗ S6 独立审核未通过" -ForegroundColor Red; exit 1 }
    Write-Host "  ✓ S6 独立审核通过" -ForegroundColor Green

    Write-Host "==============================================" -ForegroundColor Cyan
    Write-Host "全部检查通过。项目状态良好。" -ForegroundColor Green
    exit 0
    }

  "audit" {
    $py = Get-PythonExe
    Write-Host "[audit] 运行 S2 独立审核（审核 agent 的断言，非自证）" -ForegroundColor Cyan
    if ($Dsn) { & $py (Join-Path $Root "scripts\audit_s2.py") --dsn $Dsn }
    else { & $py (Join-Path $Root "scripts\audit_s2.py") }
    exit $LASTEXITCODE
  }
  "audit3" {
    $py = Get-PythonExe
    Write-Host "[audit3] 运行 S3 独立审核（审核 agent 的断言，非自证）" -ForegroundColor Cyan
    if ($Dsn) { & $py (Join-Path $Root "scripts/audit_s3.py") --dsn $Dsn }
    else { & $py (Join-Path $Root "scripts/audit_s3.py") }
    exit $LASTEXITCODE
  }
  "audit4" {
    $py = Get-PythonExe
    Write-Host "[audit4] 运行 S4 独立审核（审核 agent 的断言，非自证）" -ForegroundColor Cyan
    if ($Dsn) { & $py (Join-Path $Root "scripts/audit_s4.py") --dsn $Dsn }
    else { & $py (Join-Path $Root "scripts/audit_s4.py") }
    exit $LASTEXITCODE
  }
  "audit5" {
    $py = Get-PythonExe
    Write-Host "[audit5] 运行 S5 独立审核（审核 agent 的断言，非自证）" -ForegroundColor Cyan
    & $py (Join-Path $Root "scripts/audit_s5.py")
    exit $LASTEXITCODE
  }
  "audit6" {
    $py = Get-PythonExe
    Write-Host "[audit6] 运行 S6 独立审核（审核 agent 的断言：读模型 + 写操作闸门 + 口径 + 渲染层）" -ForegroundColor Cyan
    # ⚠️ --dsn 指向的库会被 audit_s6 **DROP 后重建**（自建暂存库并回收）。
    # 因此这里把传入 DSN 的库名替换成独立暂存库，避免误删开发库。
    if ($Dsn) {
      $head = $Dsn.Substring(0, $Dsn.LastIndexOf("/") + 1)
      & $py (Join-Path $Root "scripts\audit_s6.py") --dsn ($head + "tradedesk_audit_s6")
    } else {
      & $py (Join-Path $Root "scripts\audit_s6.py")
    }
    exit $LASTEXITCODE
  }
  "index" {
    $py = Get-PythonExe
    Write-Host "[index] 建立知识库向量索引（**会调云端嵌入 API，会计费**）" -ForegroundColor Cyan
    $idxArgs = @()
    if ($Dsn) { $idxArgs = @("--dsn", $Dsn) }
    & $py (Join-Path $Root "scripts\index_kb.py") @idxArgs
    exit $LASTEXITCODE
  }
  "serve" {
    Write-Host "[serve] 启动 HTTP 接口（http://127.0.0.1:8080/docs）" -ForegroundColor Cyan
    $py = Get-PythonExe
    $env:PYTHONPATH = Join-Path $Root "src"
    & $py -m uvicorn tradedesk.api.app:create_app --factory --host 127.0.0.1 --port 8080
    exit $LASTEXITCODE
  }  "push" {
    Write-Host "[push] 推送当前分支到 GitHub（git 优先，失败自动切 API）" -ForegroundColor Cyan
    & (Join-Path $Root "scripts\push.ps1")
    exit $LASTEXITCODE
  }
  "eval" {
    $py = Get-PythonExe
    Write-Host "[eval] 分语种分通道检索评测（自建语料层；需要向量索引，否则稠密通道会自动跳过）" -ForegroundColor Cyan
    $evalArgs = @()
    if ($Dsn) { $evalArgs = @("--dsn", $Dsn) }
    & $py (Join-Path $Root "scripts\eval_retrieval.py") @evalArgs
    exit $LASTEXITCODE
  }
  "evallm" {
    $py = Get-PythonExe
    Write-Host "[evallm] 模型层分语种评测（S5-1c；事实保真度 / 越权率 / 单位成本）" -ForegroundColor Cyan
    $evalArgs = @()
    if ($Live) { $evalArgs += "--live" }
    if ($PerLang -gt 0) { $evalArgs += @("--per-lang", $PerLang) }
    & $py (Join-Path $Root "scripts\eval_llm.py") @evalArgs
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
  check    交付前自检（十一步，含 S5/S6 独立审核）：语法 + 双后端 + S2/S3 自证 + S2/S3/S4 独立审核 + S4 + pytest
  audit    S2 独立审核（审核 agent 断言，见 docs/S2-AUDIT-R9.md；R9 缺陷修完前为红）
  audit3   S3 独立审核（审核 agent 断言，见 docs/S3-AUDIT-R11.md；R11 缺陷修完前为红）
  serve    启动 HTTP 接口（FastAPI，/docs 有接口文档）
  push     推送当前分支到 GitHub（git 优先，失败自动切 API）
  audit4   S4 独立审核（审核 agent 断言，见 docs/S4-AUDIT-R15.md；R15 缺陷修完前为红）
  audit5   S5 独立审核（审核 agent 断言，见 docs/S5-AUDIT-R19.md）
  audit6   S6 独立审核（审核 agent 断言，见 docs/S6-AUDIT-R21.md 与 docs/S6-ACCEPTANCE.md）
  index    建立知识库向量索引（显式动作，会调云端 API）
  eval     分语种分通道检索评测（S4-4c 自建语料层）
  evallm  模型层分语种评测（S5-1c；加 -Live 才跑真实云端，会产生费用）
  clean    清理验证临时文件、缓存与 __pycache__
  # 说明：verify 跑完会自动清理临时物，clean 只是手动扫尾
  shell    进入项目 Python 交互环境

示例：
  & "$($MyInvocation.MyCommand.Path)" verify
  & "$($MyInvocation.MyCommand.Path)" verify -Backend postgres
"@ -ForegroundColor Cyan
  }
}