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
  agents/     inquiry（询盘应答）、sentinel（风控）、registry（角色注册表）
  retrieval/  S3 检索层：base（协议）、lexical（多语种分词）、pipeline（7 步管线）、embedders（BGE-M3 占位）
  api/        FastAPI 层：只做参数搬运 + 闸门令牌鉴权，业务规则一律不在这里
  store/      base（接口）、sqlite_store、postgres_store —— 换后端业务代码零改动
scripts/      verify_s1.py / verify_s2.py / verify_s3.py（自证）、audit_s2.py（审核方断言）
docs/         S2-ACCEPTANCE.md、S2-AUDIT-R9.md、S3-ACCEPTANCE.md、NEXT.md（状态卡）
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

## 当前状态（2026-11-19）

S1 收口 → S2 已交卷 → R9 审核（14 个根因）→ R10 全修完
→ S3 检索层交卷（`3e86884`）→ 审核 R11（8 个根因）→ 审核 R12（3 个根因）
→ **R12/R13 修复全部完成**（详见 `docs/S3-FIX-R13.md`）。

`dev.ps1 check` 七步当前全绿：语法 → S1 双后端 53/53 → S2 自证 97/97 → S3 自证 52/52
→ S2 独立审核 106/106 → pytest 126 passed → **S3 独立审核 78/78**。

下一步是 S4（真实向量检索 BGE-M3 + pgvector）—— 看 `docs/NEXT.md`。

## 常用命令

```powershell
$dev = "D:\桌面\AIagent_project\外贸项目\dev.ps1"
& $dev check              # 交付前自检（七步）：语法 + S1 双后端 + S2/S3 自证 + S2/S3 独立审核 + pytest
& $dev verify             # 只跑 S1 验证
& $dev test               # 只跑 pytest
& $dev audit              # S2 独立审核（审核方断言）
& $dev audit3             # S3 独立审核（已进 check，这里可单独跑）
& $dev audit3             # S3 独立审核（审核方断言；R11 缺陷修完前为红）
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
- **PostgreSQL 审核库**：`scripts/audit_s2.py --dsn` 会自建独立库并回收，
  **不要**指向 `tradedesk_a` / `tradedesk_b`（会被 truncate）。
- **知识库是演示数据**：里面含**已过期**的 TC-M77-118、**已废止**的 CERT-CE-1890、
  **未生效**的 REG-US-204 —— 用来验时效闸门；名单也是结构化演示数据，不是真实名单。
- **产品块分两类**：`doc_type="产品规格"` 是**数值的唯一真源**（价格/交期/MOQ/负荷），
  `doc_type="产品条目"` 是**每型号 4 个语言版本**的描述性文本（文本块里不得出现数值，PRD 9.3）。
- **库版本号只有一份**：`config.kb_version` 直接引用 `data/kb_seed.py` 的 `KB_VERSION`，别再写字面量。
- **`list_kb_chunks` 必须有 `ORDER BY chunk_id`**：PostgreSQL 更新一行后那行会被挤到堆尾，不排序就不能靠顺序猜块。
- **产品必须被结构化通道确认才进草稿**：只靠词法命中的产品不许报型号与价格，
  否则"给我个最好价"会被随便挑一个产品并报价。

## 写代码的规矩

- 中文注释解释**为什么**，不解释**做了什么**。
- 每个提交前跑 `dev.ps1 check`；涉及越权路径的改动，还要跑 `dev.ps1 audit`。
- 改了行为就同步改对应的 `docs/*-ACCEPTANCE.md` 和 `docs/NEXT.md`（状态卡）。
- 提交信息格式见 WORKFLOW.md 第 3 节：`<切片号>: <一句话做了什么>` + 正文 + `Co-Authored-By`。
