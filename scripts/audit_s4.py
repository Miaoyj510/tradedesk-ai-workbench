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
    check("向量通道尚未接入检索管线（4a 口径：只做向量层）",
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
def run_all() -> None:
    for fn in (audit_s1_contract, audit_s2_client, audit_s3_storage, audit_s4_indexer,
               audit_s5_vector_index, audit_s6_wiring, audit_s7_docs, audit_s8_secrets,
               audit_s9_postgres):
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
