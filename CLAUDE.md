# CLAUDE.md —— TradeDesk-AI（外贸业务员的 AI 数字员工工作台）

> 本文件在**每次会话开始时自动进入上下文**。上下文丢失不要紧 —— 状态都写在仓库里。

## 开工三步（顺序别省）

1. 读 `docs/NEXT.md` —— **状态卡**：现在到哪了、下一步干什么、怎么验收。
2. 读 `docs/S2-ACCEPTANCE.md` —— **S2 验收基线**（条款号 = 验收时的判据，改代码要对着它）。
3. `git log --oneline --all --graph -12` —— 看三个分支各自到哪了。

## 这是什么项目

一个"询盘自动应答 + 交易风控 + 人审签核"的后端工作台。PRD 在仓库根目录
（`PRD-外贸业务员的AI数字员工工作台-v1.0.md`，174KB，是所有行为的唯一真源）。

```
src/tradedesk/
  domain/     状态机、领域模型、枚举（纯函数，无 IO）
  runtime/    supervisor（路由/仲裁/SLA）、gates（11 条闸门 + 闸门令牌）、handoff（15 字段契约）
  agents/     inquiry（询盘应答）、draft_writer（模型出稿）、prompts、sentinel（风控）、registry（角色注册表）
  llm/        多云厂商层：providers（5 家注册表）、factory、chat、pricing（成本计量）
  retrieval/  检索层：词法 + 结构化 + 稠密三通道 RRF、embedders（远端/占位）、indexer、store_retriever
  api/        FastAPI 层：app、auth（登录/会话/角色可见域）、workbench（读模型 BFF）、deps（闸门令牌守卫）
  store/      base（接口）、sqlite_store、postgres_store —— 换后端业务代码零改动
  release/    发布与灰度（S6-3）：manifest（发布清单）、bucketing（按企业分桶）、service（推进 / 分类别回滚）
scripts/      verify_s1…s6（自证）、audit_s2…s6（审核方断言）、eval_retrieval / eval_llm（评测）、release_cli（发布灰度命令行）、push.ps1（推送）
docs/         S2…S6-ACCEPTANCE.md、审核报告 R9…R21、S6-FIX-R22.md、S4-EVAL-BASELINE.md、NEXT.md（状态卡）
             作品集：PORTFOLIO.md（一页纸）、ARCHITECTURE.md（架构图）、DEMO-SCRIPT.md（演示脚本）、screenshots/
```

## 三个角色 / 分支（WORKFLOW.md 第 1 节）

| 位置 | 分支 | 干什么 |
| --- | --- | --- |
| `D:\桌面\AIagent_project\外贸项目` | `main` | **只用来合并与整体验证，不要在这里改代码** |
| `.worktrees\agent-a` | `agent-a` | 实现 agent：开发、验证、提交 |
| `.worktrees\agent-b` | `agent-b` | 审核 agent：审核、出缺陷、改进、提交 |

**七条铁律**（WORKFLOW.md 第 2 节，摘要）：只在自己的 worktree 改文件；只提交自己改的
（禁止 `git add -A`）；不碰对方的分支；不覆盖对方未提交的工作；一片一提交；
提交前 `dev.ps1 check` 必须全绿；数据库各自隔离。

**PRD 冲突以 PRD 为准**；实在判断不了就停下来问，不要擅自选一边。

## 当前状态（2026-11-24）

S1 → S2（R9→R10）→ S3（R11/R12/R14→R13/R16）→ S4-4a/4b/4c（R15→R16、R17→R18 `e002015`）
→ S5 厂商层 + 真模型出稿 + 成本落库 + 分语种评测（R19→R20 `623ca24`）
→ S6-1a/1b 前端接真数据 + S6-2 镜像 + S6-4 登录（R21→`92bb7d1`）均已交卷；
→ **S6-3 灰度与回滚已交卷**（发布单元四类独立回滚 / 按企业分桶 / 观察窗推进 / 双索引（复合主键）/ 越权闸门 / 证据可核验），
审核 R22 + R23（三轮共 11 个根因）已由实现线修完 —— 见 `docs/S6-FIX-R22.md`。

`dev.ps1 check` **十三步**：语法 → S1 双后端 53/53 → S2 自证 97/97 → S3 自证 52/52
→ S2 独立审核 106/106 → pytest 262 passed → S3 独立审核 90/90 → S4 向量层 60/60（PG 62/62）
→ S4 独立审核（SQLite 78 / PG 85）→ S5 独立审核 54 项 → S6 独立审核（49 + 渲染层 15 项）
→ **S6-3 灰度回滚自证（91 项 × SQLite / PostgreSQL 双后端）** → **S6-3 独立审核（67 项，含越权路径）**。

**审核线（`agent-b`）现状**：**R23 审核完成**（S6-3 发布 / 灰度 / 回滚；
判据 `docs/S6-3-AUDIT-CRITERIA.md` 写于实现之前，断言 **67 项**，
三轮共 **11 个根因 —— 都已由实现线修完**，复跑 **66 PASS / 0 FAIL**）；
`dev.ps1 audit6r` 已入 `check` 第十三步（绿）。
上一轮 **R21**（S6-1a 工作台读模型 + S6-1b 前端接真数据）的 12 个根因
**已由实现线修完**（`agent-a @ 92bb7d1`），审核方复核通过（已入 `check` 第十一步）。
再上一轮 **R19**（S5 厂商层 + 真模型出稿）的 13 个根因**已由 R20 修完**（`agent-a @ 623ca24`），
S5 独立审核已入 `check` 门禁。
下一步：**审核 agent 复核 R22 修复** → 用户确认 → 合并 `agent-a` → `main` 并推送 —— 看 `docs/NEXT.md`。

## 常用命令

```powershell
$dev = "D:\桌面\AIagent_project\外贸项目\dev.ps1"
& $dev check              # 交付前自检（十三步）：语法 + S1 双后端 + S2/S3 自证 + pytest + S2…S6/S6-3 独立审核
& $dev verify             # 只跑 S1 验证
& $dev test               # 只跑 pytest
& $dev audit              # S2 独立审核（审核方断言）
& $dev audit3             # S3 独立审核（已进 check，可单独跑；R14 缺陷已修，绿）
& $dev audit4             # S4 独立审核（R15/R17 缺陷已修，绿）
& $dev audit5             # S5 独立审核（R19 缺陷已由 R20 修完，绿）
& $dev audit6             # S6 独立审核（R21 缺陷已修，绿；渲染层需 node；--dsn 派生暂存库 tradedesk_audit_s6）
& $dev audit6r            # S6-3 独立审核（发布/灰度/回滚/越权；R22 + R23 缺陷已修，绿）
& $dev index              # 建立知识库向量索引（显式动作，会调云端 API、计费）
& $dev release status     # 发布台账（S6-3：status / create / advance / rollback / resolve）
& $dev serve              # 起 HTTP 接口（http://127.0.0.1:8080/docs）
& $dev clean              # 清验证临时文件与缓存
```

双后端验证要带 DSN（两个 agent 各用各的库，见 WORKFLOW.md 第 4 节）：

```powershell
& $dev check -Dsn "postgresql://tradedesk:tradedesk_local_dev@127.0.0.1:5433/tradedesk_a"
```

## 本项目特有的坑（都是踩过的）

- **`str(枚举)` 在 Python 3.11+ 是 `"Stage.QUOTING"` 不是 `"QUOTING"`** —— 拿去查库会一条都查不到
  （看板按阶段筛选静默返回空）。用 `store/base.py` 的 `enum_val()`。
- **SQLite 连接不能跨线程共用**；`check_same_thread=False` 只是不报错，事务照样互相踩。
  存储层已改成线程局部连接 + 写锁，别退回去。
- **闸门令牌必须一次性 + 绑 thread_id + draft_id + action** —— 少绑一个就是越权漏洞。
- **闸门令牌只认 `action="sign_off"` / `"send_draft"` / `"advance_stage"`**；
  `action=""` 的令牌是万能令牌（签发端要校验）。
- **`dev.ps1` 必须存成 UTF-8 with BOM** —— Windows PowerShell 5.1 按 GBK 读会整段语法崩。
- **控制台是 GBK** —— Python 输出中文会乱码。`verify_s1/2/3.py` 与 `audit_s2.py` 已做
  编码自适应（tty 用原生编码 + replace 兜底，非 tty 强制 UTF-8）；新脚本照抄这个开头。
- **audit_s6 的 --dsn 会 DROP 库**：`scripts/audit_s6.py --dsn <DSN>` 会把 DSN 里的库
  **DROP 后重建**（自建暂存库并回收）。所以**绝不能**把开发库（tradedesk_a /
  tradedesk_b）传给它。`dev.ps1 check` 与 `dev.ps1 audit6` 已改成从传入 DSN 派生
  独立暂存库名（tradedesk_audit_s6）；手工跑时也要指向暂存库。
- **PostgreSQL 审核库**：`scripts/audit_s2.py --dsn` 会自建独立库并回收，
  **不要**指向 `tradedesk_a` / `tradedesk_b`（会被 truncate）。
- **知识库是演示数据**：里面含**已过期**的 TC-M77-118、**已废止**的 CERT-CE-1890、
  **未生效**的 REG-US-204 —— 用来验时效闸门；名单也是结构化演示数据，不是真实名单。
- **产品块分两类**：`doc_type="产品规格"` 是**数值的唯一真源**（价格/交期/MOQ/负荷），
  `doc_type="产品条目"` 是**每型号 4 个语言版本**的描述性文本（文本块里不得出现数值，PRD 9.3）。
- **库版本号只有一份**：`config.kb_version` 直接引用 `data/kb_seed.py` 的 `KB_VERSION`，别再写字面量。
- **pgvector 镜像只能本地构建**：`docker build -t tradesk/pgvector:pg16 -f docker/pgvector/Dockerfile .`。
  Docker Hub 拉不到不可用 `postgresql16-pgvector`；从源码编译时 PGXS 要生成位码，需要 **clang21/llvm21**（`NO_BC=1` 对它无效）。
- **嵌入默认关闭**：`TRADEDESK_EMBEDDER_ENABLED=1` + `MINIMAX_API_KEY` 才会调云端 API（启动即计费）。
- **pgvector 的 `vector(N)` 列会拒绝错误维度**：换嵌入模型维度不同时要重建列（换模型时分步切换）。
- **向量索引是显式动作**：`dev.ps1 index`（或 `scripts/index_kb.py`）。应用启动**只读**索引，不自动建
  —— 否则每个审核脚本都要重新调云端 API 重新嵌入全部块（计费 + 慢）。
- **`embo-01` 实测 1536 维**（不是文档常见的 1024）；响应里是 `vectors` 而不是 `data[].embedding`。
- **`embo-01` 跨语种对齐弱**：zh↔es/ar 余弦相似度约 -0.02（无关）——四语分块与术语表桥接是必需的，不是优化。
- **向量检索分不清型号**（型号号是弱信号）——型号识别的唯一权威是**结构化通道**，
  融合时稠密通道的 RRF 参数必须大于结构化通道（见 docs/S4-ACCEPTANCE.md 第三节）。
- **稠密通道权重最低**（RRF k=100）：向量分不清型号（实测），型号识别只认结构化通道。
- **每条询盘只能 1 次云端嵌入调用**：规格/证书的内部回查走 `use_dense=False`（按标识取块，不需要语义）。
- **`list_kb_chunks` 必须有 `ORDER BY chunk_id`**：PostgreSQL 更新一行后那行会被挤到堆尾，不排序就不能靠顺序猜块。
- **产品必须被结构化通道确认才进草稿**：只靠词法命中的产品不许报型号与价格，
  否则"给我个最好价"会被随便挑一个产品并报价。
- **PG 近邻检索必须带 `kb_version`**（R17-01，R18 已修）：否则旧版本向量会把稠密通道挤空 ——
  同一查询在 PG 上返回 0 条、SQLite 上返回 10 条。查索引用
  `docker exec tradedesk-db psql -U tradedesk -d <库> -c "SELECT indexname FROM pg_indexes WHERE tablename='kb_embeddings'"`。
- **HNSW 索引在存量库也要建**（R17-02，R18 已修）：此前只在「表不存在」分支建索引，
  从 4a 升上来的库永远没有 ANN 索引，近邻检索退化成全表扫描。
- **`dev.ps1 eval` 每次跑 = 160 次云端查询嵌入调用**：评测脚本不看
  `TRADEDESK_EMBEDDER_ENABLED`（那只管应用启动），跑之前先想清楚要不要花这个钱。
- **评测 Macro-F1 用 PRD 12.3 的五类意图口径**（R17-05，R18 已修）：此前把无样本的
  `unknown` 算进分母，报出来的数系统性低 1/6；比对门槛前先看清口径。

- **厂商表在 `src/tradedesk/llm/providers.py`**：换厂商 = 改表里一行 + 改一个 env，业务代码不动。
  基址地址**按厂商分开**（每家各有 `base_url_env`），不会拿 MiniMax 的地址去请求阿里云。
  ✅ **key 已按厂商隔离（R19-01，R20 已修）**：`resolve_api_key` 只认属于对话厂商的 fallback，
  嵌入厂商只有在自己就是对话厂商时才能用它；不会再把 MiniMax 的密钥发往阿里云/智谱。
  仍建议 `.env` 里只留当前厂商的 key，避免混淆。
- **`MiniMax-Text-01` 已下线**（实测 HTTP 404 "no longer available"），`MiniMax-M2` 也一样返回 404 ——
  **`/v1/models` 列表不等于可用**。当前可用：`MiniMax-M3` / `MiniMax-M3.1-Flash`（默认已改 M3）。
- **MiniMax-M3 把思考过程写在 `message.content` 里**（`<think>...</think>`）——
  不剔就会把内部推理对外发给买家。剔除做在**解析层**（`chat.py` 的 `strip_reasoning`）。
- **HTTP 4xx 是业务错误，不是网络故障**：`urllib.error.HTTPError` 是 `URLError` 的子类，
  一律当网络错误处理就会把 401 重试三次（每次都计费），且报错指向错的方向。
  必须单独捕获 `HTTPError` 并转成业务错误。
- **MiniMax 与其余厂商的嵌入请求体不同**：MiniMax 是 `{"model","texts":[...],"type":"db|query"}` → `vectors`；
  其余是 OpenAI 兼容 `{"model","input":[...]}` → `data[].embedding`。两种形态各有一个客户端实现。
- **pytest 的 basetemp 固定在项目内 `.pytest_tmp`**：系统 `TEMP` 里的 `pytest-of-<user>` 目录可能被其他账号的 ACL 限制，
  pytest 创建 basetemp 时会直接 `PermissionError`，测试全部报错（不是代码的错）。
- **模型出稿的开关默认关**（`TRADEDESK_LLM_DRAFT_ENABLED=0`）：开启后每条询盘都会发起一次**付费**云端调用，
  且草稿会从“确定性模板”变成“模型写的”——**审核断言会随机器人变动**（不可重复）。
  审核 / 验证脚本必须用 `deps.offline_config()` 强制关掉它。
  此事已实测踩过：第一次接线后 `dev.ps1 check` 的 pytest 从 6 秒涨到 48 秒 ——
  审核脚本经 `AppContext` 构造 Agent 时自动拿到了云端客户端，真的调了云端。
- **模型出草的数值不能给模型看**：模型只拿到 `{{价格}}` 这种占位符，真值由代码从结构化字段回填。
  这不是提示词是否加强的问题 —— 拒绝依赖模型配合度。断言在
  `tests/test_draft_writer.py::test_model_never_sees_the_real_price`。
- **买家消息的数据区用随机后缀标签**：`</buyer_message>` 是写死的，买家能提前关区把指令塞进去。
- **模型的数字白名单要分层**：① **币种位置**的数字必须在**知识库真值**里（否则拿买家的数量冒充价格）；
  ② 其他位置的数字可以是**买家原话里的数字**（模型复述买家的数量不算编造）。
  旧逻辑将任何数字都当越权 → 真实测试里 ar/es 语种的越权率被抬到 70%/20%（事实上是误判）。
- **模型漏了可选占位符就代码补齐**，不是整段丢弃：必填只有三个核心事实（型号/价格/交期）；
  MOQ / 条款 / 港 / 证书句 / 追问句缺失时由 `render_facts` 按语种补上 —— 事实不会因模型的忘丼而丢失。
- **`finish_reason="length"` 要报“输出被截断”**，不是“模型返回空”：M3 是思考模型，**思考 token 与正文共用 `max_tokens`**（默认 2048）。
  当时只看到“返回为空”根本无法区分“模型不会写”与“被截断了”。
- **汇总口径只有一份**：`store/base.py` 的 `summarize_llm_usage()` 是唯一实现，SQLite 与 PostgreSQL 都只取行。
  单价未核实时 `cost_cny` 是 **None（不是 0）** —— 0 元会让人误以为“不费钱”。
- **闸门判定只能在后端（`runtime/gates.py`）**：前端若自己重算闸门，就出现了第二份真相（R17 教训）。
  所以前端只走**聚合读模型** `GET /workbench`（BFF），不自己拼 N+1 个接口。
- **买家原话要落库**（FR-EVA-02）：只存 `message_hash` 的话前端展示的“买家原话”是空的。
  现在 ingest 时补 `EventType.MESSAGE_RECEIVED` 事件。静态原型的 mock 把这个缺口藏了过去。
- **质量分数不是实时数据**：分语种检索质量来自评测脚本产物（`dev.ps1 eval`），不在实时接口里 —— 给一个“看起来像实时”的分数比给不给更坏。
- **前端切片必须有一层“真跑渲染层”的断言**（R21 教训）：S6 的自证脚本只跑了数据层 `api.js`，
  `app.js` 怎么把数据画成界面**一次都没被跑过** → “数据对、界面说错”一个都没被发现。
  **“接上真数据” ≠ “界面显示的是真数据”。**
- **拼 `innerHTML` 必须转义**：转交原因可经 HTTP 接口任意写进事件流 → 时间线文本与草稿正文
  不转义就是**存储型 XSS**（R21-01）。漏转义的地方往往恰好是买家/人工可控的那两处。
- **界面上每个数字都要有出处**：写死的 mock 数字（`CN-2211` / `41` / `78%` / `¥0.046`）在真数据模式下
  **一个都不会被覆盖**（没有 id，前端也不写它们）→ 宁可显示“待评测”，不要写死“已达标”。
- **单位成本的分母是“询盘条数”，不是“调用次数”**（R21-05）：PRD 12.4 的硬指标是**元/条询盘**；
  `llm_usage` 的行数是**调用次数**（一条询盘 3~4 次）→ 拿它做分母会把硬指标报低 3~4 倍。
- **多版本共存要求复合主键**（R23-01）：`kb_chunks` / `kb_embeddings` 主键是**单列 `chunk_id`**，
  `ON CONFLICT(chunk_id) DO UPDATE` → **同名 chunk_id 只能存一份**，灌新版本 = 旧版本被顶掉。
  要两版共存，主键必须改成 `(chunk_id, kb_version)`（PG 侧还要重建 HNSW 索引）。
- **灌种子是 replace 式 → 会抹掉别的版本**（R23-02）：`load_kb()` 换版本即清空。
  启动时"基线版本为空就重灌"会把灰度版顶掉；`dev.ps1 index` 也一样
  → **运维跑一次日常的索引命令，灰度版本的块与向量当场清空**。
- **测试 fixture 里的手工技巧不能证明产品行为**（R23-01）：自证脚本给 chunk_id 手工加
  `-v2` 后缀来造"两版共存"，断言是绿的 —— 但生产路径（`load_kb`/`index_kb`）根本不加后缀。
  **断言要测生产路径，不测脚手架。**
- **判据与审核断言要写于实现交付之前**（R23 的做法）：否则审出来的是"实现自己"，
  不是 PRD 13.4.8。审核方先写判据（`docs/S6-3-AUDIT-CRITERIA.md`）+ 脚本，再看实现。
- **加了组件不等于有了机制**（R23-04）：清单里的 `index_version` **没有任何模块消费**
  → 改了它什么都不会发生。老形态（R17/S15-03）：**标志位只有声明没有守卫，等于装饰**。
- **价格表只在 `src/tradedesk/llm/pricing.py` 里维护**，必须带来源与取数日期；模型不在表里就返回 None（不猜、不估算）。
- **`.env` 里各厂商专用的 key 变量名由厂商表统一管**（不写死在 `config.py`）；
  项目自己的 `.env` 解析器**不支持行内注释**（取右侧整个当值），注释必须单独占一行。

## 写代码的规矩

- 中文注释解释**为什么**，不解释**做了什么**。
- 每个提交前跑 `dev.ps1 check`；涉及越权路径的改动，还要跑 `dev.ps1 audit`。
- 改了行为就同步改对应的 `docs/*-ACCEPTANCE.md` 和 `docs/NEXT.md`（状态卡）。
- 提交信息格式见 WORKFLOW.md 第 3 节：`<切片号>: <一句话做了什么>` + 正文 + `Co-Authored-By`。
