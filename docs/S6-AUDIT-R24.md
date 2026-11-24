# S6 审核 R24 —— 作品集包装 / 前端 Agent 抽屉 / S6-5 知识库导入

> 审核方：agent-b（独立核验，不替实现方改代码）。日期：2026-11-24。
> 轮次说明：R22/R23 已用于 S6-3 发布灰度回滚审核与修复，本轮顺延为 **R24**。
> 方法：读代码 + 实跑脚本 + 临时库 probe；**在制品（未提交工作区）与已提交切片分开判定**，
> 中间态已修的不算交付缺陷，但要点名"已提交版本里存在过"。

## 〇、审核对象与范围

| 对象 | 基线 | 状态 |
| --- | --- | --- |
| 作品集包装（PORTFOLIO / ARCHITECTURE / DEMO-SCRIPT / README 摘要 / 3 张截图） | `agent-a @ e36a577` | 已提交，未合 main |
| 前端 Agent 契约抽屉 | `agent-a @ f1e80f9` | 已提交；两处缺陷在**未提交工作区**已修 |
| /ui 静态资源 no-store | `agent-a @ c36e377` | 已提交 |
| S6-5 知识库导入（`data/kb_import.py` + `/kb/import`、`/kb/versions/{v}/index` + 知识库页 UI） | `agent-a @ c36e377` + **未提交工作区** | 在制品，19 单测绿 |
| 对照基线 | `main @ 5ec5d2a` | check 十三步全绿（本轮实跑） |

不在本轮范围：S6-2 镜像、S6-4 登录（沿用 R21 结论）；S6-3 机制本身（R23 已闭环）。

## 一、根因清单（13 条：P1×3 / P2×7 / P3×3）

### R24-01（P1）S6-5：显式 `kb_version` 可静默改写**已发布**版本 —— R23-02 在新路径复发

`/kb/import` 把 `body.kb_version` 直通 `import_kb()` → `store.upsert_kb_chunk()`
（复合主键 `(chunk_id, kb_version)` 的 `ON CONFLICT DO UPDATE`）。**没有任何守卫**检查
目标版本是否已存在 / 已发布。模块 docstring 宣称"导入产出的是一个**新的** kb_version，
不覆盖旧版本"——只对**自动版本号**成立。

**probe 实锤**（临时 SQLite 库，非开发库）：

```
基线块 kb-live / KB-P-X200-spec：price_low = 4.85
import_kb(kb_version="kb-live", dry_run=False, content=<改价 CSV>)
→ price_low = 0.01，written = 1
→ list_releases() = 0（无发布记录）；审计动作仅 ['kb.import']
```

后果：结构化通道**立即**按被改写的价格服务稳定版买家；无灰度、无观察窗、无回滚路径
（回滚只认发布台账）；旧向量还在 → 词法/结构化与稠密通道给出**两个版本的事实**。
鉴权默认关（`TRADEDESK_AUTH_REQUIRED=0`），任何调用者可触发；开启后也只需 `release` 能力。
UI 不传 `kb_version`（走自动版本），所以目前仅 API 可达——但 API 就是生产路径。

**测试盲区**：`tests/test_kb_import.py::test_import_leaves_other_versions_untouched`
只测自动版本号路径；显式版本覆盖路径**零断言**（R23-01 教训：断言要测生产路径）。

修法建议：显式 `kb_version` 若已存在 → 422 拒绝（"改已发布版本请走发布流程"）；
或强制导入只产生新版本，显式参数仅用于**不存在**的版本号。补一条覆盖路径的活断言。

### R24-02（P1）知识库页写死"200 概念"——实际术语表 2 块，PRD 硬要求未达标却被显示成已达标

`ui-prototype/knowledge.html:90`：`<span class="hint">200 概念 × zh/en/es/ar（含禁用译法）</span>`
是**静态 HTML，无 id，真数据模式永不覆盖**（R21-03 模式）。
实际：`src/tradedesk/data/kb_seed.py` 术语表块仅 `KB-T-MOQ`、`KB-T-LEADTIME` **2 块**；
mock `data.js` 5 条。PRD FR-KB-02（349 行）要求 **≥200 概念**——未达标。
作品集截图 3 原样暴露该标签；`ARCHITECTURE.md`（"200 概念术语表"）与
`DEMO-SCRIPT.md`（教演示者说"右边是 200 个概念的四语术语表"）继续传播。

修法建议：标签改为读模型实数（`terms.length`）或明写"演示 2 概念 / PRD 要求 ≥200（未达标）"；
两份演示文档同步改口。**宁可显示"未达标"，不要写死"已达标"**（R21 纪律）。

### R24-03（P1）知识库页写死"BGE-M3 / bge-reranker-v2-m3"——实际 embo-01，reranker 零消费

`ui-prototype/knowledge.html:49` 静态写死嵌入/重排模型名。实际：
`config.embedding_model = "embo-01"`（云端，1536 维实测）；`reranker_model` 只在
`config.py:60/136` 声明，`retrieval/`、`agents/` **无任何消费点**——R23-04"装饰性组件"模式。
`ARCHITECTURE.md` 一层总览图 EMB 盒重复此声明，且卷首自称"图里的每个方框都能在
`src/tradedesk/` 里找到同名模块"（BGE-M3 / bge-reranker 找不到）；四层图"统一重排"亦无实现
（RRF 是融合，不是重排模型）。截图 3 顶栏原样暴露。

修法建议：顶栏改读 `config`/读模型的真实嵌入标签（含"占位/未启用"态）；
架构图 EMB 盒改 embo-01 并删重排声明，或明写"重排为 PRD 原定、本期未实现"。

### R24-04（P2）f1e80f9：抽屉关闭按钮/遮罩**死绑**（未提交工作区已修，需复核）

`f1e80f9` 的 `initAgentRail()` 在**页面加载时**绑 `#agent-drawer-close` 与遮罩 click，
但抽屉是**首次点击时**才 `createElement` → 绑定永远落空，叉号与遮罩点不动（仅 ESC 有效）。
未提交工作区已把绑定移进 `ensureAgentDrawer()`（跟着创建走），修法正确。
**已提交切片里该缺陷存在过**；合并前按工作区版本复核一次开关路径。

### R24-05（P2）f1e80f9：只接了 index.html，与提交信息"三个页面共用"不符（工作区已修）

`f1e80f9` 仅 `index.html` 四个按钮加 `data-agent-id`；`dashboard.html` / `knowledge.html`
的四个按钮仍是死按钮（提交信息宣称三页共用一份实现）。未提交工作区已给三页都接上
（各 4 个 `data-agent-id`，实测）。同 R24-04，合并前复核。

### R24-06（P2）渲染层审核**未覆盖抽屉**——R21 教训在新切片上重演（未修）

`scripts/audit_s6_ui.js` 15 项断言无一项触发抽屉：DOM 桩的
`querySelectorAll('[data-agent-id]')` 返回 `[]`，`initAgentRail` 空转，
`agentPanelHtml` / `openAgentDrawer` / 关闭路径**一次都没被跑过**。
"新代码对 DOM 桩无害"≠"新代码被验证过"。提交信息以"渲染层 15/15 仍绿"作为验证依据，
但那 15 项测的是旧渲染面。

修法建议（审核方可代补断言，实现方亦可）：桩里造 4 个 agent 按钮 → 点开 →
断言仅声明角色出现"本期未实现/永不真正执行"、已实现角色出现"绝不自动对外发送"、
scope/工具名带 `<script>` 载荷时被转义、关闭后 `aria-hidden=true`。

### R24-07（P2）S6-5：审计写失败被**静默吞掉**，与发布链路纪律不一致

`kb_import.py` 落库后的 `append_audit` 包在 `try/except Exception: pass` 里；
对照 `release/service.py:239/321/361` 同类调用**不吞异常**。
导入已 commit 而留痕丢失 = 不可追责，且与"append-only 审计哈希链"的对外主张矛盾。
修法建议：去掉 swallow（让失败显式），或至少把失败写进 report.warnings 并返回告警。

### R24-08（P2）S6-5：占位建索引在 PostgreSQL 上必失败——端点注释的"结构上能跑通"只成立于一端

`/kb/versions/{v}/index` 无嵌入器时用 `NullEmbedder`（`dim=256`，`embedders.py:266`）；
PG 向量列按启动 config 建为 `vector(1536)`（`postgres_store.py:652-662`），
**vector(N) 拒绝错误维度**（CLAUDE.md 已录坑）→ 插入即报错 → 502 `index_failed`。
SQLite 侧能跑。端点 docstring"没有真嵌入器时用占位嵌入器建（结构上能跑通链路）"
在 PG 部署上不成立；知识库页"为该版本建索引"按钮在 PG + 未配嵌入器时点了就 502。
修法建议：占位路径按 embedder.dim 重建列（store 已有维度变更重建逻辑），
或 PG 上明确拒绝占位建索引并返回可读原因；双后端各补一条断言。

### R24-09（P2）作品集证据表三处数字与仓库不符

| 文档原话 | 实测 | 出处 |
| --- | --- | --- |
| "自证脚本…S5 54"（PORTFOLIO 三节 / README 证据表） | `verify_s5.py` = **28 项**；54 是**独立审核** `audit_s5.py` | 本轮实跑两者 |
| "验证与审核脚本 9.7k 行"（README） | `e36a577` 时 scripts/*.py+js = **10100 行** | git ls-tree 逐文件 wc |
| "13 项生产化差距…9 项未做、2 项部分做"（PORTFOLIO 五节 / DEMO-SCRIPT 收尾） | PRD 13.4.12 表 = **12 行**：8 未做 + 2 部分 + 2 未闭环（未确认/未定稿） | PRD 1820-1831 行 |

其余数字本轮复核**一致**：pytest 262；自证 S1 53 / S2 97 / S3 52 / S4 60 / S6-3 91；
审核 S2 106 / S5 54 / S6 49 / S6-3 41（SQLite）——以上均本轮实跑；
源码 9722≈9.7k、单测 2959≈3.0k（逐文件 wc）。
**沿用口径、本轮未复跑**：S6 渲染层 15 项（R21 基线）、S6-3 PG 追加 26 项（R23 基线）、
S3 90 / S4 78(SQLite)/85(PG)。"67 = 41+26"的合计口径与 R23 报告一致。

### R24-10（P2）角色命名与角色数量违反 PRD 自有口径

- README 摘要与 ARCHITECTURE（EVA/SEN 盒、模块地图）用 **Eva / Grace** 指代本项目
  Inquiry / Sentinel。PRD 第 27 行（"必读"）与 2.4 明写：Eva/Grace 是**阿里国际站**
  「AI 生意助手 2.0」的命名，**不作为本项目角色名**；且作品集自称"与任何对标产品名称不重合"。
  （PRD 1527 行甘特图自己也用了 Eva/Grace——PRD 内部不一致，以 27 行命名说明为准。）
- "拆给**五个** Agent（含客户背调）"（PORTFOLIO 首段 / README / DEMO-SCRIPT 0:00 话术）：
  PRD 2.4 角色清单为 **4 个**（Inquiry/Sentinel 实现 + Merch/Growth 声明）；
  客户背调是任务不是本期角色（PRD 261 行：全网背调列后续版本）。

### R24-11（P2）PORTFOLIO.md 截图相对路径裂图

文件在 `docs/PORTFOLIO.md`，图片写 `![](docs/screenshots/ui_01_workbench.png)` →
GitHub 解析为 `docs/docs/screenshots/…` → 404。README（根目录）同写法是对的。
修法：PORTFOLIO 内改 `screenshots/…`。三张 PNG 本身合法（1570×905，内容已目视核验）。

### R24-12（P3）口径与细节一组

1. PORTFOLIO 追问"多语种怎么保证不串＝术语表+**跨语种嵌入**+查询扩展"：实测 embo-01
   跨语种余弦 ≈ -0.02（CLAUDE.md），跨语种靠**术语表+四语分块**；"跨语种嵌入"作为保证不成立。
2. PORTFOLIO 追问"模型写出数字→**整段丢弃**"：R20 后是**分层白名单**（币种位置须在 KB 真值、
   其他位置可为买家原话数字）+ 可选占位符代码补齐；"整段丢弃"口径过期。
3. DEMO-SCRIPT"audit6r（67 项断言）"：不带 `-Dsn` 实跑 **41 项**；67 需双后端。
4. `make_kb_version` 含 `YYYY.MM` → "同内容同版本（幂等重导）"**跨月即破**（注释口径过期）。
5. 导入路径不校验 PRD 9.3"产品条目文本块不得含数值"（种子守了，导入没守）。
6. `TIMED_DOC_TYPES` 含"名单条目"，但 Sentinel 筛查读名单表**不读 KB**——导入名单块
   不会带来任何筛查能力，易误导（建议导入时拒收或明写提示）。
7. 抽屉 `z-index:60` 写死（`--z-drawer:40` 变量闲置），高于 toast(50)：
   `/agents` 失败的 toast 被遮罩挡住。
8. `f1e80f9`/`c36e377` 版本号只 bump 了 index.html（v1.0.1），dashboard/knowledge 页脚仍 v1.0。

### R24-13（P3）NEXT.md 状态卡自相矛盾（文档卫生）

§三之三 R23 表仍标"❌ 未修"（270-274 行）vs 顶部 41/43 行"✅ 已全部修完"；
261 行"断言 41 项"vs 41/49 行"67 项"（应写明 41=SQLite、67=双后端合计）；
页首日期仍 2026-11-21。CLAUDE.md 口径是对的，状态卡没跟上。

### 流程项（不计根因）：S6-5 尚无验收判据文档

`docs/` 无 S6-5 判据/验收基线。项目规矩（R23 做法）：**判据先于实现写定**。
在制品阶段可补，但合并前应有 `docs/S6-5-ACCEPTANCE.md`（或并入 S6-ACCEPTANCE）+ 审核断言进 check。

## 二、已核验且一致（本轮实跑/目视）

- **main @ 5ec5d2a `dev.ps1 check` 十三步全绿**（含双后端与 S6-3 独立审核 41/41）。
- **S6-5 单测 19 passed**；干跑默认、确认按钮门控、建索引显式+占位披露、mock 拒导入、
  数值进 extra、证书缺有效期拒收、CSV 错位拒收、同版本重复 ID 拒收、自动版本共存——设计正确。
- **抽屉契约数据与后端一致**：mock 4 角色 scope/tools 与 `registry.py`/`inquiry.py`/`sentinel.py`
  逐条相同；`GET /agents` 形状与前端消费字段对齐；渲染走 `esc()`/`textContent`，无注入点。
- **c36e377 no-store 中间件**改法正确（StaticFiles 不收 headers）。
- **演示脚本数字全部对得上种子**：X-200 价 4.85–5.20 / 交期 25-30 / MOQ 1000 /
  CERT-CE-2041 / SPEC-X-200 / SKU-X-200-ar；`up/serve/check` 命令存在；sales/demo1234 与
  `auth.py:82-87` 一致；config.js 默认 mock；`TRADEDESK_PORT` 存在。
- **三张截图确为真数据模式**：api 徽标、三语线程、门槛卡"待评测"、质量分层空态披露、
  成本口径说明均在——R21 修掉的口径纪律在截图里守住了（除 R24-02/03 两处写死标签）。
- **verify_s6_3 PG 偶发 FAIL 判定为并发竞态，非回归**：agent-a 工作区 check 曾报
  90/91（缺 `release.advance` 审计）；同脚本在 main 复跑 **91/91 绿**。根因：两个会话的
  check 共用同一个 tradedesk PG 验证库互相 truncate。**建议**：verify_s6_3 默认 DSN
  改为按会话派生暂存库（同 audit_s6 的做法），消除竞态。

## 三、本轮未核验（诚实披露）

- ARCHITECTURE 五张 mermaid：内容已读、语法目视无问题，**未跑 mmdc 渲染级验证**
  （提交信息自称已跑，未复核）。
- `audit_s3` / `audit_s4` 本轮未复跑（沿用状态卡 90 / 78 / 85 口径）；`audit6r` PG 侧（26 项）
  与 `audit_s6_ui.js` 渲染层（15 项）未复跑，沿用 R21/R23 基线。
- agent-a 含 S6-5 未提交改动的**干净 check 复跑**未做（实现会话仍在编辑，避免互踩）。
- 截图只目视了 UI 口径，未逐像素核对每个数字与读模型的一致性。

## 四、建议处理顺序

1. **R24-01**（版本守卫 + 覆盖路径活断言）——唯一动生产数据完整性的洞；
2. **R24-02 / 03**（两处写死标签改读真实值）+ 作品集文档同步改口（含 R24-09/10/11/12）；
3. **R24-07 / 08**（审计不吞异常；PG 占位索引口径）；
4. **R24-06**（补抽屉渲染层断言，审核方可代写）+ 复核 R24-04/05 的工作区修复；
5. S6-5 判据文档 + 断言进 check；R24-13 状态卡收口。

> 本轮最值得记的一条：**R23 刚把"灌种子抹掉别的版本"修完，S6-5 的导入又在显式版本号上
> 开了同一个洞**——同一类根因会在每条新写入路径上复发，守卫要放在**存储写入层**
> （upsert 前校验版本是否已发布），而不是每条业务路径各写一遍。