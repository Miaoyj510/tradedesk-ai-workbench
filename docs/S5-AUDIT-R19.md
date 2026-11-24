# S5 审核报告（R19）—— 审核 agent

> 审核对象：`main @ 9c51ad0` —— **S5 多云厂商 provider 层**（`a9f3002`）
> + **S5-1a 真模型出草稿**（`a3ee737`）+ **S5-1b/S5-1c 成本落库与模型层评测**（`878f360`）。
> 审核方式：**不复用** `scripts/verify_s5.py` 的断言，按 `docs/S5-ACCEPTANCE.md`
> 与 PRD（9.3 / 12.4 / 12.5 / 14.5）**重写**成 `scripts/audit_s5.py`
> （**54 项断言**，§S1—§S7）。
> 复核：`dev.ps1 audit5`（或 `python scripts/audit_s5.py`）。

## 一、结论

| 项 | 结果 |
| --- | --- |
| 审核断言 | **54**（SQLite；S5 与后端无关，不涉及 PG） |
| 通过 | 41 |
| **违约 FAIL** | **13** —— 归并为 **13 个根因**（见第三节） |
| 记账 WARN | 0 |
| 自证 `verify_s5.py` | 28/28 全绿（实现方自证） |
| 前序回归 | pytest 213 passed、`audit_s2` 106/106、`audit_s3` 90/90、`audit_s4` 85/85 —— **S5 没碰坏 S1—S4** |
| R18 收口 | R17 的 13 个根因确已修完（`e002015`），本轮未复发 |

**先说好话。S5 的三件事是真做成了，而且做在了正确的位置。**

- **厂商表真的表驱动**：换厂商 = 改一行 `TRADEDESK_LLM_PROVIDER` + 填一个 key；
  **基址地址按厂商分开**（每家各有 `base_url_env`）—— 切到阿里云不会拿 MiniMax 的地址去请求。
- **两种嵌入请求体各有一个客户端**：MiniMax 的 `{"model","texts","type"}` → `vectors`，
  其余四家的 OpenAI 兼容 `{"model","input"}` → `data[].embedding` —— **不用靠猜**。
- **错误分类是对的**：`HTTPError` ⊂ `URLError`，单独捕获后 401/403 **不重试**
  （否则一次 401 重试三次、每次都计费）；网络错误才重试。
- **思考块在解析层剔除**：`<think>...</think>` 不会被发给出买家。
- **模型看不到真值**：占位符机制是真的 —— `tests/test_draft_writer.py::test_model_never_sees_the_real_price`
  把"模型输入里不出现价格真值"钉成了断言。
- **数字白名单分层**（S5-1b/1c 的修正）：币种位置的数字必须在知识库真值里，
  其他位置的数字可以是买家原话里的 —— 这一条把 ar/es 的**误判**越权率从 70%/20% 拉回 0%。
- **红线没松**：模型只出草稿；含价格的草稿一律触发 GATE-01（强制人工签核）；
  合同必须人工确认；开关默认关、审核脚本强制离线。

**问题出在四个地方**：**厂商隔离只做了一半（地址做了，key 没做）**、
**越权判定只看阿拉伯数字**、**计量口径把进程累计值当单次值**、
**口径声明比实际乐观**（PRD 12.4 的硬指标结论）。

> 三个 P1 里，**R19-01（key 串厂商）是安全缺陷**，请优先收。

## 二、厂商层与模型层各做对了什么（不在这里重复挑刺）

见第一节。要补一句**本轮最重要的观察**：

> S5-1c 的"事实保真度 100%"这个**硬指标**，其有效性完全依赖
> `check_model_body` 的数字判定。而该判定只认阿拉伯数字（R19-03）。
> 评测器的"恶意假模型"也只写阿拉伯数字 —— **评测器与被评测对象共享同一个盲区**，
> 所以"100%"这个数字目前只能证明"写阿拉伯数字的编造被抓住了"。

## 三、违约项（13 个根因）

### R19-01 · 切厂商后 **API key 串厂商** —— 把 A 家的密钥发给 B 家（P1，安全）

**事实**：`S5` 修了"基址地址不串厂商"，但 **key 走的是同一条没修的路**。

`config.py:120-122`：

```python
llm_api_key=(get("TRADEDESK_LLM_API_KEY", "")
             or _vendor_env(_provider, "api_key_env")
             or get("MINIMAX_API_KEY", "")),      # ← 旧的 MiniMax key 兜底
```

`llm/providers.py:136-140`：

```python
for env in (provider.api_key_env, GENERIC_KEY_ENV):
    value = os.environ.get(env, "").strip()
    if value:
        return value
return (fallback or "").strip()                  # ← fallback = config.llm_api_key
```

于是：`.env` 里留着旧的 `MINIMAX_API_KEY`，把 `TRADEDESK_LLM_PROVIDER` 改成 `aliyun`
（**不填** `DASHSCOPE_API_KEY`）→ `config.llm_api_key` = MiniMax 的 key →
`build_chat_client` 拿它当阿里云的 key → **MiniMax 的密钥被发往 `dashscope.aliyuncs.com`**。

审核方实测（`audit_s5.py` §S2，断言 S5-12 / S5-13）：

```
TRADEDESK_LLM_PROVIDER=aliyun, MINIMAX_API_KEY=MINIMAX-SECRET
  client.base_url = https://dashscope.aliyuncs.com/compatible-mode
  client.api_key  = 'MINIMAX-SECRET'      ← 串了

TRADEDESK_LLM_PROVIDER=minimax, TRADEDESK_EMBEDDER_PROVIDER=zhipu
  embedder.base_url = https://open.bigmodel.cn/api/paas/v4
  embedder.api_key  = 'MINIMAX-SECRET'    ← 也串了
```

**为什么这是 P1**：`.env.example` 里 `MINIMAX_API_KEY=` 是**留着的**（用户填过就不会删），
而"换厂商"的官方指引就是"改一行 provider + 填一个 key"。两个条件凑齐 → **凭据泄露给另一家公司**，
而且**没有任何报错**（对方返回 401，用户只会以为 key 填错）。

**修法**：`config.llm_api_key` 不该做跨厂商兜底；`resolve_api_key` 的 fallback 也不该
拿"对话厂商的 key"去喂"嵌入厂商"。要么按厂商各取各的 key（取不到就**不发请求**），
要么至少在 key 的来源厂商与目标厂商不一致时**拒绝建客户端**并说明原因。

---

### R19-02 · PRD 12.4 硬指标的"达标"结论**没覆盖端到端**（P1，口径）

**事实**：PRD 12.4 / NFR-COST-01 的硬指标是

> **单位询盘 Token 成本 ≤ 0.2 元/条**

"单位询盘"= 一条询盘的全部 Token 成本。而每条询盘除了 1 次对话调用，还有
**1 次云端查询嵌入调用**（S4 的既定口径，见 `CLAUDE.md`"每条询盘只能 1 次云端嵌入调用"）。
**embo-01 的单价至今没核实**（待验证项 S5-V4 / V-20）。

`docs/S5-ACCEPTANCE.md` 自己在两个地方给了两个答案：

| 位置 | 结论 |
| --- | --- |
| 第四行「四、计量与成本口径」 | "PRD 12.4 的硬指标…**在单价核实前无法判定**"（第 70 行） |
| 第二部分「六、成本口径」 | "PRD 12.4 硬指标 \| ≤ ¥0.2 / 条 → **达标**（余量约 9–38 倍）"（第 210 行） |
| 第四部分「S5-1c」 | "仍在 PRD 12.4 的 ¥0.2/条 之内（余量约 11–13 倍）"（第 365 行） |

**而 PRD 12.6 里他们自己回填的是**："实测回填对话部分单位成本（¥0.0154~0.0182/条），
并明确**端到端总成本仍不能判定**（嵌入单价未找到）"。

→ 同一件事，**PRD 里说"不能判定"，验收基线里说"达标"**。
"余量 9–38 倍 / 11–13 倍"这个倍数是拿**只对话**的成本算的，
按端到端口径这个倍数**不成立**（embo-01 的单价可能是 0，也可能是能吃掉余量的量级）。

**修法**：结论改成"**对话部分**达标（¥0.0154~0.0182/条）；**端到端不可判定**，
待 embo-01 单价核实后（V-20）再判 PRD 12.4"。余量倍数要么删掉，要么注明是"仅对话部分"。

---

### R19-03 · 越权判定**只认阿拉伯数字** → 模型把价格写成英文单词即可放行（P1，对外失真）

**事实**：`draft_writer.py:38`

```python
NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
```

数字白名单（`check_model_body`）的全部依据是 `NUMBER_RE`。于是：

```python
body = ("... our reference price is {{price_range}} ... "
        "We can actually do it for four dollars fifty per piece.")
check_model_body(body)   # → []   放行！
```

审核方实测（断言 S5-45）：判定器返回**空问题列表**，回填后正文变成：

> "…our reference price is **USD 4.85–5.20 per unit**… **We can actually do it for four
> dollars fifty per piece**."

**真值与幻觉同时出现在一封对外回复里。** 这直接证伪了 S5-ACCEPTANCE 里那句话：

> "把数值从模型**输入**里拿掉，它就**物理上**没有可抄错的真值。"

—— **抄不到 ≠ 编不出**。占位符机制挡住的是"抄"，挡住不了"编"。
挡住"抄"的是结构性保证；挡住"编"目前只有这一道数字判定，而它有盲区。

**为什么严重**：

1. GATE-01 的价格模式（`runtime/gates.py:21-25`）同样只认数字 →
   单词价格**不会**额外触发任何新闸门（好在草稿里总还有真值价格，GATE-01 本来就会命中，
   人工签核这道防线还在 —— 但"自动越权拦截"这一层是漏的）。
2. S5-1c 把"**事实保真度 100%**"列为**硬指标**，而它的度量（`eval_llm.py:256` 的
   `numbers_in(body)`）与判定器**用的是同一套数字正则** →
   **评测器与被评测对象共享盲区**，"100%"只证明了"写阿拉伯数字的编造被抓住了"。
3. 评测的"层 B 恶意假模型"也只写阿拉伯数字 → **评测器自身的有效性没被证明**。

**修法**：数字判定要能覆盖**数字的词形**（four / two thousand / twelve fifty），
或者换个思路 —— 不要靠"扫描数字"来判越权，而是靠**结构**：
正文里**只允许出现白名单里的占位符与连接词**，任何"数值位置的自由文本"一律拒。
（前者难，后者稳：把"价格数字"限定为只能由 `{{price_range}}` 产生，
出现 `{{price_range}}` 之外的价格措辞 —— 如 `price` / `报价` 附近的名词短语 —— 就拒。）

---

### R19-04 · `llm_usage.calls` 是**进程累计值** → 成本看板的调用次数按 N(N+1)/2 膨胀（P2，计量）

**事实**：`draft_writer.py:257`

```python
calls=getattr(self.chat, "calls", 0),      # ← ChatClient 的累计计数，从不复位
```

`ChatClient.calls` 在 `chat.py:161` 每次 HTTP 调用后自增，**进程内永不复位**。
`supervisor.py:341` 把它**按行落库**：

```python
http_calls=int(result.llm_usage.get("calls") or 0),
```

`store/base.py:46` 汇总时又**求和**：

```python
http_calls += int(r.get("http_calls") or 0)
```

审核方实测（断言 S5-57）：三行分别是 1 / 2 / 3 次调用（累计值）→ 汇总显示 **6 次**，
真实调用数是 **3 次**。N 条询盘 → 汇总显示 N(N+1)/2。

**后果**：PRD 12.4 的成本看板（`GET /usage/costs`）上的"HTTP 调用次数"**不可用**；
`scripts/eval_llm.py:243` 的 `b["http_calls"] += ...` 同样累加累计值 → 评测报表里的调用次数也是膨胀的。

**修法**：`ModelDraftWriter.write` 里取**本次**的调用数（调用前记下基准值相减），
或者把 `ChatClient.calls` 改成"本次出稿"的计数（在 `write()` 里归零）。

---

### R19-05 · 越权被拒的那次调用**token 记 0** —— 明明响应里有 usage（P2，成本低估）

**事实**：`S5-1b` 已经意识到"降级也要记钱"（`inquiry.py:344-348`），
但把 `prompt_tokens / completion_tokens / total_tokens` 一律写成 0，
文档里的理由是：

> "**失败的响应里没有 `usage` 字段** → 那一次的 token 数拿不到 → `cost_cny` 只能空着。"

**这个理由只对"调用失败"成立，对"越权被拒"不成立**：越权被拒时模型**已经返回了正文**
（正因为有正文才被判定器拒），响应里**有 `usage`**（`ChatResult` 已经把它解析出来了）。
审核方实测（断言 S5-52）：

```
HTTP 调用 1 次，但记的是：
{'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0,
 'calls': 1, 'cost_cny': None, 'degraded': True, 'reject_reason': '模型写出了价格数字 12.50…'}
```

→ **这次调用花了钱，但账上是 0 元 / 0 token**。`DraftRejected` 在
`draft_writer.py:239-241` 抛出时，`result` 就在手边（token 拿得到），只是没带上。

**修法**：`DraftRejected` 带上 `usage`（prompt/completion/total tokens + provider/model），
降级行按真实 token 计价。失败调用（HTTP 错误）才留 `cost_cny=None`。

---

### R19-06 · 必填事实槽位为空时**照样出稿**（残缺正文；价格为空时 GATE-01 还不命中）（P2，事实）

**事实**：`draft_writer.py:176`

```python
out = out.replace(token, "" if value is None else str(value))
```

事实槽位为空 / None 时，**直接替换成空串**，没有任何校验。审核方实测（断言 S5-48）：

```
facts = {"sku": "X-200", "moq": "", "price_range": "", "terms": "FOB", ...}
→ 照样出稿："Dear buyer, thank you for your enquiry about X-200. For  units …"
```

出稿里会出现 "For  units"、"on  basis from " 这种漏了值的句子；
价格真值为空时是 "USD None–None per unit"。

**为什么这条不只是难看**：`runtime/gates.py:21-25` 的价格模式**要求有数字**
（`(?:USD|EUR|\$|€)\s?\d`、`price…\D{0,12}\d`）→ "USD None–None" **不命中 GATE-01** →
**一份没有真实价格的报价草稿可以绕过价格签核闸门**。

**当前是否触发**：知识库种子（`data/kb_seed.py`）三个型号的
`moq / price_low / price_high / terms / port / lead_time_days` **都是齐的** →
**今天不触发**。但没有任何一层校验它，改知识库 / 加型号 / 换供应商时就会踩。
（`inquiry.py:256` 只检查了 `price_low` 与 `lead_time_days` **两个键在不在**，
不检查值是不是空，也不检查 `moq/terms/port`。）

**修法**：`render_facts` 对**必填**槽位的空值一律抛 `DraftRejected`（宁可不发，不要发残缺的）。

---

### R19-07 · S5 仍然**不在交付门禁**里（P2，流程）

**事实**：`dev.ps1 check` 仍是九步：语法 → S1 双后端 → S2 自证 → S3 自证 → S2 审核 →
pytest → S3 审核 → S4 验证 → S4 审核。**没有 S5**。

S5 已经有 `verify_s5.py`（28 项）与本轮的 `audit_s5.py`（54 项），
但**都不在门禁里** → S5 的回归（厂商表改错、提示词改坏、判定器改松）**不会被 check 拦住**。

**修法**：修完 R19 后，把 `audit_s5.py` 作为**第十步**加进 `dev.ps1 check`
（本轮已加 `dev.ps1 audit5` 独立命令，但**没有**动 `check` ——
铁律六要求"提交前 check 全绿"，本轮审核线是红的，所以不进 check）。

---

### R19-08 · 文档声明的 **key 优先级与实现相反**（P3，口径）

`.env.example:22` 与 `docs/S5-ACCEPTANCE.md` 第五节第 3 条都写：

> "优先级：通用 `TRADEDESK_LLM_API_KEY` > 厂商专用 > 旧的 `MINIMAX_API_KEY`"

**实现是反的**（`providers.py:136`）：`厂商专用 > 通用 > fallback`，
`tests/test_llm_providers.py::test_api_key_prefers_vendor_env` 还把它**钉成了断言**。

审核方实测（断言 S5-60）：同时设 `TRADEDESK_LLM_API_KEY=GENERIC` 与 `MINIMAX_API_KEY=VENDOR`
→ 实现返回 `'VENDOR'`（厂商专用），与文档声明的顺序相反。

两个 key 都配了的时候，谁生效要看实现而不是看文档 —— 这正是本项目栽过多次的坑
（R11-07 / R15-07 / R17-05 都是"文档写的 ≠ 代码做的"）。

**修法**：要么改实现、要么改文档，**两边必须一致**；并把断言改成"按文档声明的顺序"。

---

### R19-09 · `prompts.py` 引用的 PRD 章节号**指错了地方**（P3，引用错位）

`src/tradedesk/agents/prompts.py:2`：

> `"""询盘回复的提示词资产（S5-1a；PRD 附录 14.6 清单）。`

**PRD 14.6 是「数据合成方法（附录）」**；**Prompt 资产清单是 14.5**（P-01~P-12）。
审核方实测（断言 S5-61）：引用 14.6 → 「数据合成方法（附录）」。

本项目的验收基线以"条款号 = 判据"为准，引用错位会让下一个人去错的地方找依据。

**顺带**：PRD 14.5 的 **P-06（术语标准译注入片段）**、**P-12（拒答与降级话术）**、
**P-02（输出 JSON Schema 约束）** 在 S5 各片里**没有实现**，
而 `docs/S5-ACCEPTANCE.md` 第九节「本次不做的事（边界声明）」只列了三条，
**没有把 P-06/P-12/P-02 声明为不在本期范围**。

**修法**：章节号改 14.5；边界声明补上未实现的 PRD 资产编号。

---

### R19-10 · `estimate_cost` 注释说"取最保守"，代码取的是**最便宜**的一档（P3，口径）

`llm/pricing.py:83`：

```python
p = min(tiers, key=lambda x: x.input_per_mtok * x.output_per_mtok)
```

文档字符串写的是"多档位时取**标准档（最保守的一档，避免低估成本）**"。
`min` 取的是**最便宜**的一档。今天的表里 `MiniMax-M3` 有两档（非五折）：
标准档 4.20/16.80 与 >512k 档 8.40/33.60 → `min` 选中标准档，**结论恰好对**；
但一旦表里多一个更便宜的档位，估算就会**低估**。

审核方实测（断言 S5-55）：把表换成"标准档 4.20/16.80 + 特惠档 1.00/1.00"，
`estimate_cost` 返回 **¥2.0**，而保守值应为 **¥21.0**。

**修法**：要么按"最贵的一档"取，要么把注释改成"取最低档（乐观口径）"——
**代码与声明必须一致**。

---

### R19-11 · `{{cert_sentence}}` 是**整句**，占位符语义没写"整句替换"（P3，提示词）

`prompts.py:69`：

```python
PLACEHOLDER_CERT: "the certification statement covering this model",
```

没说"这是**一整句**，直接放进去，不要再套壳"。模型若写成
"The model is covered by {{cert_sentence}}." → 回填后是
"The model is covered by **The model is covered by certificate CERT-CE-2041 (CE),
valid until 2027-03-31.**." —— 复读 + 双句号。

**证据就在他们自己的文档里**：`docs/S5-ACCEPTANCE.md` 第七节「真实草稿样例」：

> "…valid until 2027-03-31**..** To prepare an accurate quotation…"

—— 同一个句号出现了两次。这次没复读纯属模型当次写对了。

**修法**：占位符说明里写明"整句插入，不要加前缀/后缀"（其余几个占位符是短语，语义不同，也要写清）。

---

### R19-12 · 模型层评测的**平均成本只统计成功行**（P3，口径）

`eval_llm.py:273`：

```python
b["avg_cost_cny"] = (b["cost_cny"] / b["priced"]) if b["priced"] else None
```

`priced` 只数 `cost_cny is not None` 的行 —— **降级行（越权被拒）的成本是 `None`，被排除在外**。
而降级行恰恰是**花过钱**的（见 R19-05）。

本轮真实评测里越权率 0%，所以这次报出的 ¥0.0154~0.0182 **恰好不受影响**；
但**评测口径本身**没有排除降级调用 —— 一旦线上有 10% 的降级，
报出来的"单位成本"会比真实值低 10% 左右。评测报表也没有声明这个口径。

**修法**：`avg_cost_cny` 的分母改成"全部案例"，或另报一个"含降级调用的总成本/条"。

---

### R19-13 · `RemoteEmbedder` 把路径**写死** → 厂商表里的 `embedding_path` 对 MiniMax 无效（P3，表）

`retrieval/embedders.py:121-125`：

```python
def _url(self) -> str:
    url = f"{self.base_url}/v1/embeddings"
```

厂商表里 MiniMax 的 `embedding_path="/v1/embeddings"` **从不生效**（工厂走 minimax 分支时
不把 path 传进去）。改表里的路径不会改行为 —— **表不是唯一真源**，
与本项目"库版本号只有一份""价格表只在一处维护"的规矩相悖。

**修法**：MiniMax 分支也按 `provider.embedding_path` 拼 URL。

---

## 四、记账项（不违约，但要记）

| # | 事项 |
| --- | --- |
| 1 | **各厂商嵌入维度全是标称值**（S5-V1~V3）：阿里云 / 智谱 / 火山三家的维度**未实测**，维度不符会在客户端直接报错 —— 报错快，不危险，但切厂商前必须先核实 |
| 2 | **`CURRENCY_NUM_RE` 只认 USD/EUR/$/€/¥**：模型写 "AED 55" 时走的是"其他位置数字"分支 → 仍会被拒（不在真值里），但错误信息说的是"自行写出了数字"而不是"价格"，排查时会绕一点路 |
| 3 | **`base_url` 通用覆盖仍然能跨厂商**：`TRADEDESK_LLM_BASE_URL` 会覆盖一切厂商的基址 —— 这是设计意图（自建代理网关要用），但与 R19-01 叠加时更危险 |
| 4 | **`ChatClient` 没有 `GroupId`**：MiniMax 部分账号的接口需要 `GroupId` 查询参数，表里 `extra_query` 是空的（国际站可能有差异）—— 切国际站时可能 400 |

## 五、复核方式

```powershell
# 审核断言（54 项；当前 13 FAIL）
& "D:\桌面\AIagent_project\外贸项目\.worktrees\agent-b\dev.ps1" audit5

# 或直接跑
& ".\.venv\Scripts\python.exe" scripts\audit_s5.py
& ".\.venv\Scripts\python.exe" scripts\audit_s5.py --json
```

审核脚本**不联网、不计费**：全部用假传输 / 假客户端，
断言的唯一输入是**知识库真种子**（数值是真值）。

## 六、建议的处理顺序

| 顺序 | 根因 | 为什么这个顺序 |
| --- | --- | --- |
| 1 | **R19-01**（key 串厂商） | 安全缺陷；改动小（配置层 + 工厂），但越晚修越可能多发几天凭据 |
| 2 | **R19-03**（数字词形盲区）+ **R19-11**（整句语义） | 这两条决定"模型编价"这个**核心主张**成不成立 —— 主张不成立，验收基线的第一句话就得改 |
| 3 | **R19-04 / R19-05**（计量） | 成本看板现在拿的是**错数**（N(N+1)/2 次调用、降级调用 0 元）。**先修口径，再谈达标** |
| 4 | **R19-02**（达标结论） | 口径类：等 3 修完，数字准了再把结论写成"对话部分达标 / 端到端待定" |
| 5 | **R19-06**（空事实）、**R19-07**（门禁） | 一个是防患、一个是把这次修的东西**锁进门禁**（改完第 7 条，R19 的回归就再也跑不掉了） |
| 6 | R19-08~R19-13 | 口径 / 引用 / 表的一致性 |

## 七、给实现 agent 的一句话

> S5 的骨架是对的（表驱动、两种嵌入形态各一个客户端、错误分类、思考块剔除、
> 占位符机制、开关默认关、审核强制离线）—— **不要重写**。
> 这一轮全是**边角**：key 的隔离、判定的盲区、计数的口径、文档与代码的措辞。
> 但请注意 R19-02 与 R19-03 的性质：
> **你们对外报出去的"模型编不出价格"和"≤0.2 元/条达标"这两句话，目前都比实际弱。**
> 要么把机制补上（词形数字 / 端到端单价），要么把话收回来 —— 两句都成立最好。

## 附录 · 审核方探针（可复现）

```python
# R19-01：key 串厂商
os.environ["MINIMAX_API_KEY"] = "MINIMAX-SECRET"
os.environ["TRADEDESK_LLM_PROVIDER"] = "aliyun"
cfg = Config.load(env_file="__no__")
build_chat_client(cfg).api_key      # → 'MINIMAX-SECRET'（本该是空的或阿里云的）

# R19-03：数字词形盲区
check_model_body(GOOD_BODY + " We can do it for four dollars fifty per piece.")   # → []

# R19-04：累计值
# 同一个 ChatClient 连出两稿 → 第 2 稿的 md.calls == 2

# R19-06：空事实照样出稿
render_facts(GOOD_BODY, {**facts, "moq": "", "price_range": ""})   # → 不抛错，照样出稿
```
