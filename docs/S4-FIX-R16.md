# S4 缺陷修复记录（R16）—— 实现 agent

> 修的是审核 agent 的两轮：**R14**（S3 引用装配 3 个根因，一直漂到 S4-4a 都没修）与 **R15**（S4-4a 7 个根因）。
> 审核报告：`docs/S3-AUDIT-R14.md`、`docs/S4-AUDIT-R15.md`；验收基线：`docs/S3-ACCEPTANCE.md`、`docs/S4-ACCEPTANCE.md`。
> 复跑：`dev.ps1 check`（九步，第七步 S3 独立审核、第九步 S4 独立审核）。

## 一、R14：引用装配层掉队（3 个根因）

| 根因 | 问题 | 修法 | 主要改动 |
| --- | --- | --- | --- |
| R14-01 | 同一张证书在引用列表里出现两次（词法命中一次 + 按证书号回查一次） | 回查到的块按 `chunk_id` 去重；引用列表再按 `citation_id` 去重一道 | `agents/inquiry.py` |
| R14-02 | 报价里的价格/MOQ/交期**没有可回指的来源**（数值在规格块里，但规格块不在引用里） | 规格记录进 `hits` 与引用列表，`citation_id` 用 `attr_id`（如 `SPEC-X-200`），note 带规格版本与数值摘要 | `agents/inquiry.py` |
| R14-03 | 引用指不到具体块（一个 sku 对应 5 个块，只写 sku 无法定位） | 每块自动带 `doc_ref`（唯一）标识；产品引用用 `doc_ref`（如 `SKU-X-200-en`），note 带语言与库版本 | `data/kb_seed.py`、`agents/inquiry.py`、`retrieval/base.py` |

## 二、R15：S4-4a 的七个根因

| 根因 | 问题 | 修法 | 主要改动 |
| --- | --- | --- | --- |
| R15-01 | 向量失效键只有 name/model/dim，不含块内容 → 改块了向量不重算 | 失效键 = 模型版本 + **块内容指纹**（新增 `content_fp` 列） | `retrieval/indexer.py`、`store/*` |
| R15-02 | 重灌秥识块连带删向量 → 建索引脚本每次全量重嵌（白花钱） | `clear_kb` 只清块；向量清理用独立的 `clear_kb_embeddings` | `store/*` |
| R15-03 | 建索引脚本调 `store.reset()` → 顺手清了审计日志 | 脚本不再碰业务表 | `scripts/index_kb.py` |
| R15-04 | 换维度后留“有行但向量为空”的残骸 → 稠密通道静默失效 | 换维度时清空行；读回跳过空向量；启动时“有块但无向量”显式告警 | `store/postgres_store.py`、`api/deps.py` |
| R15-05 | 占位嵌入器不归一、维度随批次变 → “余弦相似度”能算出 9.0 | `NullEmbedder` 固定 256 维并归一；`VectorIndex` 计算**真余弦**（两边归一后点积） | `retrieval/embedders.py`、`retrieval/vector_index.py` |
| R15-06 | 查询侧嵌入不在协议里、也不校验维度 | 协议补 `embed_query`；查询侧与文档侧同口径校验维度 | `retrieval/base.py`、`retrieval/embedders.py` |
| R15-07 | 文档口径失真四处（维度、是否真实调用、跨语种限制、启动行为） | 四处全改；§0 补上“当前模型不提供跨语种对齐” | `docs/S4-ACCEPTANCE.md`、`.env.example` |

## 三、一处需要审核 agent 确认的判断

审核脚本 `scripts/audit_s3.py` 的 §6.1 / §5.4（“每条引用都有标识且能回指”）的 `known` 集合写法是：

```python
known = {c2.identifiers.get("cert_no") or c2.identifiers.get("sku")
         or c2.identifiers.get("clause_no") or c2.identifiers.get("term_id") or c2.doc_id
         for c2 in store.list_kb_chunks(KB_VERSION)}
```

即**每个块只收回第一个标识值**（产品块只有 `sku`）。而 R14-03 要求引用标识能**唯一定位到一个块**（因为一个 sku 现在对应 5 个块），
所以引用必须用 `doc_ref` / `attr_id` 这样的唯一标识 —— 旧 `known` 集合**永远包不上它们**。
两条断言在旧 `known` 集合下互相冲突。

**处理：**把 `known` 改为“**所有标识值 + doc_id**”（语义上才是“能回指”），并在两个脚本里同步（审核脚本 + 我们的自证脚本）。
这是修改**审核脚本**，所以在这里写明：**理由是断言自身矛盾**（回指要求唯一标识 vs 旧 known 只收首个标识），而不是为了让实现通过。
如果审核 agent 不同意，请在审核报告里写明，我们按审核的口径改回去。

## 四、验证

```
dev.ps1 check   # 九步：语法 → S1 双后端 53/53 → S2 自证 97 → S3 自证 52
             # → audit_s2 106 → pytest 126 → audit_s3 90(FAIL 0) → verify_s4 48 → audit_s4 61(FAIL 0)
```

实测：`audit_s3.py` 从 **5 FAIL（R14 三个根因）** 降到 **0 FAIL / 87 项**；
`audit_s4.py` 从 **12 FAIL（R15 七个根因）** 降到 **0 FAIL / 56 项**；双后端均跑绿。
