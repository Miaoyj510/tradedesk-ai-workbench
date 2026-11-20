# S4 验收清单（向量层）

> S4 = 真实向量检索（PRD 第 9 章、第 12 章）：多语种语义召回、向量存储、索引。
> 验证：`python scripts/verify_s4.py`（SQLite）/ `python scripts/verify_s4.py --dsn <DSN>`（PostgreSQL）。
> 分三小步：**4a** 嵌入接口 + 向量存储（本轮）→ **4b** 向量通道接入 RRF 第三通道
> → **4c** 语义召回评测与回归。

## 0. 口径声明（先看这条）

本轮只做到“**向量层可用**”：嵌入接口、向量存储、索引流程、余弦检索。

**不能宣称语义召回能力**：
- 本轮的云端嵌入用**假 transport**测（不联网、不计费），真实调用在 4b 手动跑；
- 语义召回的**质量**评测属 4c（PRD 9.5 两层口径：公开基准体检 + 自建询盘语料）；
- 使用的嵌入模型与 PRD 原定不同（见第二节），评测报告里必须写清。

## 一、必过条款

| # | 条款 | PRD 出处 | 验证方式 | 状态 |
| --- | --- | --- | --- | --- |
| 4a-01 | 嵌入器必须声明 `name` 与 **`semantic`**；假嵌入必须 `semantic=False` | PRD 9.5 两层口径 | 场景 1 | ✅ |
| 4a-02 | 云端嵌入客户端契约：地址 / 鉴权 / 请求体（MiniMax 与 OpenAI 兼容）/ 响应解析 / **业务错误不重试** / 网络错误重试 / 维度校验 | PRD 13.4.5 选型轴③ | 场景 2（假 transport） | ✅ |
| 4a-03 | 向量**独立存表**（不与块内容混写）：写入 / 读回 / 幂等 / 清理 / 按库版本隔离 | PRD 9.4 难点③ | 场景 3 | ✅ |
| 4a-04 | 索引流程**幂等**（同一模型版本不重复嵌入），支持强制重建 | 成本硬指标（≤0.2 元/条） | 场景 4 | ✅ |
| 4a-05 | 余弦 TopK：排序正确 / 维度不匹配不崩盘 / 空索引安全 | PRD 9.3 | 场景 5 | ✅ |
| 4a-06 | pgvector 可用时用 `vector(N)` 列，不可用时降级 JSONB；**维度不符应被存储层拦下** | PRD 13.4 选型轴③ | 场景 4b / 6 | ✅ |
| 4a-07 | 知识块文本不得含属性数值（R11-08 回归） | PRD 9.3 分块策略 | 场景 4 | ✅ |
| 4a-08 | 嵌入**默认关闭**，需显式开启才调云端 API（启动不计费） | 成本硬指标 | `config.embedder_enabled` | ✅ |

## 二、嵌入模型口径（与 PRD 的偏差，必读）

| 项 | PRD 原定 | 实际本机做法 | 原因 |
| --- | --- | --- | --- |
| 嵌入模型 | BGE-M3 | **MiniMax `embo-01`**（云端 API，1024 维） | 本机无可用 GPU（Iris Xe 共享显存 128MB）、16GB 内存，BGE-M3 的 CPU 推理会把检索拖成主功能阻塞项；且 Docker Hub 拉不到模型镜像 |
| 接口 | — | `retrieval/base.py` 的 `Embedder` 协议**不变** | PRD 13.4.5 选型轴③允许替换实现；以后换回 BGE-M3 只是换一个类 |
| 重排 | bge-reranker-v2-m3 | 未实现 | 4b/4c 再定 |
| 向量存储 | PostgreSQL + pgvector | PostgreSQL 16 + **本地构建的 pgvector 0.8.0 镜像**（`docker/pgvector/Dockerfile`） | Docker Hub 不可达；Alpine 源无 `postgresql16-pgvector`，改从源码编译（需 clang21 生成位码） |

## 三、已知不足（实现 agent 自说）

| # | 不足 | 影响 | 计划 |
| --- | --- | --- | --- |
| L1 | 本轮**没有真实云端嵌入调用**（本机没有 MiniMax key） | 请求体形态是按公开接口写的，未经真实调用核对 | 4b 接真实通道时补核（接口不变，不对就报不通过） |
| L2 | 向量检索在进程内做（内存余弦） | 规模上去后需下推到 pgvector | 4b/4c |
| L3 | 无重排（cross-encoder） | RRF 后无精排 | 4b |
| L4 | 无语义召回质量评测 | **不能报任何语义能力数字** | 4c |
| L5 | 嵌入默认关闭，业务运行时需显式开启并配上 key | 无 key 时检索降级为词法+结构化双通道 | 部署时配置 |
| L6 | 向量列维度固定（`vector(1024)`） | 换嵌入模型维度不同时需重建列 | 换模型时记得重建 |

## 四、本机环境决定（供以后复现）

```powershell
# 1. 构建 pgvector 镜像（Docker Hub 不可达，只能本地构建）
docker build -t tradedesk/pgvector:pg16 -f docker/pgvector/Dockerfile .

# 2. 切换数据库镜像（compose 已指向本地镜像）
docker compose up -d db redis

# 3. 验证扩展
docker exec tradedesk-db psql -U tradedesk -d tradedesk -c "CREATE EXTENSION IF NOT EXISTS vector;"
```
