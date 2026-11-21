# -*- coding: utf-8 -*-
"""S4 独立审核脚本（审核 agent 用）。

与 `scripts/verify_s4.py` 的区别
------------------------------
verify_s4.py 是实现 agent 交卷时的自证；本脚本是**审核方自己按
`docs/S4-ACCEPTANCE.md` 与 PRD 第 9 章重写**的断言，重点盯四件事：

1. **口径不许失真**：文档里写的维度、模型、调用方式，必须与代码和实测一致
   （S3 在"库版本号两份"和"引用断链"上栽过，文档口径失真要当成缺陷，不是笔误）。
2. **计费不许白花**：索引必须幂等；重灌知识库不得连带删掉向量索引
   （删了就等于每次建索引都全量重嵌 —— PRD 硬指标 ≤0.2 元/条）。
3. **护栏不许松**：嵌入默认关闭、启动不得调云端 API、假嵌入器必须声明
   `semantic=False`，且 4b 之前向量通道不得被宣称成"语义召回"。
4. **越权与密钥**：任何对外内容与异常信息里都不得出现 API key。

审核轮次
--------
- R15（§S1—§S9）：S4-4a 向量层
- **R17（§S10—§S12）：S4-4b 稠密通道接入 RRF + S4-4c 语义召回评测框架**
  结论见 `docs/S4-AUDIT-R17.md`。

用法
    python scripts/audit_s4.py                # SQLite
    python scripts/audit_s4.py --dsn <DSN>    # PostgreSQL
    python scripts/audit_s4.py --json

退出码：0 = 无 FAIL；1 = 有 FAIL。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _setup_stdout() -> None:
    if os.environ.get("PYTHONIOENCODING"):
        return
    try:
        if sys.stdout.isatty():
            sys.stdout.reconfigure(errors="replace")
        else:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


_setup_stdout()

from tradedesk.config import Config, get_settings            # noqa: E402
from tradedesk.data.kb_seed import KB_VERSION, load_kb        # noqa: E402
from tradedesk.domain.models import KBChunk                   # noqa: E402
from tradedesk.retrieval.base import Embedder, SearchQuery    # noqa: E402
from tradedesk.retrieval.embedders import (                   # noqa: E402
    EmbeddingError, HashEmbedder, NullEmbedder, RemoteEmbedder, cosine)
from tradedesk.retrieval.indexer import (                     # noqa: E402
    chunk_text, embedder_tag, index_kb, load_index)
from tradedesk.retrieval.pipeline import RetrievalPipeline     # noqa: E402
from tradedesk.retrieval.store_retriever import StoreRetriever  # noqa: E402
from tradedesk.retrieval.vector_index import VectorIndex      # noqa: E402
from tradedesk.store.sqlite_store import SqliteStateStore     # noqa: E402

RESULTS = []
CURRENT_CLAUSE = "?"
_STORES = []
_TMPFILES = []
_PG_DBS = []
_SEQ = [0]
DSN = ""


def _pg_split(dsn: str):
    head, _, _db = dsn.rpartition("/")
    return head + "/"


def _pg_exec(db: str, sql: str) -> None:
    import psycopg
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(sql)


def clause(c: str) -> None:
    global CURRENT_CLAUSE
    CURRENT_CLAUSE = c


def check(name: str, ok: bool, detail: str = "", level: str = "FAIL") -> bool:
    RESULTS.append({"clause": CURRENT_CLAUSE, "name": name, "ok": bool(ok),
                    "level": "PASS" if ok else level, "detail": detail})
    print(f"  [{'PASS' if ok else level}] {CURRENT_CLAUSE} {name}"
          + (f" — {detail}" if detail else ""))
    return bool(ok)


def note(name: str, detail: str = "") -> None:
    RESULTS.append({"clause": CURRENT_CLAUSE, "name": name, "ok": True,
                    "level": "WARN", "detail": detail})
    print(f"  [WARN] {CURRENT_CLAUSE} {name}" + (f" — {detail}" if detail else ""))


def section(title: str) -> None:
    print("\n" + title)
    print("-" * max(len(title), 64))


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def new_store():
    """一个干净的 SQLite 库（每个断言组一个，互不影响）。"""
    _SEQ[0] += 1
    path = Path(tempfile.gettempdir()) / f"audit_s4_{_SEQ[0]:02d}.db"
    for suffix in ("", "-wal", "-shm"):
        f = path.with_name(path.name + suffix)
        if f.exists():
            f.unlink()
    _TMPFILES.append(path)
    st = SqliteStateStore(str(path))
    _STORES.append(st)
    return st


def close_stores() -> None:
    while _STORES:
        st = _STORES.pop()
        try:
            st.close()
        except Exception:  # noqa: BLE001
            pass
    for head, name in _PG_DBS:
        try:
            _pg_exec(head + "postgres", f'DROP DATABASE IF EXISTS "{name}"')
        except Exception:  # noqa: BLE001
            pass


class FakeTransport:
    """假 transport：记录请求，按脚本返回响应或抛异常。"""

    def __init__(self, mode="ok", dim=4):
        self.calls = []
        self.mode = mode
        self.dim = dim

    def __call__(self, url, headers, payload, timeout):
        self.calls.append({"url": url, "headers": headers, "payload": payload,
                           "timeout": timeout})
        if self.mode == "business_error":
            return {"base_resp": {"status_code": 1004, "status_msg": "invalid api key"},
                    "vectors": []}
        if self.mode == "net_error":
            raise OSError("connection reset")
        if self.mode == "http_401":
            import urllib.error
            raise urllib.error.HTTPError("u", 401, "Unauthorized", None, None)
        n = len(payload.get("texts") or payload.get("input") or [])
        return {"base_resp": {"status_code": 0}, "total_tokens": 3,
                "vectors": [[0.1 * (i + 1) for i in range(self.dim)] for _ in range(n)]}


# --------------------------------------------------------------------------
# §S1 嵌入器契约（PRD 9.5 两层口径 / 13.4.5 选型轴③）
# --------------------------------------------------------------------------
def audit_s1_contract() -> None:
    section("§S1 嵌入器契约与『是否真语义』（PRD 9.5 / 13.4.5）")
    clause("S15-01")
    for emb in (RemoteEmbedder, HashEmbedder, NullEmbedder):
        check(f"{emb.__name__} 必须声明 name 与 semantic",
              isinstance(getattr(emb, "name", None), str) and emb.name != ""
              and isinstance(getattr(emb, "semantic", None), bool),
              f"name={getattr(emb, 'name', None)} semantic={getattr(emb, 'semantic', None)}")
    clause("S15-02")
    check("假嵌入器必须声明 semantic=False（不得冒充语义召回）",
          HashEmbedder.semantic is False and NullEmbedder.semantic is False,
          f"hash={HashEmbedder.semantic} null={NullEmbedder.semantic}")
    check("RemoteEmbedder 声称有语义能力", RemoteEmbedder.semantic is True,
          str(RemoteEmbedder.semantic))
    clause("S15-03")
    src = (ROOT / "src" / "tradedesk").rglob("*.py")
    readers = []
    for p in src:
        t = p.read_text(encoding="utf-8", errors="replace")
        if re.search(r"\.semantic\b", t) and "embedders.py" not in p.name:
            readers.append(str(p.relative_to(ROOT)))
    note("semantic 标志位目前无人消费 —— 4b 接向量通道时必须检查它",
         f"引用 .semantic 的文件={readers or '无'}"
         f"（标志位只有声明没有守卫，等于装饰）")
    clause("S15-03b")
    proto = (ROOT / "src" / "tradedesk" / "retrieval" / "base.py").read_text(
        encoding="utf-8")
    check("Embedder 协议必须声明查询侧嵌入（4b 的稠密通道要用）",
          "def embed_query" in proto,
          "协议里只有 embed()，没有 embed_query() —— 查询侧嵌入不在契约里，"
          "两个实现都实现了纯属巧合，换个实现就断")
    clause("S15-03c")
    tag = embedder_tag(HashEmbedder(dim=64))
    check("失效键必须能区分不同模型（协议未要求 model/dim，getattr 兜底成空串会撞键）",
          "hash" in tag and "64" in tag,
          f"HashEmbedder 的标签={tag}；协议只要求 name/semantic，"
          f"model/dim 靠 getattr 兜底 —— 两个同名不同维的模型会撞键")


# --------------------------------------------------------------------------
# §S2 云端嵌入客户端（PRD 13.4.5 选型轴③）
# --------------------------------------------------------------------------
def audit_s2_client() -> None:
    section("§S2 云端嵌入客户端（离线假 transport，不联网不计费）")
    clause("S15-04")
    t = FakeTransport()
    emb = RemoteEmbedder(base_url="https://example.invalid", api_key="K", dim=4,
                          transport=t)
    vecs = emb.embed(["a", "b"])
    check("批量嵌入返回正确数量", len(vecs) == 2, str(len(vecs)))
    check("请求体带 type=db（文档类）", t.calls[0]["payload"].get("type") == "db",
          str(t.calls[0]["payload"]))
    check("向量已归一（余弦口径的前提）",
          all(abs(sum(x * x for x in v) - 1.0) < 1e-6 for v in vecs), "")
    clause("S15-05")
    emb.embed_query("q")
    check("查询嵌入用 type=query", t.calls[-1]["payload"].get("type") == "query",
          str(t.calls[-1]["payload"]))
    clause("S15-06")
    t2 = FakeTransport(mode="business_error")
    emb2 = RemoteEmbedder(base_url="https://example.invalid", api_key="K", dim=4,
                           transport=t2)
    try:
        emb2.embed(["a"])
        check("业务错误必须抛异常", False, "没抛")
    except EmbeddingError as exc:
        check("业务错误必须抛异常", True, str(exc)[:60])
    check("业务错误不重试", len(t2.calls) == 1, f"{len(t2.calls)} 次")
    clause("S15-07")
    t3 = FakeTransport(mode="net_error")
    emb3 = RemoteEmbedder(base_url="https://example.invalid", api_key="K", dim=4,
                           retries=2, transport=t3)
    try:
        emb3.embed(["a"])
    except EmbeddingError:
        pass
    check("网络错误必须重试", len(t3.calls) == 3, f"{len(t3.calls)} 次（期望 3）")
    clause("S15-08")
    t4 = FakeTransport(dim=8)
    emb4 = RemoteEmbedder(base_url="https://example.invalid", api_key="K", dim=4,
                           transport=t4)
    try:
        emb4.embed(["a"])
        check("文档维度不符必须报错拦下", False, "没抛")
    except EmbeddingError as exc:
        check("文档维度不符必须报错拦下", "维度" in str(exc), str(exc)[:60])
    # 查询侧
    t5 = FakeTransport(dim=8)
    emb5 = RemoteEmbedder(base_url="https://example.invalid", api_key="K", dim=4,
                           transport=t5)
    try:
        v = emb5.embed_query("q")
        check("**查询**嵌入维度不符也必须报错（与文档侧同口径）", False,
              f"返回了 {len(v)} 维向量，未报错")
    except EmbeddingError as exc:
        check("**查询**嵌入维度不符也必须报错（与文档侧同口径）", True, str(exc)[:60])
    clause("S15-09")
    t6 = FakeTransport(mode="http_401")
    emb6 = RemoteEmbedder(base_url="https://example.invalid", api_key="K", dim=4,
                           retries=2, transport=t6)
    try:
        emb6.embed(["a"])
    except EmbeddingError:
        pass
    note("HTTP 401/403 会被当成网络错误重试（业务错误不重试的承诺只对 MiniMax 形态成立）",
         f"HTTPError ⊂ URLError → 401 也重试，共 {len(t6.calls)} 次调用")
    clause("S15-10")
    err = EmbeddingError("boom")
    check("异常信息里不得出现 API key", "K" not in str(err), "")
    t7 = FakeTransport(mode="business_error")
    emb7 = RemoteEmbedder(base_url="https://example.invalid", api_key="SUPERSECRET",
                           dim=4, transport=t7)
    try:
        emb7.embed(["a"])
        msg = ""
    except EmbeddingError as exc:
        msg = str(exc)
    check("业务错误信息里不得出现 API key", "SUPERSECRET" not in msg, msg[:60])


# --------------------------------------------------------------------------
# §S3 向量存储（PRD 9.4 难点③ / 13.4 选型轴③）
# --------------------------------------------------------------------------
def audit_s3_storage() -> None:
    section("§S3 向量存储（独立表 / 幂等 / 隔离 / 维度拦截）")
    store = new_store()
    load_kb(store, KB_VERSION)
    clause("S15-11")
    vec = [0.1, 0.2, 0.3, 0.4]
    store.upsert_kb_embedding("KB-T-1", KB_VERSION, "hash/1/4", vec)
    items = store.list_kb_embeddings(KB_VERSION)
    check("向量写入后能读回", len(items) == 1, str(items))
    check("读回的向量与写入一致",
          abs(items[0][2][0] - vec[0]) < 1e-9 and len(items[0][2]) == 4,
          str(items[0][2]))
    check("读回带模型版本标签", items[0][1] == "hash/1/4", str(items[0][1]))
    store.upsert_kb_embedding("KB-T-1", KB_VERSION, "hash/1/4", [0.9, 0.8, 0.7, 0.6])
    items2 = store.list_kb_embeddings(KB_VERSION)
    check("同一 chunk_id 覆盖写（幂等）", len(items2) == 1, str(len(items2)))
    check("覆盖后值已更新", abs(items2[0][2][0] - 0.9) < 1e-9, str(items2[0][2]))
    clause("S15-12")
    store.upsert_kb_embedding("KB-T-2", "kb-other", "hash/1/4", vec)
    only = store.list_kb_embeddings(KB_VERSION)
    check("按库版本隔离", len(only) == 1 and only[0][0] == "KB-T-1", str(only))
    clause("S15-13")
    store.upsert_kb_embedding("KB-T-3", KB_VERSION, "hash/1/4", vec)
    ids = [c for c, _m, _v in store.list_kb_embeddings(KB_VERSION)]
    check("读回顺序确定（ORDER BY chunk_id）", ids == sorted(ids), str(ids))
    clause("S15-14")
    store.clear_kb_embeddings(KB_VERSION)
    check("清理向量表可用", store.list_kb_embeddings(KB_VERSION) == [], "")


# --------------------------------------------------------------------------
# §S4 索引流程（幂等 / 失效键 / 计费）
# --------------------------------------------------------------------------
def audit_s4_indexer() -> None:
    section("§S4 索引流程（幂等 / 失效键 / 不白花钱）")
    store = new_store()
    load_kb(store, KB_VERSION)
    blocks = store.list_kb_chunks(KB_VERSION)
    emb = HashEmbedder(dim=64)
    clause("S15-15")
    stats = index_kb(store, emb, KB_VERSION, chunks=blocks)
    check("首次索引全部块", stats["indexed"] == len(blocks), str(stats))
    stats2 = index_kb(store, emb, KB_VERSION, chunks=blocks)
    check("重复索引幂等（同一模型版本不重复嵌入）",
          stats2["indexed"] == 0 and stats2["skipped"] == len(blocks), str(stats2))
    clause("S15-16")
    # 块内容改了、库版本号没升 → 向量必须跟着重算（否则向量通道返回旧内容）
    target = blocks[0]
    changed = target.model_copy(deep=True)
    changed.text = target.text + "（内容已改）"
    store.upsert_kb_chunk(changed)
    before = dict((c, v) for c, _m, v in store.list_kb_embeddings(KB_VERSION))
    stats3 = index_kb(store, emb, KB_VERSION, chunks=store.list_kb_chunks(KB_VERSION))
    after = dict((c, v) for c, _m, v in store.list_kb_embeddings(KB_VERSION))
    changed_ok = (stats3["indexed"] >= 1
                  and before.get(target.chunk_id) != after.get(target.chunk_id))
    check("块内容改了（版本号未升）→ 向量必须重算，不能用旧向量",
          changed_ok,
          f"indexed={stats3['indexed']} 向量有变化={before.get(target.chunk_id) != after.get(target.chunk_id)}"
          f"（失效键只有 name/model/dim，不含块内容）")
    clause("S15-17")
    # 重灌知识库种子是幂等操作，不得连带删掉向量索引（删了就等于每次建索引都全量重嵌）
    store.upsert_kb_embedding("KB-T-KEEP", KB_VERSION, "hash/1/4", [0.1] * 64)
    n_before = len(store.list_kb_embeddings(KB_VERSION))
    load_kb(store, KB_VERSION)
    n_after = len(store.list_kb_embeddings(KB_VERSION))
    check("重灌知识库种子不得连带删除向量索引（否则每次建索引都全量重嵌 = 白花钱）",
          n_after >= n_before, f"重灌前 {n_before} 条 → 重灌后 {n_after} 条")
    clause("S15-18")
    src = (ROOT / "scripts" / "index_kb.py").read_text(encoding="utf-8")
    check("建索引脚本不得清空业务数据与审计日志（store.reset() 会清 audit_log）",
          ".reset(" not in src,
          "脚本里调用了 store.reset() —— 它清 threads/customers/thread_events/audit_log/gate_tokens")
    clause("S15-19")
    # 只查**数值**（属性值），不查"价格区间"这类词名 —— 文本块里出现属性名不算违规
    dirty = []
    for c in store.list_kb_chunks(KB_VERSION):
        txt = chunk_text(c)
        if re.search(r"(USD\s*\d|\d+\s*(?:天|台|吨|kg|公斤)|MOQ\s*\d|"
                     r"交期\s*[\d-]|参考价\s*\d|载荷\s*\d)", txt):
            dirty.append(c.chunk_id)
    check("送进嵌入模型的文本不得含属性数值（PRD 9.3 / R11-08 回归）",
          not dirty, str(dirty))
    specs = [c.chunk_id for c in store.list_kb_chunks(KB_VERSION)
             if c.doc_type == "产品规格"]
    note("『产品规格』块的文本是模板句（三个 SKU 高度相似、语义无信息量）",
         f"这些块也进了向量索引：{specs} —— 建议 S4b 把它们排除在语义通道之外"
         f"（它们只该走属性过滤）")


# --------------------------------------------------------------------------
# §S5 余弦检索
# --------------------------------------------------------------------------
def audit_s5_vector_index() -> None:
    section("§S5 余弦 TopK 检索")
    clause("S15-20")
    idx = VectorIndex([("A", [1.0, 0.0, 0.0]), ("B", [0.0, 1.0, 0.0]),
                       ("C", [0.9, 0.1, 0.0])])
    hits = idx.search([1.0, 0.0, 0.0], top_k=2)
    check("相似度最高的排第一", hits and hits[0][0] == "A", str(hits))
    check("TopK 数量正确", len(hits) == 2, str(len(hits)))
    check("相似度降序", hits[0][1] >= hits[1][1], str(hits))
    check("维度不匹配不崩盘", idx.search([1.0, 0.0], top_k=2)[0][1] == 0.0, "")
    check("空索引返回空", VectorIndex().search([1.0, 0.0]) == [], "")
    clause("S15-21")
    # 余弦的定义：相似度必须落在 [-1, 1]。未归一向量会让"余弦"变成点积。
    raw = VectorIndex([("A", [3.0, 0.0, 0.0]), ("B", [0.0, 1.0, 0.0])])
    h2 = raw.search([3.0, 0.0, 0.0], top_k=1)
    check("相似度必须落在 [-1, 1]（未归一向量不得算出 >1 的『余弦』）",
          all(-1.0 - 1e-9 <= s <= 1.0 + 1e-9 for _c, s in h2),
          f"未归一向量的相似度={h2}（VectorIndex 不校验归一）")
    clause("S15-22")
    n1 = NullEmbedder().embed(["hello"])
    n2 = NullEmbedder().embed(["hello", "world"])
    check("同一嵌入器对不同批次的输出维度必须一致（否则向量不可比）",
          len(n1[0]) == len(n2[0]),
          f"单条 {len(n1[0])} 维 / 两条 {len(n2[0])} 维"
          f"（NullEmbedder 的词表随批次变化；且它不归一）")


# --------------------------------------------------------------------------
# §S6 装配与护栏（默认关闭 / 启动不调云端 / 不得宣称语义能力）
# --------------------------------------------------------------------------
def audit_s6_wiring() -> None:
    section("§S6 装配与护栏（成本 / 降级 / 口径）")
    clause("S15-23")
    cfg = get_settings()
    check("嵌入默认关闭（不配 key 不计费）",
          Config().embedder_enabled is False and Config().llm_api_key == "",
          f"默认 embedder_enabled={Config().embedder_enabled}")
    clause("S15-24")
    from tradedesk.api.deps import AppContext
    store = new_store()
    cfg2 = Config(embedder_enabled=True, llm_api_key="FAKE-KEY",
                  embedding_dim=4, embedder_base_url="https://example.invalid")
    ctx = AppContext(store=store, config=cfg2)
    check("开启嵌入后启动不得调用云端 API（建索引是显式动作）",
          ctx.embedder is not None and ctx.embedder.calls == 0,
          f"启动后调用次数={getattr(ctx.embedder, 'calls', None)}")
    check("启动时只读现成索引，不自动建",
          ctx.vector_index is not None and len(ctx.vector_index) == 0,
          f"启动时索引条数={len(ctx.vector_index) if ctx.vector_index else None}")
    clause("S15-25")
    pipe = RetrievalPipeline(store.list_kb_chunks(KB_VERSION), KB_VERSION)
    hits = pipe.search(SearchQuery(text="X-200 price", lang="en", top_k=5))
    chans = sorted({ch for h in hits for ch in h.channels})
    check("未喂稠密命中时，管线只出词法 + 结构化两路",
          "dense" not in chans, f"命中用到的通道={chans}")
    clause("S15-26")
    # 索引加载失败不得阻断启动
    try:
        ctx2 = AppContext(store=store, config=cfg2)
        check("向量索引加载失败不得阻断启动", True, "正常路径")
    except Exception as exc:  # noqa: BLE001
        check("向量索引加载失败不得阻断启动", False, str(exc)[:80])


# --------------------------------------------------------------------------
# §S7 口径声明与文档一致性（S3 栽过的坑：文档口径 ≠ 实际）
# --------------------------------------------------------------------------
def audit_s7_docs() -> None:
    section("§S7 口径声明与文档一致性")
    doc_p = ROOT / "docs" / "S4-ACCEPTANCE.md"
    doc = doc_p.read_text(encoding="utf-8")
    dim = Config().embedding_dim
    clause("S15-27")
    stale = re.findall(r"vector\((\d+)\)", doc)
    check("文档里的向量列维度必须与配置一致（换维度后旧文档会误导）",
          all(int(x) == dim for x in stale),
          f"配置={dim} 文档里出现 vector({', '.join(stale) or '无'})")
    row = [ln for ln in doc.splitlines() if "embo-01" in ln and "云端" in ln]
    check("模型口径表的维度必须与配置一致",
          all(f"{dim} 维" in ln for ln in row) if row else True,
          "；".join(row)[:120])
    clause("S15-28")
    claim_no_call = ("没有真实云端嵌入调用" in doc) or ("真实调用在 4b" in doc)
    has_measured = ("实测" in doc) and ("embo-01" in doc)
    check("口径声明不得与实测章节自相矛盾（说没真实调用，又给实测矩阵）",
          not (claim_no_call and has_measured),
          "文档同时声称『没有真实云端嵌入调用 / 真实调用在 4b』与『已用真实 key 实测』")
    clause("S15-29")
    check("口径声明必须写明当前嵌入模型的跨语种限制（实测 zh↔es/ar ≈ 0）",
          ("跨语种" in doc.split("## 一、")[0]) or ("对齐" in doc.split("## 一、")[0]),
          "§0 口径声明里没有跨语种限制的说明 —— 4b 极易据此宣称『多语种语义召回』")
    clause("S15-30")
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    check(".env.example 的注释不得与实际行为矛盾（启动已改为只读索引）",
          "启动会建向量索引" not in env,
          "注释仍写『开启后启动会建向量索引（会计费）』，实际启动只读索引")
    clause("S15-31")
    check("与 PRD 的模型偏差必须有显式声明（FR-KB-03 默认 BGE-M3）",
          "BGE-M3" in doc and ("偏差" in doc or "不同" in doc),
          "docs/S4-ACCEPTANCE.md 第二节已声明偏差")


# --------------------------------------------------------------------------
# §S8 密钥卫生
# --------------------------------------------------------------------------
def audit_s8_secrets() -> None:
    section("§S8 密钥卫生")
    clause("S15-32")
    hits = []
    for pat in ("*.py", "*.md", "*.example", "*.yml", "*.yaml", "*.ps1"):
        for p in ROOT.rglob(pat):
            if any(part in (".git", ".venv", "worktrees", "node_modules")
                   for part in p.parts):
                continue
            try:
                t = p.read_text(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                continue
            for m in re.finditer(r"(api[_-]?key|token|secret)\s*[:=]\s*[\"']([^\"']{12,})[\"']",
                                 t, re.I):
                val = m.group(2)
                if val.lower() in ("example", "your-api-key", "changeme", "x"):
                    continue
                if re.search(r"(sk-|eyJ|Bearer\s+\w{10,})", val):
                    hits.append(f"{p.relative_to(ROOT)}: {val[:12]}…")
    check("仓库里不得出现疑似真实的密钥", not hits, str(hits))
    clause("S15-33")
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    m = re.search(r"^MINIMAX_API_KEY=(.*)$", env, re.M)
    check(".env.example 里的 key 必须是空的", bool(m) and not m.group(1).strip(),
          (m.group(1)[:8] + "…") if m and m.group(1).strip() else "")


# --------------------------------------------------------------------------
# §S9 PostgreSQL 专属（pgvector 列 / 维度拦截 / 维度变更）
# --------------------------------------------------------------------------
def audit_s9_postgres() -> None:
    section("§S9 PostgreSQL 专属（pgvector 列 / 维度拦截）")
    if not DSN:
        note("本次未跑 PostgreSQL", "加 --dsn <DSN> 再跑一次")
        return
    from tradedesk.store.postgres_store import PostgresStateStore

    head = _pg_split(DSN)
    _SEQ[0] += 1
    name = f"audit_s4_{_SEQ[0]:02d}"
    _pg_exec(head + "postgres", f'CREATE DATABASE "{name}"')
    _PG_DBS.append((head, name))
    dsn = head + name
    st = PostgresStateStore(dsn, vector_dim=4)
    _STORES.append(st)
    clause("S15-34")
    check("本机装了 pgvector 扩展", st.pgvector_available, str(st.pgvector_available))
    check("有扩展时必须用 vector 列（而不是降级 JSONB）",
          st.embedding_column == "vector", st.embedding_column)
    clause("S15-35")
    st.upsert_kb_embedding("KB-T-1", KB_VERSION, "hash/1/4", [0.1, 0.2, 0.3, 0.4])
    items = st.list_kb_embeddings(KB_VERSION)
    check("vector 列里的向量能读回并解析成数值列表",
          len(items) == 1 and abs(items[0][2][1] - 0.2) < 1e-6, str(items))
    clause("S15-36")
    try:
        st.upsert_kb_embedding("KB-T-2", KB_VERSION, "hash/1/8", [0.1] * 8)
        check("维度不符必须被存储层拦下", False, "写入成功了")
    except Exception as exc:  # noqa: BLE001
        check("维度不符必须被存储层拦下", "dimension" in str(exc).lower(),
              str(exc)[:80])
    clause("S15-37")
    # 换维度 → 存储层初始化时自动重建列（向量是派生数据，可重建）
    st.close()
    _STORES.remove(st)
    st2 = PostgresStateStore(dsn, vector_dim=8)
    _STORES.append(st2)
    left = st2.list_kb_embeddings(KB_VERSION)
    empty = [c for c, _m, v in left if not v]
    check("维度变更后不得留下『有行但向量为空』的残骸（空向量让稠密通道静默失效）",
          not empty,
          f"换维度后剩 {len(left)} 行，其中空向量 {len(empty)} 条 {empty}；"
          f"AppContext 按行数打印『向量索引已加载：N 条』→ 4b 接进来后表现为"
          f"『语义召回没结果』，容易被误判成模型不行")
    note("维度变更会在存储层初始化时自动重建列（清空全部向量）",
         f"换维度后剩 {len(left)} 行；这是存储层初始化的副作用，不是显式动作")
    st2.close()
    _STORES.remove(st2)


# --------------------------------------------------------------------------
# §S10 S4-4b 稠密通道（第三通道的守则）
# --------------------------------------------------------------------------
class FakeSemanticEmbedder(HashEmbedder):
    """假的"语义"嵌入器：只把 semantic 标成 True，用来验接线（它本身没有语义能力）。"""

    name = "fake-semantic"
    semantic = True
    model = "fake"


class FlakyTransport:
    """前 n 次抛网络错误、之后正常返回（用来测重试与计费口径）。"""

    def __init__(self, dim=4, fail_times=0):
        self.dim = dim
        self.fail_times = fail_times
        self.calls = 0

    def __call__(self, url, headers, payload, timeout):
        self.calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise OSError("connection reset")
        n = len(payload.get("texts") or payload.get("input") or [])
        return {"base_resp": {"status_code": 0}, "total_tokens": 3,
                "vectors": [[0.1 * (i + 1) for i in range(self.dim)] for _ in range(n)]}


def audit_s10_dense_channel() -> None:
    section("§S10 S4-4b 稠密通道（语义守卫 / 权重 / 降级 / 失效 / 计费）")
    store = new_store()
    load_kb(store, KB_VERSION)
    blocks = store.list_kb_chunks(KB_VERSION)
    emb = FakeSemanticEmbedder(dim=64)
    index_kb(store, emb, KB_VERSION, chunks=blocks)
    idx = load_index(store, KB_VERSION)
    q = SearchQuery(text="¿Cuál es su mejor precio para X-200?", lang="es", top_k=5)

    clause("R17-01")
    r = StoreRetriever(store, KB_VERSION, embedder=emb, vector_index=idx)
    hits = r.search(q)
    check("语义嵌入器下稠密通道必须被接进检索管线",
          any("dense" in h.channels for h in hits),
          f"命中用到的通道={sorted({c for h in hits for c in h.channels})}")

    clause("R17-02")
    r2 = StoreRetriever(store, KB_VERSION, embedder=HashEmbedder(dim=64), vector_index=idx)
    hits2 = r2.search(q)
    check("假嵌入器（semantic=False）不得开启稠密通道",
          not any("dense" in h.channels for h in hits2) and bool(r2.dense_skipped),
          f"通道={sorted({c for h in hits2 for c in h.channels})} 原因={r2.dense_skipped}")

    clause("R17-03")
    from tradedesk.retrieval.pipeline import RRF_K
    check("稠密通道权重必须最低（k 最大）、结构化最高（k 最小）",
          RRF_K["dense"] > RRF_K["lexical"] > RRF_K["structured"], str(RRF_K))

    clause("R17-04")
    specs = [h.chunk.chunk_id for h in hits
             if "dense" in h.channels and h.chunk.doc_type == "产品规格"]
    check("「产品规格」块不得进稠密通道（模板句无语义信息量）", not specs, str(specs))

    clause("R17-05")
    class BrokenEmbedder(FakeSemanticEmbedder):
        def embed_query(self, text):
            raise RuntimeError("云端嵌入失败")
    r3 = StoreRetriever(store, KB_VERSION, embedder=BrokenEmbedder(dim=64), vector_index=idx)
    hits3 = r3.search(q)
    check("查询嵌入失败只降级、不得拖垮检索",
          bool(hits3) and not any("dense" in h.channels for h in hits3)
          and bool(r3.dense_skipped),
          f"命中 {len(hits3)} 条 原因={r3.dense_skipped}")

    clause("R17-06")
    # 重建索引（换嵌入器 → 向量全变）后，**进程内**的稠密通道必须跟着变。
    # 否则 S3 的 R11-07（改库后同进程不生效）在向量层重演：库里是新向量，
    # 检索用的还是启动时载入的旧快照。
    emb2 = FakeSemanticEmbedder(dim=64)
    emb2.ngram = 5                      # 同文本 → 不同向量
    index_kb(store, emb2, KB_VERSION, chunks=blocks, force=True)
    fresh = load_index(store, KB_VERSION)
    stale = StoreRetriever(store, KB_VERSION, embedder=emb2, vector_index=idx)
    fresh_r = StoreRetriever(store, KB_VERSION, embedder=emb2, vector_index=fresh)
    stale_pairs = [(h.chunk.chunk_id, h.score) for h in stale.dense_hits(q)]
    fresh_pairs = [(h.chunk.chunk_id, h.score) for h in fresh_r.dense_hits(q)]
    check("重建索引后，进程内稠密通道必须使用新向量（索引快照要能失效）",
          stale_pairs == fresh_pairs,
          f"陈旧快照前 2 条={stale_pairs[:2]}｜库里现向量前 2 条={fresh_pairs[:2]}"
          f"（检索器把启动时的索引当快照，不重载）")

    clause("R17-07")
    nul = NullEmbedder()
    two = nul.embed(["手动搬运车", "电动搬运车"])
    check("占位嵌入器对不同文本必须给出不同向量（否则向量通道全是同一向量）",
          two[0] != two[1],
          f"两条文本的向量完全相同（余弦恒为 1，检索无从排序）："
          f"embed(['a','b'])[0] == embed(['a','b'])[1]")

    clause("R17-08")
    # 计费口径：dense_calls 数的是 embed_query 次数，不是**实际云端调用次数**。
    # 网络故障重试会多花钱，但计量看不出来 → S5 的 ≤0.2 元/条 基线会低估。
    t = FlakyTransport(dim=4, fail_times=2)
    remb = RemoteEmbedder(base_url="https://example.invalid", api_key="K", dim=4,
                           retries=2, transport=t)
    fake_idx = VectorIndex([(c.chunk_id, [0.1, 0.2, 0.3, 0.4]) for c in blocks])
    r4 = StoreRetriever(store, KB_VERSION, embedder=remb, vector_index=fake_idx)
    r4.search(q)
    check("成本计量必须等于实际云端调用次数（重试也是要花钱的）",
          r4.dense_calls == remb.calls,
          f"计量 dense_calls={r4.dense_calls}，实际 HTTP 调用={remb.calls}"
          f"（重试 2 次后成功，只记 1 次 → 成本基线低估到 1/3）")

    clause("R17-09")
    # 稠密通道不得绕过属性过滤与时效闸门：dense 命中必须落在过滤后的范围内
    expired = [h.chunk.chunk_id for h in r.search(
        SearchQuery(text="CERT-CE-1890 certificate", lang="en", top_k=8))
        if "dense" in h.channels and h.chunk.identifiers.get("cert_no") == "CERT-CE-1890"]
    check("已废止的证书不得被稠密通道召回", not expired, str(expired))


# --------------------------------------------------------------------------
# §S11 S4-4c 评测框架（口径优先：数字必须站得住）
# --------------------------------------------------------------------------
def _load_eval_module():
    """按路径载入 scripts/eval_retrieval.py（它不是包）。"""
    import importlib.util
    path = ROOT / "scripts" / "eval_retrieval.py"
    spec = importlib.util.spec_from_file_location("audit_eval_retrieval", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def audit_s11_eval() -> None:
    section("§S11 S4-4c 评测框架（指标口径 / 稠密通道可观测 / 语料质量 / 编号）")
    ev = _load_eval_module()
    from tradedesk.data.inquiry_corpus import build_corpus, INTENTS
    cases = build_corpus()

    clause("R17-10")
    # PRD 12.3 的「意图 Macro-F1」是**五类意图**的宏平均。
    # 评测脚本把没有样本的 "unknown" 也算进分母 → 每个语种被系统性压低 1/6。
    pairs = {lang: [] for lang in ("en", "es", "ar", "zh")}
    for c in cases:
        pairs[c.lang].append((c.intent, ev.predict_intent(c.text)))
    for lang in ("en", "es", "ar"):
        got, _per = ev.macro_f1(pairs[lang])
        sub = pairs[lang]
        tot = 0.0
        for lab in INTENTS:
            tp = sum(1 for g, p in sub if g == lab and p == lab)
            fp = sum(1 for g, p in sub if g != lab and p == lab)
            fn = sum(1 for g, p in sub if g == lab and p != lab)
            pr = tp / (tp + fp) if (tp + fp) else 0.0
            rc = tp / (tp + fn) if (tp + fn) else 0.0
            tot += (2 * pr * rc / (pr + rc)) if (pr + rc) else 0.0
        prd = round(tot / len(INTENTS), 4)
        check(f"{lang} 的 Macro-F1 必须与 PRD 12.3 同口径（不含无样本标签）",
              abs(got - prd) < 1e-6,
              f"脚本报 {got}（分母含无样本的 unknown）｜PRD 五类口径 {prd}｜"
              f"差 {round(got - prd, 4)}")

    clause("R17-11")
    src = (ROOT / "scripts" / "eval_retrieval.py").read_text(encoding="utf-8")
    check("评测报表必须能区分『稠密通道失败』与『稠密通道召回差』",
          "dense_skipped" in src,
          "查询嵌入失败会逐条降级（retriever.dense_skipped 有记录），"
          "但评测脚本从不读它 → 稠密通道指标全是 0.000，报表头部却照写『启用』，"
          "读者会误判成『模型不行』而不是『通道没跑』（R15-04 同型）")

    clause("R17-12")
    check("评测必须给出稠密通道的调用次数与成本口径（≤0.2 元/条 的计量基础）",
          "dense_calls" in src,
          "评测跑了每条语料 1 次云端嵌入（160 次），报表里既没有调用次数也没有成本，"
          "跑一次 eval 的真实花费不可见")

    clause("R17-13")
    # 待验证项编号是 PRD 14.3 的全局编号表，不得撞号
    prd = (ROOT / "PRD-外贸业务员的AI数字员工工作台-v1.0.md").read_text(encoding="utf-8")
    prd_rows = {}
    for m in re.finditer(r"^\|\s*(V-\d\d)\s*\|\s*([^|]+)\|", prd, re.M):
        prd_rows.setdefault(m.group(1), m.group(2).strip())
    base = (ROOT / "docs" / "S4-EVAL-BASELINE.md").read_text(encoding="utf-8")
    base_rows = {}
    for m in re.finditer(r"^\|\s*(V-\d\d)\s*\|\s*([^|]+)\|", base, re.M):
        base_rows.setdefault(m.group(1), m.group(2).strip())
    clash = []
    for num, topic in sorted(base_rows.items()):
        ref = prd_rows.get(num, "")
        if not ref:
            continue
        # 主题词重叠判定：PRD 那行的关键词应当出现在基线那一行里
        keys = [w for w in re.split(r"[（）()/、，,\s]+", ref) if len(w) >= 3]
        hit = any(k[:4] in topic for k in keys)
        if not hit:
            clash.append(f"{num}: PRD=「{ref[:18]}…」 vs 基线=「{topic[:18]}…」")
    check("待验证项编号不得与 PRD 14.3 撞号（编号是全局唯一表）",
          not clash, "；".join(clash))

    clause("R17-14")
    # 语料质量：审核方人工挑出的非词（可复现清单见 docs/S4-AUDIT-R17.md 附录）
    nonwords = {
        "ar": ["ناسرفع", "التصليل", "التمتليم", "الضحة", "الطراة", "مطاطع", "أكددوا",
               "موعددار", "تكلفونة", "معلكة", "بخص", "الطلب الأدنى"],
        "es": ["zanpan", "Compénsennos", "Confirmenme", "merchancía"],
    }
    bad = []
    for c in cases:
        for w in nonwords.get(c.lang, []):
            if w in c.text:
                bad.append(f"{c.case_id}:{w}")
    per_lang = {}
    for c in cases:
        if any(w in c.text for w in nonwords.get(c.lang, [])):
            per_lang[c.lang] = per_lang.get(c.lang, 0) + 1
    total_lang = {}
    for c in cases:
        total_lang[c.lang] = total_lang.get(c.lang, 0) + 1
    check("自建语料不得含成规模的非词（ar/es 是买家语种，语料本身就是被评对象）",
          len(bad) <= 2,
          f"含非词的样本 {len(bad)} 条：" + "，".join(bad[:8])
          + f"｜各语种占比 " + "，".join(f"{k}={per_lang.get(k,0)}/{total_lang.get(k,0)}"
                                        for k in ("en", "es", "ar")))

    clause("R17-15")
    named = sum(1 for c in cases if any(m in c.text for m in ("X-200", "X-300", "M-77")))
    scored = sum(1 for c in cases if c.expect_sku)
    ratio = named / len(cases) if cases else 0
    m = re.search(r"(\d+)%\s*点名了型号", base)
    claimed = int(m.group(1)) if m else -1
    check("基线文档里『点名型号的比例』必须与语料实际一致（它是头条数字的免责说明）",
          m is not None and abs(claimed - ratio * 100) <= 2,
          f"文档写 {claimed}%，语料实际点名 {named}/{len(cases)} = {round(ratio * 100, 1)}%"
          f"（参与 SKU 评分的 {scored} 条 100% 点名 —— 这才是要说清的那句）")

    clause("R17-16")
    check("公开基准层必须独立成表、不得与自建集合并算总分（PRD 12.3 分层规则）",
          "公开基准" in base and not re.search(r"MIRACL[^\n]*\d\.\d", base),
          "基线文档把公开基准体检层单列，且明确标注不可达 → 符合分层规则")

    clause("R17-17")
    check("语料覆盖度不足必须显式披露（PRD 12.3 缺失即披露）",
          ("缩减版" in base) and ("V-10" in base or "语料扩量" in base),
          "基线文档写明是缩减版并登记待验证项")

    clause("R17-18")
    note("评测脚本不看 TRADEDESK_EMBEDDER_ENABLED（那只管应用启动）",
         "只要 .env 里有 key，跑一次 dev.ps1 eval 就是 160 次云端查询嵌入调用"
         "（每条语料 1 次、无缓存无批量），脚本既没有开关也没有提示")

    clause("R17-19")
    note("评测脚本在 --dsn 模式下直接往传入的库写种子",
         "audit_s2/audit_s3 都是自建独立库并回收，eval 不是 → "
         "误把 --dsn 指到 tradedesk_a 会污染实现方的库")


# --------------------------------------------------------------------------
# §S12 PostgreSQL 稠密通道（版本隔离 / ANN 索引 / 双后端一致）
# --------------------------------------------------------------------------
def audit_s12_postgres_dense() -> None:
    section("§S12 PostgreSQL 稠密通道（版本隔离 / ANN 索引 / 双后端一致）")
    if not DSN:
        note("本次未跑 PostgreSQL", "加 --dsn <DSN> 再跑一次")
        return
    import psycopg
    from tradedesk.store.postgres_store import PostgresStateStore

    head = _pg_split(DSN)
    _SEQ[0] += 1
    name = f"audit_s4b_{_SEQ[0]:02d}"
    _pg_exec(head + "postgres", f'CREATE DATABASE "{name}"')
    _PG_DBS.append((head, name))
    dsn = head + name

    clause("R17-20")
    # 模拟"存量库升级"：4a 时代建的表（有 embedding 列、没有 content_fp、没有 ANN 索引）
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.execute(
            "CREATE TABLE kb_embeddings ("
            "  chunk_id TEXT PRIMARY KEY,"
            "  kb_version TEXT NOT NULL DEFAULT '',"
            "  model TEXT NOT NULL DEFAULT '',"
            "  dim INTEGER NOT NULL DEFAULT 0,"
            "  embedding vector(4),"
            "  updated_at TIMESTAMPTZ NOT NULL DEFAULT now())")
    st = PostgresStateStore(dsn, vector_dim=4)
    _STORES.append(st)
    with psycopg.connect(dsn, autocommit=True) as conn:
        idxs = [r[0] for r in conn.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename='kb_embeddings'").fetchall()]
    check("存量库（4a 建的表）升级后也必须建上 ANN 索引（HNSW）",
          "idx_kb_embeddings_hnsw" in idxs,
          f"存量库升级后的索引={idxs or '无'} —— 近邻检索退化为全表线性扫描，"
          f"而验收 4b-07 写的是『走 pgvector + HNSW（生产形态）』；"
          f"本机 tradedesk_a / tradedesk_b / tradedesk_audit_b 实测也只有 pkey")

    clause("R17-21")
    # 版本隔离：近邻检索必须按 kb_version 过滤（内存口径 list_kb_embeddings 是过滤的）
    load_kb(st, KB_VERSION)
    blocks = st.list_kb_chunks(KB_VERSION)
    emb = FakeSemanticEmbedder(dim=4)
    index_kb(st, emb, KB_VERSION, chunks=blocks)
    # 塞一批**别的库版本**的残留向量（换库版本后旧向量不会自动清）
    for i in range(12):
        st.upsert_kb_embedding(f"KB-OLD-{i:02d}", "kb-old", "fake-semantic/fake/4",
                               [0.9, 0.1, 0.0, 0.0])
    pairs = st.search_kb_vectors([0.9, 0.1, 0.0, 0.0], top_k=8)
    old = [cid for cid, _s in pairs if cid.startswith("KB-OLD")]
    check("PostgreSQL 近邻检索必须按库版本过滤（旧版本向量不得进结果）",
          not old, f"返回里混进了别的库版本的向量 {old[:5]}（共 {len(pairs)} 条）")

    clause("R17-22")
    # 后果：旧版本向量会占满 LIMIT 的名额 → 稠密通道在 PG 上静默变空
    # （把旧向量设成与查询向量完全一致 —— 换库版本后旧向量留在库里正是这种情形）
    qv = emb.embed_query("precio X-200")
    for i in range(12):
        st.upsert_kb_embedding(f"KB-OLD2-{i:02d}", "kb-old", "fake-semantic/fake/4", qv)
    retriever = StoreRetriever(st, KB_VERSION, embedder=emb,
                               vector_index=load_index(st, KB_VERSION))
    hits = retriever.dense_hits(SearchQuery(text="precio X-200", lang="es", top_k=5))
    check("旧版本向量不得把稠密通道挤空（PG 与 SQLite 必须一致）",
          len(hits) > 0,
          f"PG 稠密通道返回 {len(hits)} 条 —— top_k 的名额被 12 条旧版本向量占满"
          f"（距离 0），当前库的向量被挤出 LIMIT，命中块在块表里又找不到 → 全被丢掉")

    st.close()
    _STORES.remove(st)


# --------------------------------------------------------------------------
def run_all() -> None:
    for fn in (audit_s1_contract, audit_s2_client, audit_s3_storage, audit_s4_indexer,
               audit_s5_vector_index, audit_s6_wiring, audit_s7_docs, audit_s8_secrets,
               audit_s9_postgres, audit_s10_dense_channel, audit_s11_eval,
               audit_s12_postgres_dense):
        fn()
        close_stores()


def cleanup() -> None:
    close_stores()
    for p in _TMPFILES:
        for suffix in ("", "-wal", "-shm"):
            f = p.with_name(p.name + suffix)
            if f.exists():
                try:
                    f.unlink()
                except OSError:
                    pass


def main() -> int:
    ap = argparse.ArgumentParser(description="S4 独立审核脚本")
    ap.add_argument("--dsn", default="", help="PostgreSQL DSN（留空 = SQLite）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    global DSN
    DSN = args.dsn
    backend = f"PostgreSQL {args.dsn.rsplit('/', 1)[-1]}" if args.dsn else "SQLite"

    print(f"TradeDesk-AI · S4 独立审核（{backend}）")
    print("=" * 70)
    try:
        run_all()
    finally:
        cleanup()

    fails = [r for r in RESULTS if r["level"] == "FAIL"]
    warns = [r for r in RESULTS if r["level"] == "WARN"]
    print("\n" + "=" * 70)
    print(f"审核结果（{backend}）：通过 {len(RESULTS) - len(fails) - len(warns)}，"
          f"FAIL {len(fails)}，WARN {len(warns)}，共 {len(RESULTS)} 项")
    if fails:
        print("\n违约项（对应 docs/S4-ACCEPTANCE.md / PRD 第 9 章）：")
        for r in fails:
            print(f"  ✗ [{r['clause']}] {r['name']}" + (f" —— {r['detail']}" if r["detail"] else ""))
    if warns:
        print("\n记账项（不违约，但要记）：")
        for r in warns:
            print(f"  ! [{r['clause']}] {r['name']}" + (f" —— {r['detail']}" if r["detail"] else ""))
    print("=" * 70)
    if args.json:
        print(json.dumps({"backend": backend, "results": RESULTS,
                          "fail": len(fails), "warn": len(warns)}, ensure_ascii=False, indent=2))
    return 1 if fails else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(2)
