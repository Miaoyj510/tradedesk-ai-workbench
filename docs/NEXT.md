# NEXT —— 状态卡（2026-11-19）

> **每次开工先读这一页。** 上下文丢了不要紧，这页写着现在到哪了、下一步干什么。
> 配套：`docs/S2-ACCEPTANCE.md`（S2 验收基线）、`docs/S2-AUDIT-R9.md`（R9 审核报告）、
> `docs/S3-ACCEPTANCE.md`（S3 验收清单）。

## 一、现在到哪了

| 切片 | 状态 |
| --- | --- |
| S1（存储层 / 领域模型 / 状态机 / 闸门） | ✅ 收口 |
| S2（Supervisor / Handoff / FastAPI / 闸门令牌） | ✅ 收口 |
| S2 审核 R9（14 个根因，含 1 个 P0 越权） | ✅ 审核完成（`8927d7c`，分支 `agent-b`） |
| S2 缺陷修复 R10 | ✅ 14 个根因全修完（`9a34d4e`） |
| **S3（检索层：多语种召回 / 合规时效 / 引用装配）** | ✅ 已交卷（`3e86884`，分支 `agent-a`），自证 38/38 |
| S4（真实向量检索：BGE-M3 + pgvector） | ⬜ **下一步就是这件事** |
| S5（接 MiniMax 真模型 + 成本计量） | ⬜ 未开始 |
| S6（前端联调 + 镜像 + 灰度回滚） | ⬜ 未开始 |

`dev.ps1 check` 现在是**六步**，当前全绿：语法 → S1 双后端 53/53 → S2 自证 97/97 →
S3 自证 38/38 → S2 独立审核 106/106 → pytest 124 passed。

分支现状：`main` = `e9b8278`（已合并 S3），`agent-a` = `3e86884`（S3 实现线），
`agent-b` = `f4fb934`（R9 审核线）。

## 二、下一步：S4 真实向量检索

S3 只是**词法（BM25）+ 结构化（型号/证书号/HS 精确匹配）**双通道，稠密通道是占位实现。
S4 才把"多语种语义召回"变成真的，验收基线见 PRD 第 9 章与第 12 章。

| 事项 | 现状 / 约束 |
| --- | --- |
| 嵌入模型 | BGE-M3（多语种），本机无 GPU（Iris Xe 共享显存 128MB）、16GB 内存 → 走 ONNX 量化版或远端嵌入 API |
| 向量库 | PostgreSQL 16 + pgvector。**Docker Hub 拉不到镜像**，用本地构建：`docker/pgvector/Dockerfile` |
| 替换点 | `retrieval/base.py` 的 `Embedder` / `Retriever` 协议，`retrieval/embedders.py` 的 `NullEmbedder` 就是替身 |
| 检索口径 | 公开基准（MIRACL / MLDR / Mr. TyDi / mMARCO / MKQA）只能证明**检索管线**没问题，证明不了询盘场景做得好 —— 询盘语料必须自建（PRD 9.5 两层口径，别混） |
| 硬指标 | 引用正确率 100%、制裁名单召回 ≥ 99%、单位询盘 Token 成本 ≤ 0.2 元/条 |
| 并发 | 测试 1–3 人；生产 50–100 并发（此时向量检索的批量与缓存策略要一起定） |

建议 S4 切成三小步：**4a 嵌入接口与 pgvector 表结构** → **4b 向量通道接入 RRF（第三通道）**
→ **4c 语义召回评测与回归**。每步照旧"一片一提交 + 验证脚本"。

## 三、每片子的验收动作

```powershell
$dev = "D:\桌面\AIagent_project\外贸项目\dev.ps1"
& $dev check -Dsn "postgresql://tradedesk:tradedesk_local_dev@127.0.0.1:5433/tradedesk_a"  # 实现 agent
& $dev audit                                                                          # 审核断言
```

审核脚本双后端跑法（脚本自建独立库并回收，**不要**指向 `tradedesk_a` / `tradedesk_b`）：

```powershell
& $dev audit
& $dev audit -Dsn "postgresql://tradedesk:tradedesk_local_dev@127.0.0.1:5433/tradedesk_audit_b"
```

## 四、别忘了合并

合并路径固定为 `agent-b` → `agent-a` → `main`，每次合并后在集成区重跑一次 `check`。
S3 已走完这条路径：`agent-a`(`3e86884`) → `main`(`e9b8278`)，三线远端 SHA 已核对一致。

## 五、几个已知的坑

见根目录 `CLAUDE.md` 的"本项目特有的坑"一节（枚举 `str()`、SQLite 跨线程、
闸门令牌绑定、`dev.ps1` 的 BOM、控制台 GBK、PostgreSQL 审核库）。
