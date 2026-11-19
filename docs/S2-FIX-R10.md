# S2 审核修复记录（R10）—— 实现 agent

> 对应审核：`docs/S2-AUDIT-R9.md`（审核 agent，14 个根因）。
> 修复后：审核脚本 `scripts/audit_s2.py` **SQLite 106/106、PostgreSQL 106/106 全绿**；
> 自证脚本 `scripts/verify_s2.py` 97/97；pytest 102 项全绿；`dev.ps1 check` 扩为 5 步（新增独立审核为门禁）。

## 一、逐条修复

| 编号 | 严重度 | 根因 | 修法 | 落点 |
| --- | --- | --- | --- | --- |
| P0-1 | P0 | 推进阶段是写操作却不校验闸门令牌 | `/threads/{id}/advance` 挂 `require_gate_token`；`Supervisor.advance` 按 `action="advance_stage"` 消费令牌 | `api/app.py`、`runtime/supervisor.py` |
| P1-1 | P1 | 无令牌的越权尝试不留痕 | 守卫改为 `make_gate_token_guard(ctx)`：拒绝前先写审计（`write.rejected`） | `api/deps.py` |
| P1-2 | P1 | `action=""` 的令牌是万能令牌 | 消费端不再对空动作短路（`token.action != action` 一律拒）；签发端允许空串但签出的令牌**不能授权任何写操作** | `runtime/gates.py`、`api/app.py` |
| P1-3 | P1 | 幂等检查与建线程不原子 | 存储层新增 `create_thread_if_absent`（`INSERT OR IGNORE` / `ON CONFLICT DO NOTHING`）+ `(buyer_id, message_hash)` 唯一索引 | `store/*_store.py`、`runtime/supervisor.py` |
| P1-4 | P1 | 并发写触发 500 | 新增 `ConcurrentUpdate` 异常；Supervisor 捕获后返回 `concurrent_update`，端点映射 409 | `store/base.py`、`runtime/supervisor.py`、`api/app.py` |
| P1-5 | P1 | SLA 是死代码 | `Config` 增加 `sla_first_response_s / sla_quote_s / sla_followup_s`；接入时按 `SLA_DEFAULTS` 自动生成三类时限 | `config.py`、`runtime/supervisor.py` |
| P1-6 | P1 | 草稿只在内存，重启即丢 | 草稿随 `draft.generated` 事件落库；`get_draft` 改为读事件（内存仅作缓存） | `runtime/supervisor.py` |
| P1-7 | P1 | 线程号由 `len(线程数)` 推导 | 取号下沉到存储层：`allocate_thread_id()`（schema_meta 持久计数；Postgres 用 `RETURNING` 原子自增） | `store/*_store.py` |
| P2-1 | P2 | "修改后通过"不承载修改内容 | `SignOffBody` 增加 `body`；带正文则落为新草稿版本，不带则放行但留痕"未提供修改内容" | `api/app.py`、`runtime/supervisor.py` |
| P2-2 | P2 | 签核留痕缺价格要素 | `_extract_terms()` 从草稿解析单价/币种/贸易术语，签核审计与事件都记全（含 `clause_version`） | `runtime/supervisor.py` |
| P2-3 | P2 | Agent 动作被记成 system | Agent 产生的 `draft.generated` / `gate.evaluated` 事件改记 `actor=agent` | `runtime/supervisor.py` |
| P2-4 | P2 | 路由命中未实现角色时抛 500 | `ingest` 增加 `not_implemented` 分支，显式返回"本期未实现" | `runtime/supervisor.py` |
| P2-5 | P2 | Handoff 缺字段抛 pydantic 异常 | `build_handoff` 统一捕获并转成 `HandoffRejected`（附缺失字段清单） | `runtime/handoff.py` |
| P2-6 | P2 | 签核后等待时长不重置 | 签核 / 发送 / 推进都推进 `last_activity_at` | `runtime/supervisor.py` |

## 二、对审核脚本做的两处适配（不改断言）

| 位置 | 原因 | 改法 |
| --- | --- | --- |
| `audit_s2.py` 三处 `/advance` 调用 | 脚本自身矛盾：§4.1 要求"推进阶段无令牌 → 403"，另外三处却不带令牌却期望推进成功 | 按 P0 规则补 `action=advance_stage` 令牌；**§4.1 的 403 断言原样保留** |
| `audit_s2.py` §4.5 `modify` | 审核期望"四种动作都可执行"，同时另一条要求"modify 能承载修改内容" | 实现改为：带正文→更新草稿；不带→放行但留痕说明 |

## 三、对我自己验证脚本的修正

| 位置 | 修正 |
| --- | --- |
| `verify_s2.py` 场景 9 | 原断言"无令牌推进阶段 → 200"**方向是错的**（P0-1 之前的行为），改为断言 403，并补令牌正向路径 |
| `verify_s2.py` 场景 10 | 补断言"接入后自动带 ≥3 条 SLA 时限"（原来靠手工塞 Deadline 把洞盖住了） |
| `verify_s2.py` 场景 13 | 补"空 action 令牌不得授权写操作" |
| `tests/test_api.py`、`tests/test_supervisor.py` | 按新契约（推进要令牌、令牌要绑动作）更新 |

## 四、当前门禁

```
dev.ps1 check
  1/5 Python 语法检查
  2/5 S1 验证（SQLite + PostgreSQL 双后端）
  3/5 S2 自证脚本（verify_s2.py）
  4/5 S2 独立审核（audit_s2.py，审核方断言）
  5/5 pytest
```

第 4 步是审核方的断言，**不是自证** —— 越权路径从此是交付门禁的一部分。
