# TradeDesk-AI —— 外贸业务员的 AI 数字员工工作台

> 配套文档：`PRD-外贸业务员的AI数字员工工作台-v1.0.md`（产品需求，14 章）
> 前端原型：`ui-prototype/`（三页可交互原型，双击 `index.html` 即可）

## 当前进度

| 切片 | 内容 | 状态 | 验证方式 |
| --- | --- | --- | --- |
| **S1** | 时间维度骨架：状态机 + 存储（SQLite & PostgreSQL）+ 闸门令牌 + 审计哈希链 | ✅ 已完成 | `python scripts/verify_s1.py --backend both`（48 项 × 2 后端） |
| S2 | Supervisor 路由 + Handoff 协议 + HTTP 接口（FastAPI） | 计划中 | `scripts/verify_s2.py` |
| S3 | Sentinel 名单核验 + Inquiry 草稿装配 | 计划中 | `scripts/verify_s3.py` |
| S4 | 检索层（词法兜底 → BGE-M3 + pgvector 可替换） | 计划中 | `scripts/verify_s4.py` |
| S5 | 接真模型（MiniMax API Key）+ 成本计量 | 计划中 | `scripts/verify_s5.py` |
| S6 | 前端联调 + 镜像构建 + 灰度回滚 | 计划中 | `scripts/verify_s6.py` |

## 本机环境（已实测）

| 项 | 实际情况 | 处理 |
| --- | --- | --- |
| Docker Desktop | **已安装并运行**（server 29.6.2 / compose v5.3.1），但 CLI 在 `AppData` 下，沙箱内不可见 → 需用完整路径 `C:\Users\DELL\AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe` 调用 | 依赖服务用 compose 起 |
| Docker Hub | **拉取被网络阻断**（registry-1.docker.io 不可达） | 改用本机已缓存镜像：`postgres:16-alpine`、`redis:7-alpine`；pgvector 改本地构建（`docker/pgvector/Dockerfile`） |
| 可用解释器 | `C:\Users\DELL\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe`（3.12.14） | **用这个**。`D:\anaconda\python.exe` 的 `_ssl` DLL 损坏，装不了依赖 |
| 项目虚拟环境 | `.venv/`（已创建，pydantic + pytest + psycopg3） | `PYTHONNOUSERSITE=1` |
| 数据库 | PostgreSQL 16.15 容器（127.0.0.1:5433）+ Redis 7 容器（127.0.0.1:6380） | `docker compose up -d db redis` |

## 快速开始（推荐：dev.ps1，可在任意目录调用）

```powershell
$dev = "D:\桌面\AIagent_project\外贸项目\dev.ps1"

& $dev up        # 起 PostgreSQL 16(5433) + Redis 7(6380)
& $dev check     # 交付前自检：语法 + 双后端验证 + pytest（一键全绿）
& $dev verify    # 双后端验证（S1，48 项 × 2）
& $dev test      # pytest（26 项）
& $dev status    # 看容器与端口
& $dev push      # 推送当前分支到 GitHub（git 优先，失败自动切 API）
& $dev down      # 停容器

& $dev verify -Backend postgres   # 只跑 Postgres 后端
```

> `dev.ps1` 会自动定位项目根、docker.exe 与项目虚拟环境，**不需要先 cd 进项目目录**。

## 手动命令（需先 cd 到项目根）

```powershell
cd "D:\桌面\AIagent_project\外贸项目"

# 依赖服务
$docker = "C:\Users\DELL\AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe"
& $docker compose up -d db redis

# 验证与测试
& .\.venv\Scripts\python.exe scripts\verify_s1.py --backend both
& .\.venv\Scripts\python.exe -m pytest -q
```

> `verify_s1.py` 每次运行都会重建存储并输出逐项 PASS/FAIL，退出码 0 表示全通过。
> 建议节奏：**每完成一个切片就跑一次**，回归时用 `--backend both`。


## 文件夹说明（每一项都是干什么的）

| 路径 | 说明 | 能不能删 |
| --- | --- | --- |
| `PRD-外贸业务员的AI数字员工工作台-v1.0.md` | 产品需求文档（唯一的产品真相） | 不能 |
| `README.md` | 本文件：怎么跑、当前进度 | 不能 |
| `dev.ps1` | 日常入口：`up / down / verify / test / clean / status / shell` | 不能 |
| `src/tradedesk/domain/` | 领域层：枚举、模型（线程状态/承诺台账/草稿/引用/令牌/审计）、状态机 | 不能 |
| `src/tradedesk/store/` | 存储层：`base.py` 接口 + `sqlite_store.py` + `postgres_store.py`（同接口双实现） | 不能 |
| `src/tradedesk/runtime/gates.py` | 11 条人审闸门 + 闸门令牌服务 | 不能 |
| `src/tradedesk/data/seed.py` | 演示数据（名单为结构化演示数据，**非真实名单**） | 不能 |
| `src/tradedesk/config.py` | 配置读取（环境变量 > .env > 默认值） | 不能 |
| `scripts/verify_s1.py` | **验证脚本**：跑完自动清理临时物；`--keep` 保留现场，`--self-delete` 用完即焚 | 可（用完即焚） |
| `tests/` | pytest 用例（26 项，PostgreSQL 用例在容器未启动时自动跳过） | 不能 |
| `docker-compose.yml` | 依赖服务：PostgreSQL 16(5433) + Redis 7(6380) | 不能 |
| `docker/pgvector/Dockerfile` | 本地构建 pgvector 镜像的配方（S4 用，因为 Docker Hub 拉不动） | S4 前可留 |
| `Dockerfile` | 应用镜像（S2 起使用） | 不能 |
| `ui-prototype/` | 前端三页原型（工作台/看板/知识库），双击 `index.html` 即可看 | 不能 |
| `ui-prototype/design-system/` | ui-ux-pro-max 生成并持久化的设计系统（原型用的令牌与规则） | 可删（原型自带样式） |
| `.venv/` | 项目虚拟环境（pydantic / pytest / psycopg3） | **删了要重装** |
| `data/` | 运行时数据目录（主库文件、临时目录），验证后会保持为空 | 可（会自动重建） |
| `.env.example` | 配置模板，复制成 `.env` 后填 MiniMax Key | 不能 |
| `.gitignore` / `pytest.ini` / `requirements.txt` | 常规工程文件 | 不能 |

### ⚠ 两条硬规矩（都踩过坑）

1. **所有 `.ps1` 必须存成 UTF-8 with BOM**。Windows PowerShell 5.1（你终端里的那个）会把无 BOM 的脚本按 GBK 读，中文乱码 → 整段语法解析失败。PowerShell 7 无所谓，但为了两端一致，统一带 BOM。
2. **Python 脚本不要强制 UTF-8 输出**。`verify_s1.py` 已改成自适应：交互式控制台沿用控制台原生编码（中文 Windows 是 GBK/CP936，终端里显示正常），被重定向或抓取时才用 UTF-8。

> 交付前自检：在 **Windows PowerShell 5.1** 下跑 `& $dev check`，三段全绿再交付。
> 验证方式：`powershell.exe -NoProfile -ExecutionPolicy Bypass -File "<项目>\dev.ps1" check`
### 发布到 GitHub

网页版 `github.com/new` 在本机网络下会报「Couldn't check availability」（api.github.com 可达，但网页那个查询请求超时），所以走脚本：

```powershell
# ① 先到 https://github.com/settings/tokens/new 建 Token（classic，勾 repo 权限）
# ② 自检（不联网、不需要 Token）
& ".\scripts\publish_github.ps1" -DryRun
# ③ 建私有仓库并推送 main / agent-a / agent-b（会提示输入 Token，不回显）
& ".\scripts\publish_github.ps1"
# 想建公开仓库：加 -Public
```

Token 只在本机内存里用：通过 `GIT_ASKPASS` 传给 git，**不写进 `.git/config`、不进命令行历史、不进聊天记录**。
推送策略：**现在推私有库做备份与协作底座，全部切片完成、README 整理好之后再转公开。**

### 保持目录干净的三条规矩

1. **验证脚本结束即清理**：每次 `verify` 跑完会自动删掉 SQLite 临时库、清空 PostgreSQL 表、删除 `data/tmp`。
2. **临时目录固定在项目内**：pytest 不用自带的 `--basetemp`（受限 ACL 下会 PermissionError），改用 `tests/conftest.py` 的 `tmpdir_local`。
3. **`dev.ps1 clean`**：一次性清掉验证残留、`.pytest_cache` 与 `src/tests/scripts` 下的 `__pycache__`（不会动 `.venv`）。

```powershell
# 想看验证过程中的中间产物：
& $dev verify -Backend postgres --keep     # 保留临时库，便于用 psql 查
# 用完即焚（连脚本一起删）：
& .\.venv\Scripts\python.exe scripts\verify_s1.py --backend both --self-delete
```

## 目录结构

```
src/tradedesk/
├── config.py             配置（环境变量 > .env > 默认值）
├── domain/
│   ├── enums.py          阶段 / 风险 / 意图 / 闸门 / 事件类型
│   ├── models.py         线程状态、客户档案、承诺台账、草稿、引用、令牌、审计
│   └── state_machine.py  8 状态迁移表 + 前置条件 + 幂等（纯函数，可穷举测试）
├── store/
│   ├── base.py           StateStore 接口 + 共享的审计哈希算法
│   ├── sqlite_store.py   SQLite 实现（零依赖、便于单机验证）
│   └── postgres_store.py PostgreSQL 16 实现（与 SQLite 同一接口，可替换）
├── runtime/
│   └── gates.py          11 条人审闸门 + 闸门令牌服务（一次性消费）
└── data/seed.py          演示数据（名单为结构化演示数据，非真实名单）
docker/
└── pgvector/Dockerfile   本地构建 pgvector 镜像（Docker Hub 不可达时的路径）
docker-compose.yml        db（PostgreSQL 16）+ redis
Dockerfile                应用镜像（S2 起使用）
scripts/verify_s1.py      S1 验证脚本（双后端）
tests/                    pytest 用例（26 项）
```

## 设计要点（为什么这么写）

| 决策 | 原因 |
| --- | --- |
| 状态迁移是**纯函数** | 可穷举测试、可回放；不把 IO 和规则混在一起 |
| 前置条件写进**迁移表** | 进入订单必须有合同版本这类规则，不散落在业务代码里 |
| 闸门令牌**服务端签发、一次性、绑定线程与草稿** | 写操作的凭证不能由 Agent 自己声称"已获批准" |
| 审计**哈希链** | 只追加之外还要"可验伪"：篡改一条就能定位到 seq |
| 存储做成**接口 + 两套实现** | PRD 13.4.5 选型轴②的可替换性；SQLite 便于每天验证，PostgreSQL 贴近生产 |
| 名单用**结构化演示数据** | 真实名单源需要商用授权（PRD 13.4.12）；演示数据必须显式标注 |