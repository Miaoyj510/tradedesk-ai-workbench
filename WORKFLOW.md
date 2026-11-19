# 双 Agent 协作协议（TradeDesk-AI）

> 两个 AI agent 并行做同一个项目：**各写各的代码，各提各的提交，最后合并跑总测试。**
> 本文件是双方共同的约定；任何一方动手前先读一遍。

## 1. 目录与分支

| 角色 | 位置 | 分支 | 说明 |
| --- | --- | --- | --- |
| **集成区** | `D:\桌面\AIagent_project\外贸项目` | `main` | 只用来合并与整体验证，**不要在这里改代码** |
| **实现 agent** | `D:\桌面\AIagent_project\外贸项目\.worktrees\agent-a` | `agent-a` | 开发、验证、提交都在这里 |
| **审核 agent** | `D:\桌面\AIagent_project\外贸项目\.worktrees\agent-b` | `agent-b` | 审核、改进、提交都在这里 |

两个 worktree 是**同一仓库的两个检出**，共享 git 对象库，但文件互相独立 —— 一个 agent 改文件不会影响另一个。
主目录与 worktree 里的 `.venv`、`data/` 都是各自独立的（已被 .gitignore 忽略）。

## 2. 七条铁律

1. **只在自己的 worktree 里改文件**。不要直接编辑对方目录里的任何文件。
2. **只提交自己改的东西**。提交前必须 `git status` + `git diff --cached` 逐项确认；禁止无脑 `git add -A`。
3. **不碰对方的分支**。不要 checkout / rebase / reset / commit 到对方的分支上。
4. **不覆盖对方未提交的工作**。合并只在集成分支 `main` 上做，且必须先确认对方分支已提交。
5. **提交粒度 = 一个切片**。一个切片一次提交，提交信息写清楚改了什么、影响什么。
6. **提交前自检必须全绿**：在自己的 worktree 里跑 `dev.ps1 check`（语法 + 双后端验证 + pytest）。
7. **数据库要隔离**：两个 agent 用同一个 Postgres 容器，但**各用各的数据库**（见第 4 节），避免一个 agent 清表把另一个 agent 的验证搞挂。

## 3. 提交信息格式

```
<切片号>: <一句话做了什么>

<正文：改了什么文件、影响什么功能、怎么验证的>

Co-Authored-By: <agent 标识> <邮箱>
```

示例：

```
S2: Supervisor 路由 + Handoff 校验 + HTTP 接口

- 新增 runtime/supervisor.py：意图路由、风控优先仲裁、SLA 守护
- 新增 api/app.py：/health /threads /signoff，闸门令牌中间件
- 影响：询盘进入系统的唯一入口；写操作一律要求令牌
- 验证：dev.ps1 check 全绿（新增 verify_s2.py 12 项断言）

Co-Authored-By: Codex <noreply@openai.com>
```

## 4. 数据库隔离

容器 `tradedesk-db`（PostgreSQL 16）里各建一个库：

| Agent | 数据库 | DSN |
| --- | --- | --- |
| 实现 agent（agent-a） | `tradedesk_a` | `postgresql://tradedesk:tradedesk_local_dev@127.0.0.1:5433/tradedesk_a` |
| 审核 agent（agent-b） | `tradedesk_b` | `postgresql://tradedesk:tradedesk_local_dev@127.0.0.1:5433/tradedesk_b` |

建库（只做一次）：

```powershell
$docker = "C:\Users\DELL\AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe"
& $docker exec tradedesk-db psql -U tradedesk -d postgres -c "CREATE DATABASE tradedesk_a;"
& $docker exec tradedesk-db psql -U tradedesk -d postgres -c "CREATE DATABASE tradedesk_b;"
```

带库运行验证：

```powershell
& "D:\桌面\AIagent_project\外贸项目\.worktrees\agent-a\dev.ps1" check -Dsn "postgresql://tradedesk:tradedesk_local_dev@127.0.0.1:5433/tradedesk_a"
```

> SQLite 后端是本地文件，两个 agent 天然隔离（各自的 worktree 各有自己的 `data/`），无需额外处理。

## 5. 日常命令（在 worktree 里执行）

```powershell
$dev = "D:\桌面\AIagent_project\外贸项目\.worktrees\agent-a\dev.ps1"   # 换成自己的
& $dev check        # 交付前自检：语法 + 双后端验证 + pytest
& $dev verify       # 只跑 S1 验证
& $dev test         # 只跑 pytest
& $dev clean        # 清理临时物
```

## 6. 合并流程（双方都改完后，在集成分支执行）

```powershell
$repo = "D:\桌面\AIagent_project\外贸项目"
$git  = "C:\Users\DELL\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\git\cmd\git.exe"
Set-Location $repo

# 0) 确认双方都已提交、工作区干净
& $git status --short
& $git log --oneline --all --graph -n 15

# 1) 把审核方的分支合并进 main（--no-ff 保留合并记录）
& $git checkout main
& $git merge --no-ff agent-b -m "merge: agent-b 的审核修改合入 main"

# 2) 有冲突时：逐个文件解决 → git add <文件> → git commit
#    原则：对方新增的测试/文档不要删；行为冲突以 PRD 为准

# 3) 合并后跑完整测试（关键：合并完必须重跑，不能只信各自的绿灯）
& ".\dev.ps1" check
```

> 合并顺序建议：**先让审核 agent 把审核意见以提交形式落到 `agent-b`，再由实现 agent 把 `agent-b` 合进 `agent-a`（解决冲突、跑测试），最后由集成区把 `agent-a` 合进 `main` 并再跑一次 `check`**。
> 这样冲突只解一次，且每次合并后都有完整测试兜底。

## 7. 冲突处理原则

| 情况 | 怎么处理 |
| --- | --- |
| 同一文件双方都改了 | 以 **PRD 的表述**为准；先看 `git diff main..agent-b` 再动手 |
| 对方新增了测试 | **保留**，跑不通就改实现，不要删测试 |
| 对方改了闸门/状态机规则 | 以 PRD 8.5 / 4.3 为准；偏离 PRD 的要双方确认后再改 |
| 数据库相关冲突 | 合并后重建两个库再跑测试 |
| 实在判断不了 | 停下来说明冲突点，不要擅自选一边 |

## 8. 上传 GitHub

- 现在先把**本地**提交做扎实（每个切片一个提交）。
- 推 GitHub 建议**先推私有库**做备份，等全部切片完成、README 整理好再转公开。
- 推之前务必确认 `git status` 干净、`.gitignore` 生效（`.venv/`、`data/`、`*.db`、`.env` 绝不能进仓库）。