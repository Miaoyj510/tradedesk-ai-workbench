# TradeDesk-AI —— 把本机代理配置给 git（不依赖"系统代理"开关）
#
# 背景
#   git(libcurl) 不读 Windows 的"系统代理"开关，只有浏览器与 .NET 会读。
#   结果：Clash 的系统代理一开，浏览器能上 github.com、git 却被重置；
#        一关，连浏览器也直连超时。
#   但 Clash 程序本身一直在跑、端口一直在听 —— 所以正确做法是
#   **只看代理端口是否在监听，是就把地址写给 git**，与那个开关无关。
#
# 用法：
#   & "D:\桌面\AIagent_project\外贸项目\scripts\setup_git_proxy.ps1"            # 仅当前仓库
#   & "D:\桌面\AIagent_project\外贸项目\scripts\setup_git_proxy.ps1" -Global    # 全局生效
param([switch]$Global)

$ErrorActionPreference = "Stop"
$Root = Split-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) -Parent
Set-Location $Root
$git = "C:\Users\DELL\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\git\cmd\git.exe"
if (-not (Test-Path -LiteralPath $git)) { $git = (Get-Command git).Source }

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
        if (-not $listen) { return $null }    # 端口没在听 = 代理没跑
      }
    }
    return $first
  } catch {
    return $null
  }
}

$proxy = Get-UsableProxy
if (-not $proxy) {
  Write-Host "没有可用的本机代理（Clash 端口未在监听）→ 保持直连。" -ForegroundColor Yellow
  Write-Host "若 git 推送超时，请启动 Clash Verge 后重试，或设置环境变量 TRADEDESK_PROXY=http://127.0.0.1:7897" -ForegroundColor Yellow
  exit 0
}

$scope = "仅本仓库"
if ($Global) { & $git config --global http.proxy $proxy; $scope = "全局" }
else { & $git config --local http.proxy $proxy }
Write-Host "已设置 git 代理：$proxy（$scope）" -ForegroundColor Green

$env:http_proxy = $proxy
$env:https_proxy = $proxy
Write-Host "连通性测试 ..." -ForegroundColor Cyan
$out = & $git ls-remote https://github.com/octocat/Hello-World.git 2>&1
if ($LASTEXITCODE -eq 0) {
  Write-Host "  ✓ 代理可用，git 能连通 GitHub" -ForegroundColor Green
  exit 0
} else {
  Write-Host "  ✗ 仍不通" -ForegroundColor Red
  @($out) | Where-Object { $_ -and $_.ToString().Trim() } | Select-Object -First 2 | ForEach-Object { Write-Host "    $_" -ForegroundColor Yellow }
  exit 1
}