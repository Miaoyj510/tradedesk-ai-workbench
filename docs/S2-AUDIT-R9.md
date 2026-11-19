# S2 审核报告（R9）—— 审核 agent

> 审核对象：main @ `c381d62`（S2 收口，含审核方 R1–R8 修复）。
> 审核方式：**不复用** `scripts/verify_s2.py` 的断言，按 `docs/S2-ACCEPTANCE.md` 逐条重写，
> 重点补它没覆盖的边界（越权路径、并发、幂等、双后端、线程号）。
> 复核脚本：`scripts/audit_s2.py`（107 项断言，SQLite / PostgreSQL 各跑一遍）。

## 一、结论

| 项 | 数 |
| --- | --- |
| 审核断言 | 107 |
| 通过 | 90（SQLite）/ 92（PostgreSQL） |
| **违约 FAIL** | **16（SQLite）/ 14（PostgreSQL）** —— 归并为 **14 个根因**（差的 1~2 项是并发时序相关，不稳定复现） |
| 记账 WARN | 1 |
| 自证脚本 `verify_s2.py` | 94/94 **全绿** |
| 自带 pytest | 101 passed **全绿** |

**S2 的地基是扎实的，问题集中在两块：写操作的闸门边界，和状态的持久性。**

已经立住的（不用返工）：

- 风控前置是**结构性**的，不是事后过滤 —— 用 spy agent 证明名单命中时 `Inquiry.handle()` 根本没被调用；
- 冲突以 Sentinel 为准；投诉转人工；Merch / Growth 只声明不调度，被调用显式返回"本期未实现"；
- 客户档案跨线程共享、线程状态互不串；
- Handoff 15 个必填字段齐全可交接，缺字段被拒且**记为 Harness 错误**（事件 + 审计双留痕）；
- 闸门令牌：一次性、绑定线程、绑定草稿、绑定动作、过期失效 —— 并发下同一张令牌只有 1 次能消费成功；
- 看板四类筛选可用；签核界面 5 类要素齐全；4 种签核动作可执行；驳回必带原因且回灌；驳回原因 7 类；
- 审计只追加 + 哈希链可验伪，篡改一条能**定位到具体 seq**；Prompt 注入阻断 + GATE-07 + 安全记录；
- Dockerfile 六项运行契约（`tradedesk.api.app:app` / fastapi·uvicorn·httpx / `/health` / 8080 / UTF-8 / 库路径按项目根）全部对上。

**但"自证全绿"不等于合规**：`verify_s2.py` 的 21 个场景 + 101 个 pytest 没有覆盖下面任何一条。

## 二、违约项（按严重度）

### P0-1 · 推进阶段是写操作，却不校验闸门令牌

| | |
| --- | --- |
| 清单条款 | §4.1「写操作（发送 / **推进阶段**）必须带闸门令牌，无令牌一律拒绝」 |
| PRD 出处 | FR-ORC-09、PRD 8.9「工具默认只读」、NFR-SEC-04 |
| 位置 | `src/tradedesk/api/app.py:202`（`/threads/{id}/advance`）、`src/tradedesk/runtime/supervisor.py:429`（`Supervisor.advance`） |

`/advance` 是三个写接口里**唯一没有挂 `Depends(require_gate_token)`** 的，
`Supervisor.advance()` 也不消费令牌。复现：

```
POST /threads/T-2001/advance {"to_stage":"QUOTING"}     # 不带 X-Gate-Token
-> 200 {'ok': True, 'stage': 'QUOTING'}                   # 线程阶段已经变了
```

闸门令牌体系只覆盖了"签核"和"发送"两条路，阶段推进是条**裸奔的写路径**。
`GateTokenBody.action` 的注释里已经写了 `advance_stage`，说明设计时想到了，只是没接上。

最小改法：`advance` 端点加 `token: str = Depends(require_gate_token)`，
`Supervisor.advance` 增加 `token` 参数并按 `action="advance_stage"` 消费令牌
（`GateTokenService.consume` 已经支持 action 匹配，存储层不用动）。

### P1-1 · 无令牌的写请求被拒绝，但**不留痕**

| | |
| --- | --- |
| 清单条款 | §4.1「不带令牌调写接口 → 拒绝；**审计里有拒绝记录**」 |
| PRD 出处 | PRD 11.4「安全事件：注入检测、**越权尝试**、降级记录」 |

`require_gate_token`（`api/deps.py:55`）直接抛 403，**在进入 Supervisor 之前**，
所以审计表里一条拒绝记录都不会有。实测：审计条数 `0 → 0`。

现在的审计只覆盖"带了令牌但令牌不对"（`Supervisor.send/sign_off` 里有 `_audit`）。
**不带令牌的越权尝试恰恰是最该留痕的那一类，现在完全看不见。**

最小改法：把守卫改成"拒绝前先记审计"——在 `require_gate_token` 里拿不到 store，
改用 FastAPI 的全局异常处理器或在端点里先校验令牌再进业务。

### P1-2 · `action=""` 的令牌是**万能令牌**

| | |
| --- | --- |
| 清单条款 | §4.2 + 缺口 A「令牌要绑定动作」 |
| PRD 出处 | PRD 8.9、FR-ORC-09 |

`GateTokenService.consume` 的动作校验是：

```python
if token is not None and action and token.action and token.action != action:
```

`token.action` 为空串时这个条件直接短路 → **不做任何动作校验**。
而签发端 `GateTokenBody.action` 是自由字符串，默认 `"sign_off"`，**不校验取值**。
于是 `POST /gate-token {"action": ""}` 可以签出一张既能签核、又能**外发**的令牌。复现：

```
空 action 令牌 -> send : 200   (外发成功，sent_at 已写入)
sign_off 令牌 -> send : 403   ✅ 动作绑定本身是有效的
```

即：S2 收口补的"令牌绑定动作"是对的，但**留了一个绕过的门**。

最小改法：`GateTokenBody.action` 改成枚举（`sign_off` / `send_draft` / `advance_stage`），
非法值 422；`consume` 里改成 `token.action != action`（不再短路），
`issue` 时 `action` 为空直接拒绝签发。

### P1-3 · 幂等检查与建线程之间不是原子的 → 并发重复投递建出多条线程

| | |
| --- | --- |
| 清单条款 | FR-ORC-05「重复投递 10 次仅 1 次生效」 |
| PRD 出处 | PRD 8.2 |

`Supervisor.ingest` 的去重是"先 `list_threads()` 扫一遍、再 `create_thread()`"，
两步之间没有锁、没有唯一约束兜底。6 条并发同一条消息 → 建出 **2~5 条线程**（3 次连跑 3 次都复现）。

顺序投递 10 次确实只生效 1 次（这条过了），但**并发投递就漏**。
而 HTTP 层本来就是多线程的 —— 这正是验收清单 §三.3 要求专门查的场景。

最小改法：给 `threads` 表加 `(buyer_id, last_message_hash)` 唯一索引，
`create_thread` 撞唯一约束时按"重复消息"处理；或在 store 层加一个
`create_thread_if_absent(thread, message_hash)` 原子操作。

### P1-4 · 同一线程并发写 → 乐观锁冲突抛未捕获异常（500）

| | |
| --- | --- |
| 清单条款 | §三.3「S2 必须在其上做并发写入，不能假设单线程」 |
| PRD 出处 | FR-ORC-06、11.4 |

`store.save_thread` 的乐观锁冲突抛 `RuntimeError("并发冲突：…")`，
`Supervisor.sign_off / advance / ingest` 都**没有 try**，异常一路冒到 FastAPI → 500。
实测 6 个并发推进同一线程：`codes=['RuntimeError','RuntimeError','RuntimeError','RuntimeError',409,200]`
—— 一半请求 500。

并发冲突是**正常业务状态**（另一个请求刚改过），应该回 409 并让调用方重读，
不是 500。

最小改法：`save_thread` 的冲突改抛一个专用异常（如 `ConcurrentUpdate`），
Supervisor 捕获后返回 `{"ok": False, "code": "concurrent_update"}`，端点映射成 409。

### P1-5 · SLA 是**死代码**，Watchdog 永远扫不到东西

| | |
| --- | --- |
| 清单条款 | §2.3「超时任务自动升级率 100%」、§2.4「SLA 按业务类型可配置，配置项 ≥ 3 类」 |
| PRD 出处 | FR-ORC-02、FR-ORC-08 |

`SLA_DEFAULTS = {"first_response": 300, "quote": 3600, "followup": 86400}`
（`runtime/supervisor.py:36`）**除定义处外全项目零读取点**（已 grep 确认）；
`Config` 里也没有任何 `sla_*` 项；`build_thread` 不生成 deadlines，
`ingest` 也不挂 —— **接入后的线程 `deadlines` 恒为 `[]`**。

`sla_scan` 只扫 `t.deadlines`，所以：
**真实链路上超时自动升级率是 0，不是 100%。**
`verify_s2.py` 的场景 10 / 18 是**手工塞进去** `Deadline` 才跑起来的，把这个洞盖住了。

最小改法：`Config` 加 `sla_first_response_s` / `sla_quote_s` / `sla_followup_s`，
`build_thread` / `ingest` 按业务类型生成 `Deadline`（首响 / 报价 / 跟进各一条），
`sla_scan` 直接用配置阈值。

### P1-6 · 草稿只存在内存里 → 进程一重启，签核界面就空了

| | |
| --- | --- |
| 清单条款 | §4.4、FR-ORC-04 |
| PRD 出处 | PRD 8.3「跨天跨周续传，进程重启后可恢复未完成线程」 |

`Supervisor.drafts` 是内存字典（`runtime/supervisor.py:76`，注释里写着
「S2 未做草稿持久化；S4 接真检索后改为随事件落库」）。
换一个 Supervisor 读同一个库 = 模拟进程重启，实测：

```
重启后 GET /threads/T-2001/signoff-view -> draft_body = None
```

一个跨天等着人签核的线程，**重启后业务员看到的是空白签核界面**，
草稿正文、引用、置信度全没了。FR-ORC-04 在 S2 这一刀上是不成立的 ——
而 S2 恰好是做"跨天长事务"的那一刀。

最小改法：草稿随事件落库（`thread_events` 里已有 `draft.generated` 事件，
把草稿 payload 写进去，回放时取最后一条即可），`get_draft` 改成读事件而不是读内存。

### P1-7 · 线程号由 `len(线程数)` 推导，不是持久序列

| | |
| --- | --- |
| 清单条款 | §三.3（并发）、FR-ORC-04 |
| PRD 出处 | PRD 8.3 |

`Supervisor.__init__` 里 `self._id_base = 2000 + len(store.list_threads())`，
之后进程内自增。两个后果，都已复现（SQLite 与 PostgreSQL 同样复现）：

1. **多实例撞号**：两个 Supervisor 同时对着一个库（uvicorn 多 worker、多副本、
   两个 app 实例）→ 算出同一个号 → `UNIQUE constraint failed: threads.thread_id`（PostgreSQL 是
   `duplicate key value violates unique constraint "threads_pkey"`）。多一个进程就崩一次。
2. **库里有空洞时撞号**：删过线程（或从别的路径写入过非连续编号），
   `len()` 推出来的号会落到已存在的号上 → 同样 UniqueViolation。

最小改法：编号改由**存储层**发（`threads` 表加自增序列，或 `schema_meta` 里存一个
持久计数器 + `UPDATE ... RETURNING` 取号），进程内不再自己算。

### P2 项

| # | 条款 | 问题 | 位置 |
| --- | --- | --- | --- |
| P2-1 | §4.5 / FR-HUM-06 | 「修改后通过」不承载修改内容：`SignOffBody` 没有修改正文字段，人改的东西被静默丢弃，草稿不变（实测 `草稿变化=False`） | `api/app.py:29`、`runtime/supervisor.py:369` |
| P2-2 | §4.4 / FR-HUM-01、PRD 8.5 GATE-01 | 签核留痕只有 `{action, draft_id}`，缺**单价 / 币种 / 贸易术语 / 有效期 / 阶梯量**与**条款版本**（PRD 8.5 GATE-01 审计要求明确要记"签核人、时间、条款版本"） | `runtime/supervisor.py:368` |
| P2-3 | §4.8 / FR-ORC-06 | Agent 的动作被记成 `actor=system`，`Actor.AGENT` 全项目从未使用 → 回放里**分不清"人做的"还是"Agent 做的"** | `runtime/supervisor.py:88` |
| P2-4 | §1.6 | `route()` 返回了 `target="not_implemented"`，但 `ingest()` **不看这个分支**，直接 `raise RuntimeError("Inquiry 未注册")` → 500，而不是显式返回"本期未实现" | `runtime/supervisor.py:111` / `:277` |
| P2-5 | §3 | 15 个必填字段里有 7 个缺失时抛的是 pydantic `ValidationError` / `TypeError`，不是 `HandoffRejected` → 上层只 catch `HandoffRejected` 会漏（拒绝语义不统一） | `runtime/handoff.py:85` |
| P2-6 | §4.3 | 签核后 `last_activity_at` 不推进 → 看板"等待人审时长"把**已签核**的线程也算在等待里（实测签核前后时间戳相同） | `runtime/supervisor.py:398` |

## 三、复核方式

```powershell
# 审核脚本（SQLite）
$py = "D:\桌面\AIagent_project\外贸项目\.venv\Scripts\python.exe"
& $py scripts/audit_s2.py

# 审核脚本（PostgreSQL，脚本自建独立库并回收）
& $py scripts/audit_s2.py --dsn "postgresql://tradedesk:tradedesk_local_dev@127.0.0.1:5433/tradedesk_audit_b"
```

> PostgreSQL 模式下脚本会为每个 client 建一个独立数据库（对齐 WORKFLOW.md 第 4 节的双库隔离），
> 跑完自动 `DROP`。**不要**指向 `tradedesk_a` / `tradedesk_b`（会被 truncate）。

## 四、建议的处理顺序

1. **P0-1**（推进阶段闸门）—— 一处端点 + 一处 Supervisor，改动最小、风险最高，先补。
2. **P1-2**（万能令牌）—— 攻击面直接对着"外发"，和 P0-1 同属越权路径，一起做。
3. **P1-1**（拒绝留痕）—— 与 P0-1/P1-2 同批：守卫层改成"拒绝即留痕"。
4. **P1-3 / P1-4**（并发幂等 / 并发写 500）—— 需要动存储层，建议同一个切片。
5. **P1-5 / P1-6 / P1-7**（SLA 接线 / 草稿落库 / 线程号持久化）—— 触及跨天长事务的地基，
   建议在 S3 开工前一次性做掉，避免后面每加一个功能都踩一遍。
6. **P2 各项** —— 可合并成一个"签核与留痕细节"的切片。

## 五、给实现 agent 的一句话

`verify_s2.py` 94/94 和 pytest 101 全绿**只说明"实现 agent 自测覆盖到的点都对"**，
不代表合规。本轮 14 个根因里：

- **10 项自证脚本完全没碰**（P1-1、P1-2、P1-3、P1-4、P1-6、P1-7、P2-2、P2-3、P2-5、P2-6）；
- **4 项自证脚本碰到了，但断言方向是反的** ——
  场景 9 直接断言"无令牌推进阶段返回 200"是正确行为，
  场景 10 / 18 靠手工塞 `Deadline` 才让 SLA 跑起来，
  场景 13 只查签核令牌不能用于发送、没查空 action 令牌能用于发送。

补断言和补实现，两件事都要做 —— 补完实现，请把 `scripts/audit_s2.py` 也跑绿再交卷：

```powershell
& "$PWD\dev.ps1" audit                                          # SQLite
& "$PWD\dev.ps1" audit -Dsn "postgresql://tradedesk:tradedesk_local_dev@127.0.0.1:5433/tradedesk_audit_b"
```

（该命令**故意没有**并进 `dev.ps1 check` —— 现在它是红的。
等 R9-01~R9-14 全部修完，建议再把它折进 `check`，让越权路径从此变成交付门禁。）
