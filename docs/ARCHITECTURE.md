# 一页架构（TradeDesk-AI · 外贸业务员的 AI 数字员工工作台）

> 与代码一一对应；图里的每个方框都能在 `src/tradedesk/` 里找到同名模块。
> 本期交付范围：PRD 第 1 章第 22 行 —— **询盘应答 Agent + 多语种知识库 / 交易风控 Agent + 人工签核闸门 / 调度器路由与询盘线程状态机**；
> Merch、Growth 两个角色**只声明不实现**（图中用虚线标出）。

---

## 一、三层能力总览

```mermaid
flowchart TB
  subgraph H["③ 多 Agent 编排运行时（Harness）"]
    direction TB
    SUP["Supervisor 调度器<br/>意图路由 · 冲突仲裁 · SLA 监控 · 超时升级"]
    SM["询盘线程状态机<br/>8 状态 · 跨天跨周断点续传"]
    GATE["人审闸门 11 条 + 闸门令牌<br/>写操作与发送分离"]
    REL["发布与灰度<br/>四类发布单元 · 按企业分桶 · 分类别回滚"]
    HAND["Handoff 15 字段契约<br/>买家档案 · 已承诺事项 · 未决问题 · 时限"]
  end

  subgraph A["② 单 Agent 层（自主执行）"]
    direction TB
    EVA["Inquiry 询盘应答 Agent<br/>Eva：意图识别 / 产品匹配 / 草稿生成"]
    SEN["Sentinel 交易风控 Agent<br/>Grace：名单核验 / 风险分级"]
    MERCH["Merch 商品运营<br/>仅声明（工具接口存在，无实现）"]
    GROW["Growth 推广<br/>仅声明（工具接口存在，无实现）"]
  end

  subgraph R["① RAG 检索增强层"]
    direction TB
    KB["多语种产品与合规知识库<br/>zh / en / es / ar · 200 概念术语表"]
    PIPE["三通道 RRF 管线<br/>词法 + 结构化 + 稠密"]
    EMB["嵌入 BGE-M3<br/>重排 bge-reranker-v2-m3"]
  end

  subgraph S["存储与留痕"]
    direction TB
    ST[("SQLite / PostgreSQL<br/>同一套接口，换后端零改动")]
    AUD["审计哈希链<br/>append-only · 可验伪"]
    LLM["云端模型层<br/>5 家厂商可切换"]
  end

  EVA --> PIPE --> KB
  PIPE --> EMB
  SEN --> KB
  SUP --> EVA
  SUP --> SEN
  SUP --> SM
  SUP --> GATE
  SUP --> HAND
  EVA --> LLM
  EVA -.仅声明.-> MERCH
  EVA -.仅声明.-> GROW
  SUP --- REL
  SM --> ST
  GATE --> ST
  AUD --- ST
  HAND --- ST
```

**红线**：Agent 只产出**草稿**，报价与合同必须人工签核；不自动签合同、不自动定价、不自动付款。

---

## 二、一次询盘的处理时序

```mermaid
sequenceDiagram
  autonumber
  participant B as 买家（多语种来信）
  participant API as FastAPI
  participant SUP as Supervisor
  participant SEN as Sentinel（风控）
  participant EVA as Inquiry（Eva）
  participant RAG as 检索层
  participant H as 人审闸门
  participant H2 as 业务员

  B->>API: 询盘消息（en/es/ar，含图片与附件）
  API->>SUP: ingest()
  SUP->>SUP: 幂等校验（同买家同消息不重复处理）
  SUP->>SUP: 状态机建线程（INQUIRY）
  SUP->>SUP: 灰度分流（按企业分桶 → 决定用哪一版知识库）
  SUP->>SEN: 风控前置（先风控，后应答）
  alt 命中名单 / 高风险
    SEN-->>SUP: HIGH
    SUP-->>API: 阻断，不产生任何对外内容，转人工
  else 通过
    SEN-->>SUP: LOW / MID
    SUP->>EVA: 交Inquiry（含买家档案与时限）
    EVA->>RAG: 意图 + 产品 + 规格 + 证书
    RAG-->>EVA: 证据（可回指到证书号 / 名单条目 ID / 法规条款）
    EVA-->>SUP: 草稿 + 意图标签 + 置信度 + 引用 + 建议动作
    SUP->>H: 评估 11 条闸门
    H-->>API: 待签核 / 需人工确认
    H2->>API: 签核（通过 / 修改 / 驳回 / 转交）
    API->>API: 闸门令牌一次性消费 → 发送
  end
```

---

## 三、询盘线程状态机（跨天跨周的长事务）

```mermaid
stateDiagram-v2
  [*] --> INQUIRY: 首次来信
  INQUIRY --> SAMPLING: 打样
  SAMPLING --> QUOTING: 报价
  QUOTING --> ORDERED: 订单
  ORDERED --> DOCS: 单证
  DOCS --> PAYMENT: 收款
  INQUIRY --> CLOSED: 关闭
  QUOTING --> CLOSED: 关闭
  ORDERED --> CLOSED: 关闭
  INQUIRY --> LOST: 流失
  QUOTING --> LOST: 流失
  ORDERED --> LOST: 流失
  CLOSED --> [*]
  LOST --> [*]
```

- **断点续传**：线程、事件、审计、闸门令牌全部落库，进程重启后从上次位置继续（PRD 4.3）。
- **乐观锁**：并发更新撞版本号 → 409 而不是静默覆盖。
- 单证 / 收款本期只保留状态位（本期不做）。

---

## 四、检索管线：三个难点的工程落法

```mermaid
flowchart LR
  Q["买家提问<br/>（多语种）"] --> R1["① 跨语种召回<br/>多语种嵌入 + 术语表扩展<br/>西语提问命中中文产品库"]
  R1 --> R2["② 合规时效过滤<br/>按「买家所在国 + 当前日期」<br/>过期证书 / 废止条款被剔除"]
  R2 --> R3["③ 结构化 + 非结构化混合<br/>规格走属性过滤<br/>证书与话术走语义召回"]
  R3 --> RRF["三路 RRF 融合 → 统一重排"]
  RRF --> OUT["证据（每条可回指）<br/>→ 草稿里的引用标签"]
```

> 检索分**两层口径**：公开检索基准（MIRACL / MLDR / Mr. TyDi / mMARCO / MKQA）只证明"多语种检索管线没问题"；
> 询盘场景做得好不好，由**自建多语种询盘语料层**单独评测。两者不混算（PRD 9.5）。

---

## 五、灰度与回滚（S6-3，PRD 13.4.8）

```mermaid
flowchart TB
  subgraph PUB["发布侧"]
    REL["发布清单<br/>app / agent / knowledge / sanctions 四类单元<br/>六个组件版本号"]
    REL --> EV["审批人 + 变更说明 + 回滚预案（必填）<br/>Agent 变更要回归证据 · 名单变更要 L-6 证据"]
    EV --> ST1["PILOT → P20 → P50 → P100<br/>每档观察 ≥ 1 个工作日"]
  end

  subgraph RUN["运行侧"]
    BK["按企业分桶（纯函数 · 不存分流表）<br/>同一企业所有询盘命中同一版本"]
    DI["双索引：块/向量主键 = (chunk_id, kb_version)<br/>同名块在两个库版本下并存"]
    AL["别名切换：回滚只改指向，不重建"]
  end

  ST1 --> BK --> DI --> AL
  AL --> RB["分类别回滚<br/>回滚 = 改别名指向<br/>不删数据 · 不改历史询盘"]
```

**三个容易做错的地方**（本项目实际踩过并已修）：

1. 块与向量主键若只有 `chunk_id`，建新版本会顶掉旧版本 → 双索引根本不成立；
2. 灌基线若整库清空，重启一次灰度就没了；
3. `index_version` 若只是清单里的一个字段没人消费，改了等于没改。

---

## 六、模块地图

| 层 | 模块 | 职责 |
| --- | --- | --- |
| domain | `state_machine` `agentspec` `models` `enums` | 8 状态迁移、角色契约、领域模型（纯函数，无 IO） |
| runtime | `supervisor` `gates` `handoff` `watchdog` | 路由与仲裁、11 条闸门与闸门令牌、15 字段交接、SLA 扫描 |
| agents | `inquiry` `sentinel` `draft_writer` `prompts` `registry` | Eva、Grace、模型出稿、提示词资产、角色注册表 |
| retrieval | `pipeline` `lexical` `indexer` `store_retriever` `embedders` | 三通道 RRF、向量索引、按库版本取块的检索器 |
| release | `manifest` `bucketing` `service` | 发布清单、按企业分桶、推进与分类别回滚 |
| api | `app` `deps` `auth` `workbench` | HTTP 接口、依赖装配、本地账号会话、读模型 BFF |
| store | `base` `sqlite_store` `postgres_store` | 同一套接口，换后端业务代码零改动 |
| llm | `providers` `factory` `chat` `pricing` | 5 家厂商可切换、成本按来源与取数日期维护 |