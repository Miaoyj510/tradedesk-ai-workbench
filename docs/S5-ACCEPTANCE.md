# S5 验收基线：多云厂商 provider 层

> 对应输出：供应链说「现在只能连 MiniMax」。目标是把“厂商”从代码里拆出来，
> 变成表里的一行；业务代码不知道用的是哪家。
> 记录日期：2026-11-21。

## 一、结论一句话

已接入 **5 家主流厂商**（MiniMax / 阿里云百炼 / 智谱 GLM / 火山方舟 / DeepSeek），
所有端点**已实测可达**（带无效 key 请求返回 401/400，而非连接失败）；
MiniMax 的**真实对话与嵌入调用已跑通**（对话 1 次、嵌入 1 次，均返回正常结果）。

## 二、厂商矩阵（请求形态 / 实测口径）

| 厂商 | 对话端点 | 对话模型（默认） | 嵌入端点 | 嵌入模型 | 维度 | key 环境变量 | 对话端点实测 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| MiniMax | `https://api.minimaxi.com/v1/chat/completions` | `MiniMax-M3` | `https://api.minimaxi.com/v1/embeddings` | `embo-01` | **1536（实测）** | `MINIMAX_API_KEY` | 401（路径存在） |
| 阿里云百炼（通义千问） | `https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions` | `qwen-plus` | `https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings` | `text-embedding-v3` | 1024（标称值，**未实测**） | `DASHSCOPE_API_KEY` | 401 |
| 智谱 AI（GLM） | `https://open.bigmodel.cn/api/paas/v4/chat/completions` | `glm-4-plus` | `https://open.bigmodel.cn/api/paas/v4/embeddings` | `embedding-3` | 1024（标称值，**未实测**） | `ZHIPU_API_KEY` | 401 |
| 火山引擎方舟（豆包） | `https://ark.cn-beijing.volces.com/api/v3/chat/completions` | `doubao-pro-32k` | `https://ark.cn-beijing.volces.com/api/v3/embeddings` | `doubao-embedding` | 2560（标称值，**未实测**） | `ARK_API_KEY` | 401 |
| DeepSeek | `https://api.deepseek.com/chat/completions` | `deepseek-chat` | **无** | **无** | — | `DEEPSEEK_API_KEY` | 401 |

**需要注意的事实（本次实测得出，不是文档录转）：**

1. **`MiniMax-Text-01` 已下线**：实测返回 HTTP 404 `The model "MiniMax-Text-01" is no longer available. (2069)`。
   同一个 `/v1/models` 列表里的 `MiniMax-M2` 也返回 404（**列表不等于可用**）。
   当前实测可用：`MiniMax-M3`、`MiniMax-M3.1-Flash`。本项目默认改为 **`MiniMax-M3`**。
   → 对应的 PRD / 评测报告里写的“MiniMax-Text-01”需要更新。
2. **MiniMax-M3 把思考过程写在 `message.content` 里**（`<think>...</think>` 块），实测原文：
   `<think>内部思考</think>\n\nDear buyer, our MOQ is 500 pcs.`。
   本项目的产出是**要发给买家的回复**，思考过程混进去就是对外发送内部推理，
   也可能泄露内部价格思路 → `chat.py` 里的 `strip_reasoning()` 在**解析层**剔除（不是在业务层剔）。
3. **MiniMax 嵌入请求体是自定义形态**：`{"model","texts":[...],"type":"db|query"}`，响应是 `vectors:[[...]]`（**不是** OpenAI 的 `data[].embedding`）；
   其余四家都是 OpenAI 兼容形态 `{"model","input":[...]}` → `data[].embedding`。
   两种形态各有一个客户端实现，不用靠猜。
4. **DeepSeek 官方未提供嵌入接口**（公开文档未找到）→ `supports_embedding = False`，
   选它做嵌入厂商时工厂直接返回 `None`（不静默失败）。

## 三、切换方式（业务代码一行不动）

```powershell
# 1) 改一行
TRADEDESK_LLM_PROVIDER=aliyun
# 2) 填对应厂商的 key
DASHSCOPE_API_KEY=sk-...
# 3) 重启服务，生效
```

嵌入厂商可以单独切（对话用一家、语义通道用另一家）：

```powershell
TRADEDESK_EMBEDDER_PROVIDER=zhipu
TRADEDESK_EMBEDDER_MODEL=embedding-3
TRADEDESK_EMBEDDER_DIM=1024
```

**基址地址不会串厂商**：厂商表里每家各有自己的 `base_url_env`（`MINIMAX_BASE_URL` / `DASHSCOPE_BASE_URL` / …），
配置层按厂商取，不会把 MiniMax 的地址拿去请求阿里云。
优先级：`TRADEDESK_LLM_BASE_URL`（通用覆盖）> 厂商专用环境变量 > 厂商表默认地址。

## 四、计量与成本口径

| 项 | 口径 |
| --- | --- |
| 调用计数 | `ChatClient.calls` / `embedder.calls` = **HTTP 实际调用次数**（重试也算一次，因为重试也会计费） |
| 令牌计量 | `ChatResult.usage()` 取自厂商的 `usage` 字段（`prompt_tokens` / `completion_tokens` / `total_tokens`） |
| 缓存延价 | MiniMax 的 `usage.prompt_tokens_details.cached_tokens` 存在（实测），可用来拆分新建与命中缓存的令牌，防止把命中缓存的价格算进“新建成本” |
| 单价 | **未核实** —— 五家厂商的计费单价未在本机核实，因此**不估算金额**。单价待核实后写入 `docs/S5-ACCEPTANCE.md` 附录。 |

PRD 12.4 的硬指标“单位询盘 Token 成本 ≤ 0.2 元/条”在单价核实前**无法判定**（现在只能给出令牌数与调用次数）。

## 五、踩降与守护（本次改造实际发现的四个问题）

| # | 问题 | 为什么要修 | 处理 |
| --- | --- | --- | --- |
| 1 | **HTTP 4xx 被当成网络错误重试** | `urllib.error.HTTPError` 是 `URLError` 的子类；一律当网络错误处理就会把 401 重试三次（每次都计费），且报错指向“网络”而不是“鉴权失败”，排查方向会被引错 | 单独捕获 `HTTPError`，转成业务错误（不重试）；对话与嵌入两处都已修 |
| 2 | **HTTP 200 但 body 是 `{"error":{...}}`** | OpenAI 兼容风格的错误可能不带 4xx；不识别就会拿到 `choices 为空` 的报错，诊断信息丢失 | 解析层增加 `error` 字段分支，报错带上厂商的错误码与文案 |
| 3 | **`.env` 里的厂商专用 key 沉沉无声** | 配置层只映射了 `MINIMAX_API_KEY`，在 `.env` 里填 `DASHSCOPE_API_KEY` 不会生效 | 配置层按当前厂商自动读对应的 `api_key_env`；优先级：通用 `TRADEDESK_LLM_API_KEY` > 厂商专用 > 旧的 `MINIMAX_API_KEY` |
| 4 | **思考块混进去对外发送** | 见二第 2 条实测 | `strip_reasoning()` 在解析层剔除 `<think>...</think>`（含未闭合的尾块） |

## 六、代码结构

```
src/tradedesk/llm/
  providers.py   # 厂商表（身份 / 路由 / 模型 / key 变量 / 请求形态）+ 别名表
  chat.py        # OpenAI 兼容对话客户端 + 业务错误/网络错误分开 + 思考块剔除
  factory.py     # 从 Config 装配对话客户端 / 嵌入器
  __init__.py
src/tradedesk/retrieval/embedders.py
  OpenAICompatibleEmbedder   # OpenAI 兼容形态嵌入客户端（阿里云 / 智谱 / 火山）
  RemoteEmbedder             # MiniMax 自定义形态（texts/type）
```

切换厂商 = 改 `providers.py` 表里一行 + 改一个环境变量。**业务代码不动。**

## 七、自动验证

```powershell
& ".\.venv\Scripts\python.exe" -m pytest tests/test_llm_providers.py -q
```

覆盖：厂商表覆盖家数 / 别名 / 错误提示 / key 优先级 / 通用 key /
对话请求与解析 / HTTP 4xx 不重试 / 思考块剔除 / 两种嵌入形态 / 维度校验 /
工厂返回 None 的各种情况 / 基址地址不串厂商。
**全部离线，不联网、不计费。**

## 八、待验证项（不得自行填数）

| 编号 | 事项 | 状态 |
| --- | --- | --- |
| S5-V1 | 阿里云 `text-embedding-v3` 实际输出维度（标称 1024） | 未验证（无 key） |
| S5-V2 | 智谱 `embedding-3` 实际输出维度（标称 1024） | 未验证（无 key） |
| S5-V3 | 火山 `doubao-embedding` 实际输出维度（标称 2560） | 未验证（无 key） |
| S5-V4 | 各厂商的计费单价（用于拟合 PRD 12.4 的 ≤0.2 元/条） | 未核实 |
| S5-V5 | 各厂商的模型名是否在具体账号的可用列表内（如方舟的 `ep-xxxx`） | 未验证（无 key） |
| S5-V6 | MiniMax 国际站点 `api.minimax.chat` 的可用性 | 未验证 |

## 九、本次不做的事（边界声明）

- 不做厂商自动切换（不做多厂商并发试探、不做自动降级）——切换始终是人工动作。
- 不做模型能力评测（不评“哪家写得好”）——本片只证“接得进去、调得通、计得清”。
- 不做价格 / 交期 / 合同决策——交给业务层与人工签核闸门（红线不变）。
