# NEXT —— 状态卡（2026-11-19）

> **每次开工先读这一页。** 上下文丢了不要紧，这页写着现在到哪了、下一步干什么。
> 配套：`docs/S2-ACCEPTANCE.md`（验收基线）、`docs/S2-AUDIT-R9.md`（审核报告详版）。

## 一、现在到哪了

| 切片 | 状态 |
| --- | --- |
| S1（存储层 / 领域模型 / 状态机 / 闸门） | ✅ 收口 |
| S2（Supervisor / Handoff / FastAPI / 闸门令牌） | ✅ 已交卷（`4aeb6be`），自证 94/94 + pytest 101 全绿 |
| **S2 审核 R9** | ✅ 完成（`8927d7c`，分支 `agent-b`）：独立断言 107 项，**14 个根因待修** |
| S2 缺陷修复 | ⬜ **未开始 —— 下一步就是这件事** |
| S3+ | ⬜ 未开始 |

分支现状：`main` 领先 3 个提交（`scripts/push.ps1` 可靠性修复，**与 S2 运行时无关**），
`agent-b` 领先 1 个提交（R9 审核）。`agent-a` 与 `main` 同步在 `c381d62`。

## 二、下一步：修 R9 的 14 个根因

详版（复现步骤、PRD 出处、最小改法）见 `docs/S2-AUDIT-R9.md`；条款号见
`docs/S2-ACCEPTANCE.md` 第六节。按这个顺序做：

### 切片 A —— 越权路径（三条一起做，同一个根因族）

| # | 缺陷 | 位置 |
| --- | --- | --- |
| **R9-01（P0）** | 推进阶段是写操作，却不挂闸门令牌 —— 无令牌 `POST /advance` 返回 200 且阶段已变 | `api/app.py` 的 `/advance` 端点 + `runtime/supervisor.py` 的 `Supervisor.advance` |
| R9-02 | 无令牌的写请求被依赖层 403，**审计里一条留痕都没有** | `api/deps.py` 的 `require_gate_token` |
| R9-02b | `action=""` 的令牌是**万能令牌**（签发端不校验 action，可用于 `send` 外发） | `api/app.py` 的 `GateTokenBody` + `runtime/gates.py` 的 `consume` |

验收：`dev.ps1 audit` 里 §4.1 / §4.2 全绿。

### 切片 B —— 并发（要动存储层）

| # | 缺陷 | 位置 |
| --- | --- | --- |
| R9-03 | 幂等判定与建线程不原子 → 并发重复投递建出 2~5 条线程 | `runtime/supervisor.py` 的 `ingest` + 存储层唯一约束 |
| R9-04 | 同线程并发写 → 乐观锁冲突抛未捕获 `RuntimeError` → 500（应 409） | `store/*.py` 的 `save_thread` + Supervisor 各写方法 |

### 切片 C —— 状态持久性（跨天长事务的地基，S3 开工前必须做掉）

| # | 缺陷 | 位置 |
| --- | --- | --- |
| R9-05 | SLA 是死代码：`SLA_DEFAULTS` 零读取点、线程 `deadlines` 恒空、Watchdog 扫不到 | `runtime/supervisor.py` + `config.py` + `domain/state_machine.py` |
| R9-06 | 草稿只存内存 → 重启后签核界面 `draft_body=None` | `runtime/supervisor.py` 的 `self.drafts` |
| R9-07 | 线程号按 `len(线程数)` 推导 → 多实例 / 库里有空洞就撞号 | `runtime/supervisor.py` 的 `_id_base` / `_next_thread_id` |

### 切片 D —— 签核与留痕细节（可合并成一个切片）

R9-08 修改后通过不承载内容 ｜ R9-09 签核留痕缺价格要素与条款版本 ｜
R9-10 Agent 动作记成 `actor=system` ｜ R9-11 未注册角色抛 RuntimeError ｜
R9-12 Handoff 异常类型不统一 ｜ R9-13 签核后 `last_activity_at` 不推进

## 三、每片子的验收动作

```powershell
$dev = "D:\桌面\AIagent_project\外贸项目\dev.ps1"
& $dev check -Dsn "postgresql://tradedesk:tradedesk_local_dev@127.0.0.1:5433/tradedesk_a"  # 实现 agent
& $dev audit                                                                          # 审核断言
```

**`dev.ps1 audit` 现在是红的**（14 个根因）。修完切片 A/B/C/D 后它必须全绿，
届时建议把它折进 `dev.ps1 check`，让越权路径从此变成交付门禁。

审核脚本双后端跑法（脚本自建独立库并回收，**不要**指向 `tradedesk_a` / `tradedesk_b`）：

```powershell
& $dev audit
& $dev audit -Dsn "postgresql://tradedesk:tradedesk_local_dev@127.0.0.1:5433/tradedesk_audit_b"
```

## 四、别忘了合并

R9 审核提交在 `agent-b`（`8927d7c`），要落到主线得按 WORKFLOW.md 第 6 节走：
`agent-b` → `agent-a`（解决冲突、跑测试）→ `main`（再跑一次 `check`）。
合并顺序建议：**先让实现 agent 把 R9 缺陷修完并让 audit 跑绿，再一起合**，
免得把一个已知红的审核脚本合进 main。

## 五、几个已知的坑

见根目录 `CLAUDE.md` 的"本项目特有的坑"一节（枚举 `str()`、SQLite 跨线程、
闸门令牌绑定、`dev.ps1` 的 BOM、GBK 控制台、PostgreSQL 审核库）。
