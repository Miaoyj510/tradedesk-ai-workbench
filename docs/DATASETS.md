# 数据集采购清单与口径澄清（S4-4c 续 / PRD 9.5）

> 落库日期：2026-11-24　｜　来源：**ModelScope 直连（免登录、免 token）**
> 下载器：`scripts/fetch_datasets.py`　｜　机器可读清单：`data/datasets/manifest.json`（不入库）
> 本文件是 PRD 第 9 章、第 12 章的**证据附录**：任何引用这些数据的结论，都必须能回指到本文件的条目编号。

---

## 一、为什么换源：HuggingFace 在本机不可达

| 站点 | 443 连通（2026-11-24 实测） | 结论 |
| --- | --- | --- |
| `www.modelscope.cn` | ✅ 可达 | **本项目的取数通道** |
| `www.kaggle.com` | ✅ 可达 | 但公开检索接口需登录令牌，slug 无法离线核对 |
| `huggingface.co` | ❌ **不可达** | 原本的取数通道失效 |

ModelScope 上这几个基准是 HuggingFace 的**官方镜像**（`sentence-transformers/*`、`MTEB/*`、`C-MTEB/*`），内容一致。

---

## 二、采购清单

**层 A = 体检层**（PRD 9.5.1）：公开检索基准，只证明**多语种检索管线没问题**
**层 B = 语料种子**（PRD 9.5.2）：真实询盘语料**仍必须自建**，这里只提供中文侧的种子与标签体系参考

| 编号 | 数据集 | ModelScope 路径 | 语种 | 层 | 体积 | PRD 依据 |
| --- | --- | --- | --- | --- | --- | --- |
| **DS-01** | MIRACL 多语种检索基准 | `sentence-transformers/miracl` | ar / es / zh / en（本机取 4 语；上游共 16 语） | A | 9.0 MB | 9.5 体检层 |
| **DS-02** | MLDR 多语种**长文档**检索基准 | `sentence-transformers/mldr` | ar / es / **pt** / zh（上游共 13 语） | A | 345 MB | 9.5 体检层 |
| **DS-03** | Mr. TyDi 多语种检索基准 | `sentence-transformers/mr-tydi` | ar / en（上游共 11 语） | A | 15.0 MB | 9.5 体检层 |
| **DS-04** | MKQA 多语种知识问答检索基准 | `MTEB/MKQARetrieval` | ar / es / **pt**（上游 26 语） | A | 1.6 MB | 9.5 体检层 |
| **DS-05** | mMARCO 多语种检索语料 | `C-MTEB/MMarcoRetrieval` | ⚠️ **实为中文**，见 §4.1 | A | 19.5 MB | 9.5 体检层（**口径已修正**） |
| **DS-06** | 京东商品评论数据集（JDDC） | `DAMO_NLP/jd` | zh（纯中文单语） | B | 5.7 MB | 3.5 数据可得性缺口；9.7 |
| **DS-07** | 中文外贸意图标注数据集 | `yangzailu/chinese-foreign-trade-intent-annotation-dataset` | zh | B | 0.02 MB | 9.6 意图体系重映射种子 |

### 语种覆盖对照（**实测自文件内容，不是抄上游说明**）

| 基准 | ar | es | pt | zh | 语种分层评测能否靠它 |
| --- | --- | --- | --- | --- | --- |
| DS-02 MLDR | ✅ | ✅ | ✅ | ✅ | **能 —— 唯一四语齐全** |
| DS-01 MIRACL | ✅ | ✅ | ❌ | ✅ | 能（ar/es/zh 三语） |
| DS-03 Mr. TyDi | ✅ | ❌ | ❌ | ❌ | 只能做 ar 单语对照 |
| DS-04 MKQA | ✅ | ✅ | ✅ | zh_cn/hk/tw | 能（ar/es/pt 三语） |
| DS-05 mMARCO | ❌ | ❌ | ❌ | ✅ | **不能**（该镜像无语言字段、实为中文） |

> **结论**：PRD 12.3 要求的"分语种分层评测"，在体检层由 **DS-02（MLDR）+ DS-01 + DS-04** 承担；
> `pt` 语种在体检层可覆盖，但**在询盘语料层仍然缺位**（PRD 9.7 的自建范围是 en / es / ar）。

---

## 三、文件结构（供写 loader 时对齐）

| 数据集 | 文件 | 列 / 字段 |
| --- | --- | --- |
| DS-01 / DS-02 / DS-03 | `<lang>-triplet/train-*.parquet` | `anchor`（查询）、`positive`（相关文档）、`negative`（负例） |
| DS-04 | `<lang>-corpus/` `<lang>-queries/` `<lang>-qrels/` | corpus：`id` `text` `title`；queries：`id` `text`；qrels：`query-id` `corpus-id` `score` |
| DS-05 | `data/corpus-*.parquet` `data/queries-*.parquet` | `id` `text`（**无语言字段**） |
| DS-06 | `train.csv` `dev.csv` | `sentence` `label` `dataset`（`label` 为情感二分类） |
| DS-07 | `wm.json` | `id` `query` `label`（⚠️ 源文件有语法错误，见 §4.3） |

---

## 四、口径澄清 —— 三条必须写进 PRD 的修正

### 4.1 mMARCO：可得镜像**不是多语版本**（DS-05）

实测 6980 条 query 中 **6977 条含汉字（99.9%）**，阿语 0 条。`C-MTEB/MMarcoRetrieval` 是 **CMTEB 的中文 mMARCO 切片**。

- PRD 3.5 与 9.5 写的"mMARCO 含 ar/es/pt/zh"**在本机可得的镜像上不成立**。
- 处理：**分语种体检层不依赖 mMARCO**，改由 MLDR / MIRACL / MKQA 承担；mMARCO 只作"整库规模 + 检索管线连通性"的补充用例。
- 登记为待验证项 **V-24**：真正的多语 mMARCO（带 `language` 字段）需另行找源或改用其他多语基准替代。

### 4.2 JDDC：是**商品评论**，不是"对话语料"（DS-06）

实测 `train.csv` 为 `sentence` + `label`（1: 22424 条 / 0: 22588 条 / 空行 354 条）的**情感二分类**，无多轮对话结构。

- PRD 3.5 把淘宝 ECD、京东 JDDC 一并称作"电商**对话**语料"，对 JDDC 的描述不够准确。
- 处理：JDDC 的可用价值是**中文电商短文本的意图/槽位提炼**，不是多语种评测集；PRD 3.5 表述需微调为"电商评论/短文本语料（淘宝 ECD、京东 JDDC）"。
- 登记为待验证项 **V-25**：淘宝 ECD 的真实字段结构待核实（未在 ModelScope 找到可下载的官方镜像）。

### 4.3 DS-07 源文件有 JSON 语法错误

`wm.json` 第 18 行 `"id":. "w4"` —— 非法 JSON。修正该处后可解析，共 **10 条**，标签 10 类：
`业务沟通 / 付款方式 / 出口流程 / 合同单据 / 客户开发 / 市场行情 / 清关物流 / 物流运费 / 认证资质 / 贸易术语`。

- 这 10 类与 PRD 9.6 的**询盘五类（询价 / 打样 / 规格 / 物流 / 投诉）**不是一一对应关系，而是**上位话题**，可作为重映射的锚点，但**不足以直接当评测集**（样本量太小）。
- 处理：loader 侧做容错修正；该数据集**只登记为标签体系种子**。

---

## 五、这一批数据**解决不了**什么（写实边界）

| 问题 | 本批数据能否解决 | 说明 |
| --- | --- | --- |
| 多语种**检索管线**体检（ar/es/pt/zh） | ✅ 能 | DS-01/02/04 |
| 「询盘场景做得好」的证明 | ❌ **不能** | 体检层 ≠ 业务层（PRD 9.5 两层口径，不可混用） |
| ar / es / **en** 真实询盘语料 | ❌ 不能 | **必须自建**（PRD 9.7：C1–C7，约 46–66 人日） |
| **pt** 语种的询盘语料 | ❌ 不能 | 公开语料**完全缺位**；PRD 9.7 自建范围也未含 pt |
| 制裁 / 出口管制**真实名单** | ❌ 不能 | 需商用授权（PRD 13.6 R-02、D-04、V-06） |
| 真实买家流量下的灰度对照 | ❌ 不能 | 需真实流量（PRD 12.5 门槛 10） |

---

## 六、复现命令

```powershell
$py = "D:\桌面\AIagent_project\外贸项目\.venv\Scripts\python.exe"

& $py scripts\fetch_datasets.py --list      # 看清单
& $py scripts\fetch_datasets.py --dry-run   # 看将要下什么
& $py scripts\fetch_datasets.py             # 全量下载（约 400MB）
& $py scripts\fetch_datasets.py --only mldr --langs ar,es,pt,zh
& $py scripts\fetch_datasets.py --verify    # 校验 SHA256
```

依赖：`pyarrow`（读 parquet，已装入 `.venv`）。落盘目录 `data/datasets/` 已被 `.gitignore` 忽略，不入库。

---

## 七、版权与使用边界

| 数据集 | 上游许可（**未逐条复核，以 ModelScope 页面为准**） | 本项目用途 |
| --- | --- | --- |
| DS-01 MIRACL | 见 ModelScope 页（原始 CC BY-NC 4.0，**非商用**） | 内部评测，**不得用于商业模型训练** |
| DS-02 MLDR | 见 ModelScope 页 | 内部评测 |
| DS-03 Mr. TyDi | 见 ModelScope 页（原始 Apache-2.0） | 内部评测 |
| DS-04 MKQA | 见 ModelScope 页（原始 CC BY-SA 4.0） | 内部评测 |
| DS-05 mMARCO | 见 ModelScope 页 | 内部评测 |
| DS-06 JDDC | 见 ModelScope 页 | 中文短文本参考 |
| DS-07 外贸意图 | 见 ModelScope 页 | 标签体系种子 |

> **本项目是作品集 / 求职 Demo**，非商用；若后续商业化，DS-01 / DS-04 的非商用条款必须重新评估。
> 全部许可以下载时 ModelScope 页面标注为准，**未找到**统一的可商用授权证据 → 登记为待验证项 **V-26**。

---

## 八、待验证项（本批新增，编号接 PRD 14.3 的 V-23 之后）

| 编号 | 待验证内容 | 影响 | 现状 |
| --- | --- | --- | --- |
| **V-24** | 多语 mMARCO（带语言字段）的真实来源 | 体检层的 ar/es/pt/zh 覆盖 | 未找到；已用 MLDR/MIRACL/MKQA 兜底 |
| **V-25** | 淘宝 ECD 的真实字段结构与可下载镜像 | 中文电商对话/短文本语料 | 未找到 |
| **V-26** | 上述数据集的可商用授权证据 | 商业化可行性 | 未找到 |

---

## 九、本章要点

1. 本批取数全部来自 ModelScope 直连（HuggingFace 本机不可达），已落盘并带 SHA256 清单，可一键复现。
2. 体检层语种由 **MLDR（ar/es/pt/zh 齐全）** 承担；mMARCO 的可得镜像实为中文、JDDC 实为评论二分类，两处已按实测修正 PRD 口径。
3. **这批数据解决不了「询盘场景做得好」** —— ar/es/en 询盘语料仍必须按 PRD 9.7 自建，且 **pt 语种公开语料完全缺位**。
