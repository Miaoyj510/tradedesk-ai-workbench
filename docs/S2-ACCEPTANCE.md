# S2 验收清单（审核 agent 用）

> S2 = Supervisor 路由 + Handoff 协议 + HTTP 接口（FastAPI）。
> 本清单由审核 agent 维护：实现 agent 交卷后逐条对照，缺一条就是一条缺陷。
> 条款来源全部标了 PRD 出处，不加私货。

## 一、功能必过条款

### 1. 意图路由（FR-ORC-01，PRD 8.2）

| # | 条款 | PRD 出处 | 验证方式 |
| --- | --- | --- | --- |
| 1.1 | 识别询盘五类（price/sample/spec/logistics/complaint）并决定进 Inquiry / Sentinel / 人工 | FR-ORC-01 | 喂 5 类语料，看路由结果 |
| 1.2 | 售前询盘**先 Sentinel 后 Inquiry** | 8.2 路由规则 | 检查调用顺序，不是事后过滤 |
| 1.3 | 名单命中**不进 Inquiry**，直接转人审 | 8.2 路由规则 | 断言 Inquiry 未被调用 |
| 1.4 | Inquiry 与 Sentinel 结论冲突时**以 Sentinel 为准**（风控优先） | 8.2 路由规则 | 构造冲突用例 |
| 1.5 | 同一买家不同线程：客户档案共享、线程状态独立 | 8.2 / FR-ORC-07 | 跨线程读写档案与线程 |
| 1.6 | 未注册角色被路由命中 → 返回"本期未实现"，**不静默降级** | 8.2 路由规则 | 断言有显式返回而非兜底 |
| 1.7 | Merch / Growth **只注册不调度**（本期仅声明） | 2.4 | 断言不会被路由到 |

### 2. Supervisor 其余组件（FR-ORC-02 / FR-ORC-08）

| # | 条款 | PRD 出处 | 验证方式 |
| --- | --- | --- | --- |
| 2.1 | 任务分发 Dispatcher | 8.2 | |
| 2.2 | 冲突仲裁 Arbiter | 8.2 | |
| 2.3 | SLA 监控 Watchdog + 超时升级 Escalation | FR-ORC-02 | 超时任务自动升级率 100% |
| 2.4 | SLA 按业务类型可配置（首响 / 报价 / 跟进），**配置项 ≥ 3 类** | FR-ORC-08 | 数配置项 |
| 2.5 | 超时**分级**升级：业务员 → 经理 | FR-ORC-02 / FR-HUM-09 | 断言升级链路可达 |

### 3. Handoff 协议（PRD 8.4）

必填 15 项，**缺任一项即拒绝并记为 Harness 错误**（PRD 8.4 校验规则）：

`from_agent` `to_agent` `thread_id` `buyer_id` `handoff_reason`
`buyer_profile_digest` `committed_items` `open_questions` `deadlines`
`stage` `evidence_refs` `risk_level` `language` `idempotency_key` `pending_decision`

> ⚠ 已知缺口（审核 R1 发现，已记在下面"待定项"）：当前 `domain/models.py` 的
> `Handoff` 只有 13 个字段，**缺 `thread_id` 与 `buyer_id`**；
> 字段名用的是 `reason`（PRD 写的是 `handoff_reason`）。
> S2 的 Handoff 校验必须覆盖 PRD 的 15 项，否则 FR-ORC-06 / 11.4 不成立。

### 4. HTTP 接口（FR-ORC-09 / FR-ORC-10 / FR-HUM）

| # | 条款 | PRD 出处 | 验证方式 |
| --- | --- | --- | --- |
| 4.1 | 写操作（发送 / 推进阶段）**必须带闸门令牌**，无令牌一律拒绝 | FR-ORC-09 / PRD 8.9 工具默认只读 | 不带令牌调写接口 → 拒绝；审计里有拒绝记录 |
| 4.2 | 令牌一次性、绑定 thread_id + draft_id、不可伪造 | 8.5 闸门令牌 | 复用令牌 → 第二次失败 |
| 4.3 | 线程看板可按**阶段 / 语种 / 风险等级 / 等待人审时长**筛选（4 类） | FR-ORC-10 | 四种筛选各测一次 |
| 4.4 | 签核界面呈现 5 类要素：草稿全文、引用证据、风险项、承诺台账、置信度 | FR-HUM-05 | 接口返回这 5 项 |
| 4.5 | 签核 4 种动作：通过 / 驳回 / 修改后通过 / 转交他人 | FR-HUM-06 | 四种都可执行 |
| 4.6 | 驳回必须带原因，**回灌 Agent** | FR-HUM-06 | 驳回后 Agent 能拿到原因 |
| 4.7 | 驳回原因分类 ≥ 5 类 | FR-HUM-07 | 枚举数够 5 类 |
| 4.8 | 人审动作与 Agent 动作**同序留痕**，可按线程回放 | FR-HUM-08 / 11.4 | 回放接口返回有序事件流 |

### 5. 留痕与审计（PRD 11.4）

| # | 条款 | PRD 出处 | 验证方式 |
| --- | --- | --- | --- |
| 5.1 | 审计**只追加、不可篡改**，哈希链可验伪 | 11.4 | 篡改一条 → 定位到该 seq |
| 5.2 | 可按 `thread_id` **完整回放** | 11.4 / FR-HUM-08 | 回放接口 |
| 5.3 | 阶段迁移记录：触发事件、操作者（人/Agent）、时间戳、前后状态 | FR-ORC-06 | 迁移日志完整率 100% |
| 5.4 | 越权回复抑制：Sentinel 命中 high 时 Inquiry 输出被抑制（不是事后过滤） | PRD 8.9 / FR-EVA-07 | 命中场景外发内容 = 0 |
| 5.5 | Prompt 注入命中 → 阻断 + GATE-07 + 转人工 + 安全记录 | PRD 8.9 | |

## 二、审核方会怎么验

1. **不只跑 `verify_s2.py`**：审核方自己按上表重写一遍断言，重点是上表没有、
   但实现里容易漏的边界（幂等、并发、越权）。
2. **双后端**：SQLite 与 PostgreSQL 各跑一遍（`dev.ps1 check -Dsn ...`）。
3. **并发**：HTTP 接口是多线程的。R2 已把存储层改成线程局部连接 + 取号加锁；
   S2 必须在其上做并发写入（审计、事件、令牌消费），不能假设单线程。
4. **越权路径优先看**：不带令牌 / 令牌错绑 / 令牌复用 / 高风险名单命中，
   这四条是"人在环"的地基，出一条就是 P0。
5. **PRD 偏离**：实现与 PRD 表述不一致时，以 PRD 为准；要改就两个 agent 一起确认
   （WORKFLOW.md 第 7 节）。

## 三、已修的地雷（审核 R1/R2，避免重复踩）

| 编号 | 问题 | 状态 |
| --- | --- | --- |
| R1-1 | 金额阈值闸门只认"USD 8000"，币种挪到数字后就绕过 | 已修（agent-b） |
| R1-2 | GATE-10 / GATE-11 漏标"强制人工签核" | 已修（agent-b） |
| R1-3 | 验证清理与 pytest 直连共享库，打破双 agent 数据库隔离 | 已修（agent-b） |
| R1-4 | requirements.txt 漏 psycopg | 已修（agent-b） |
| R2-1 | 存储层并发写入丢数据、审计哈希链会断 | 已修（agent-b） |
| R2-2 | SQLite 单连接跨线程共用会踩事务 | 已修（agent-b，线程局部连接 + 写锁） |

## 四、待定项（要实现 agent / 用户拍板）

1. **Handoff 校验严格程度**：PRD 只说"缺任一必填字段即拒绝"。
   `evidence_refs` / `committed_items` 允许空列表，还是必须非空？
   —— 建议：**字段必须存在**（不允许 None），但列表允许为空；
   `stage` / `risk_level` / `language` / `idempotency_key` 等枚举与标量必须非空。
2. **Handoff 字段命名**：跟 PRD 写 `handoff_reason`，还是保持模型里的 `reason`？
   —— 建议：模型加 `handoff_reason` 字段，`reason` 保留为别名以免打断已有调用。
3. **闸门"必须逐条签核"的 6 条**（GATE-01/02/03/04/10/11）在 S2 的签核接口里，
   是不是要区别于其余 5 条的处置方式（转人工 / 提示 / 允许追问）？
   —— 影响"一次授权窗口批量签核"（FR-HUM-10）能不能碰高风险项。
