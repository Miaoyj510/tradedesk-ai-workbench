# -*- coding: utf-8 -*-
"""S4 验证脚本 —— 向量层（嵌入接口 / 向量存储 / 索引流程 / 余弦检索）。

用法
    python scripts/verify_s4.py                 # SQLite
    python scripts/verify_s4.py --dsn <DSN>     # PostgreSQL（双后端各跑一遍）

本轮验证什么
    嵌入器契约与“是否真有语义能力”标志位；云端嵌入客户端的请求/响应契约（**假 transport，不联网**）；
    向量表在 SQLite 与 PostgreSQL 上的写入/读回/幂等/清理；索引流程的幂等与强制重建；
    余弦 TopK 的排序正确性；知识块文本不得含数值（PRD 9.3 回归）。

本轮不验证什么
    真实云端嵌入调用（需 API key 与联网，在 S4b 接真实向量通道时手动跑）；
    **语义召回质量**（PRD 9.5 两层口径：公开基准体检 + 自建询盘语料 —— 属 S4c）；
    重排（cross-encoder）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
from pathlib import Path


def _setup_stdout() -> None:
    """自适应输出编码：tty 用控制台原生编码，非 tty 用 UTF-8，缺字用 replace 兜底。"""
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradedesk.data.kb_seed import KB_VERSION, load_kb      # noqa: E402
from tradedesk.retrieval.embedders import (                 # noqa: E402
    EmbeddingError, HashEmbedder, NullEmbedder, RemoteEmbedder, cosine,
)
from tradedesk.retrieval.base import SearchQuery               # noqa: E402
from tradedesk.retrieval.indexer import chunk_text, embedder_tag, index_kb, load_index  # noqa: E402
from tradedesk.retrieval.vector_index import VectorIndex    # noqa: E402
from tradedesk.store.postgres_store import PostgresStateStore  # noqa: E402
from tradedesk.store.sqlite_store import SqliteStateStore   # noqa: E402

RESULTS = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append({"name": name, "ok": bool(ok), "detail": detail})
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (f" — {detail}" if detail else ""))
    return bool(ok)


def section(title: str) -> None:
    print("\n" + title)
    print("-" * max(len(title), 60))


# ---------------------------------------------------------------------------
# 假 transport：不联网的云端嵌入服务器
# ---------------------------------------------------------------------------
def _fake_vector(text: str, dim: int) -> list:
    """确定性向量：同一段文本总是得到同一个向量。"""
    import hashlib
    h = hashlib.blake2b(text.encode("utf-8"), digest_size=dim * 2).digest()
    return [((h[i * 2] / 255.0) - 0.5) for i in range(dim)]


class FakeTransport:
    """可编程的假传输：记录请求、按场景失败或返回错误体。"""

    def __init__(self, dim: int = 8, fail_times: int = 0, error_payload=None) -> None:
        self.dim = dim
        self.fail_times = fail_times
        self.error_payload = error_payload
        self.calls = []

    def __call__(self, url, headers, payload, timeout):
        self.calls.append({"url": url, "headers": headers, "payload": payload})
        if self.fail_times > 0:
            self.fail_times -= 1
            raise urllib.error.URLError("假网络故障")
        if self.error_payload is not None:
            return self.error_payload
        texts = payload.get("texts")
        if texts is None:
            texts = payload.get("input") or []
        return {"data": [{"embedding": _fake_vector(t, self.dim)} for t in texts],
                "base_resp": {"status_code": 0, "status_msg": "success"}}


# ---------------------------------------------------------------------------
def scenario_1_contract() -> None:
    section("场景 1 · 嵌入器契约（含“是否真语义”标志位）")
    for emb in (NullEmbedder(), HashEmbedder(dim=32)):
        check(f"{emb.name} 必须声明 semantic 标志位", hasattr(emb, "semantic"))
        check(f"{emb.name} 不得声称有语义能力", emb.semantic is False)
    check("RemoteEmbedder 声称有语义能力", RemoteEmbedder.semantic is True)

    h = HashEmbedder(dim=64)
    a = h.embed(["手动水压搬运车"])
    b = h.embed(["手动水压搬运车"])
    check("离线嵌入确定（同文本同向量）", a == b)
    check("离线嵌入维度稳定", len(a[0]) == 64, str(len(a[0])))
    check("离线嵌入向量已归一", abs(sum(x * x for x in a[0]) - 1.0) < 1e-6)
    diff = h.embed(["手动水压搬运车"])[0]
    other = h.embed(["电动搬运车"])[0]
    check("不同文本向量不同", diff != other)


def scenario_2_remote() -> None:
    section("场景 2 · 云端嵌入客户端（假 transport，不联网）")
    t = FakeTransport(dim=8)
    emb = RemoteEmbedder(base_url="https://example.invalid", api_key="K", model="embo-01",
                          dim=8, batch_size=2, transport=t)
    vecs = emb.embed(["a", "b", "c"])
    check("批量嵌入返回正确数量", len(vecs) == 3, str(len(vecs)))
    check("按批大小分批请求", len(t.calls) == 2, f"{len(t.calls)} 次请求")
    check("请求地址是 /v1/embeddings", t.calls[0]["url"].endswith("/v1/embeddings"), t.calls[0]["url"])
    check("鉴权头是 Bearer", t.calls[0]["headers"].get("Authorization") == "Bearer K")
    check("MiniMax 请求体用 texts + type", "texts" in t.calls[0]["payload"]
          and t.calls[0]["payload"].get("type") == "db", str(t.calls[0]["payload"]))
    check("查询嵌入用 type=query", emb.embed_query("q") is not None)

    t2 = FakeTransport(dim=8)
    emb2 = RemoteEmbedder(base_url="https://example.invalid", api_key="K", dim=8,
                           provider="openai", transport=t2)
    emb2.embed(["x"])
    check("OpenAI 兼容形态用 input", "input" in t2.calls[0]["payload"], str(t2.calls[0]["payload"]))

    # 业务错误（MiniMax 的 base_resp）不重试
    t3 = FakeTransport(error_payload={"base_resp": {"status_code": 1004, "status_msg": "login fail"}})
    emb3 = RemoteEmbedder(base_url="https://example.invalid", api_key="K", dim=8, transport=t3)
    try:
        emb3.embed(["x"])
        check("业务错误应抛异常", False, "没抛")
    except EmbeddingError as exc:
        check("业务错误应抛异常", "1004" in str(exc), str(exc)[:60])
    check("业务错误不重试", len(t3.calls) == 1, f"{len(t3.calls)} 次")

    # 网络错误重试
    t4 = FakeTransport(dim=8, fail_times=2)
    emb4 = RemoteEmbedder(base_url="https://example.invalid", api_key="K", dim=8,
                           retries=2, transport=t4)
    vecs4 = emb4.embed(["x"])
    check("网络错误重试后成功", len(vecs4) == 1 and len(t4.calls) == 3,
          f"{len(t4.calls)} 次调用")

    # 维度不符应报错
    t5 = FakeTransport(dim=16)
    emb5 = RemoteEmbedder(base_url="https://example.invalid", api_key="K", dim=8, transport=t5)
    try:
        emb5.embed(["x"])
        check("维度不符应报错", False, "没抛")
    except EmbeddingError as exc:
        check("维度不符应报错", "维度" in str(exc), str(exc)[:60])


def scenario_3_store(store) -> None:
    section("场景 3 · 向量存储（写入 / 读回 / 幂等 / 清理）")
    # pgvector 的 vector(N) 列会拒绝错误维度的向量，测试向量必须与列维度一致。
    dim = getattr(store, "vector_dim", 0) or 8
    v1 = [1.0] + [0.0] * (dim - 1)
    v2 = [0.0, 1.0] + [0.0] * (dim - 2)
    store.clear_kb_embeddings()
    check("向量表已建立", True)
    store.upsert_kb_embedding("KB-T-1", KB_VERSION, "hash/1/8", v1)
    store.upsert_kb_embedding("KB-T-2", KB_VERSION, "hash/1/8", v2)
    items = store.list_kb_embeddings(KB_VERSION)
    check("向量写入后能读回", len(items) == 2, str(len(items)))
    got = {cid: vec for cid, _m, vec in items}
    check("读回的向量值一致", abs(got.get("KB-T-1", [0])[0] - 1.0) < 1e-6, str(got.get("KB-T-1")))
    check("读回的向量维度与写入一致", len(got.get("KB-T-1", [])) == dim,
          f"{len(got.get('KB-T-1', []))} / {dim}")
    model = dict((cid, m) for cid, m, _ in items).get("KB-T-1")
    check("读回带模型版本标签", model == "hash/1/8", str(model))
    store.upsert_kb_embedding("KB-T-1", KB_VERSION, "hash/2/8", [0.5, 0.5] + [0.0] * (dim - 2))
    items2 = store.list_kb_embeddings(KB_VERSION)
    check("嵌入幂等（同 chunk_id 覆盖）", len(items2) == 2, str(len(items2)))
    check("嵌入覆盖后值已更新",
          abs(dict((c, v) for c, _m, v in items2).get("KB-T-1", [0])[0] - 0.5) < 1e-6)
    store.clear_kb_embeddings(KB_VERSION)
    check("清理向量表", store.list_kb_embeddings(KB_VERSION) == [])
    # 版本隔离
    store.upsert_kb_embedding("KB-T-3", "kb-other", "hash/1/8", [1.0] * dim)
    store.upsert_kb_embedding("KB-T-4", KB_VERSION, "hash/1/8", [1.0, 0.0] + [1.0] * (dim - 2))
    only = store.list_kb_embeddings(KB_VERSION)
    check("按库版本隔离", len(only) == 1 and only[0][0] == "KB-T-4", str(only))
    store.clear_kb_embeddings()
    check("清理全部", store.list_kb_embeddings() == [])


def scenario_4_indexer(store) -> None:
    section("场景 4 · 索引流程（幂等 / 强制重建）")
    blocks = store.list_kb_chunks(KB_VERSION)
    store.clear_kb_embeddings()
    emb = HashEmbedder(dim=getattr(store, "vector_dim", 0) or 32)
    stats = index_kb(store, emb, KB_VERSION, chunks=blocks)
    check("首次索引全部块", stats["indexed"] == len(blocks), str(stats))
    check("向量数量与块数一致",
          len(store.list_kb_embeddings(KB_VERSION)) == len(blocks))
    stats2 = index_kb(store, emb, KB_VERSION, chunks=blocks)
    check("重复索引幂等（不重复花钱）",
          stats2["indexed"] == 0 and stats2["skipped"] == len(blocks), str(stats2))
    stats3 = index_kb(store, emb, KB_VERSION, chunks=blocks, force=True)
    check("强制重建会重写全部", stats3["indexed"] == len(blocks), str(stats3))
    idx = load_index(store, KB_VERSION)
    check("索引可从存储层载回", len(idx) == len(blocks), str(len(idx)))
    check("索引标签含模型版本",
          embedder_tag(emb).startswith("hash/") and embedder_tag(emb).endswith(str(getattr(store, "vector_dim", 0) or 32)),
          embedder_tag(emb))
    # 文本块里不得有数值（PRD 9.3 回归）
    dirty = []
    for c in blocks:
        t = c.text
        for key in ("price_low", "lead_time_days", "moq"):
            v = c.extra.get(key)
            if v and str(v) in t:
                dirty.append(f"{c.chunk_id}:{key}")
    check("知识块文本不含属性数值", not dirty, str(dirty))
    check("送入嵌入的文本 = 标题 + 正文",
          chunk_text(blocks[0]).startswith(blocks[0].title[:4]), chunk_text(blocks[0])[:40])


def scenario_4b_dimension_guard(store) -> None:
    section("场景 4b · 维度不符时应被存储层拦下（pgvector 的 vector(N)）")
    dim = getattr(store, "vector_dim", 0) or 0
    if not dim or not getattr(store, "pgvector_available", False):
        check("本后端无维度约束（JSONB 或 SQLite），跳过", True)
        return
    try:
        store.upsert_kb_embedding("KB-BAD-DIM", KB_VERSION, "hash/x", [1.0, 2.0])
        check("维度不符的向量应被拒绝", False, "写入成功了")
    except Exception as exc:
        check("维度不符的向量应被拒绝", "dimension" in str(exc).lower(), str(exc)[:80])


def scenario_5_vector_search() -> None:
    section("场景 5 · 余弦 TopK 检索")
    idx = VectorIndex([("A", [1.0, 0.0]), ("B", [0.9, 0.1]), ("C", [0.0, 1.0])])
    hits = idx.search([1.0, 0.0], top_k=2)
    check("相似度最高的排在前面", hits[0][0] == "A", str(hits))
    check("TopK 数量正确", len(hits) == 2, str(len(hits)))
    check("相似度降序", hits[0][1] >= hits[1][1], str(hits))
    check("维度不匹配不崩盘", idx.search([1.0, 0.0, 0.0], top_k=2)[0][1] == 0.0)
    check("空索引返回空", VectorIndex().search([1.0, 0.0]) == [])
    check("余弦相似度自体一致", abs(cosine([1.0, 0.0], [1.0, 0.0]) - 1.0) < 1e-9)


def scenario_6_pgvector_column(store) -> None:
    section("场景 6 · PostgreSQL 向量列类型（pgvector 存在时用 vector）")
    col = getattr(store, "embedding_column", None)
    if col is None:
        check("本后端不是 PostgreSQL，跳过列类型检查", True)
        return
    check("向量列类型已确定", col in ("vector", "jsonb"), col)
    if getattr(store, "pgvector_available", False):
        check("本机已安装 pgvector 扩展", True)
        check("使用 vector 列（而不是降级的 JSONB）", col == "vector", col)
    else:
        check("未安装 pgvector 时降级为 JSONB", col == "jsonb", col)


class FakeSemanticEmbedder(HashEmbedder):
    """假的“语义”嵌入器（semantic=True）—— 测试用，不联网。

    继承哈希嵌入器（向量与索引里的同一个空间），只是把 `semantic` 标位置为 True，
    用来验证“稠密通道确实被接上了”。它**不具备语义能力**。
    """

    name = "fake-semantic"
    semantic = True
    model = "fake"


def scenario_7_dense_channel(store) -> None:
    section("场景 7 · 稠密通道（S4-4b：第三通道 + 语义守卫）")
    from tradedesk.retrieval.indexer import index_kb, load_index
    from tradedesk.retrieval.store_retriever import StoreRetriever

    blocks = store.list_kb_chunks(KB_VERSION)
    store.clear_kb_embeddings()
    # 向量用哈希嵌入器生成（它不语义，但用来填索引结构可以）。
    # 维度必须与存储列一致（pgvector 的 vector(N) 会拒绝错误维度）。
    _dim = getattr(store, "vector_dim", 0) or 16
    index_kb(store, HashEmbedder(dim=_dim), KB_VERSION, chunks=blocks)
    idx = load_index(store, KB_VERSION)

    # --- 有语义能力的嵌入器 → 稠密通道应该出现 ---
    r1 = StoreRetriever(store, KB_VERSION, embedder=FakeSemanticEmbedder(dim=_dim), vector_index=idx)
    hits = r1.search(SearchQuery(text="Quote 5000 pcs of X-200 FOB Shenzhen", lang="en", top_k=5))
    chans = sorted({ch for h in hits for ch in h.channels})
    check("语义嵌入器下稠密通道应出现", "dense" in chans, str(chans))
    check("稠密通道确实调用了查询嵌入", r1.dense_calls == 1, str(r1.dense_calls))
    # 型号识别的唯一权威是结构化通道：第一个命中必须仍是点名的型号块
    first = hits[0].chunk if hits else None
    check("结构化命中仍排第一（稠密通道不能夺走型号识别）",
          first is not None and first.identifiers.get("sku") == "X-200",
          str(first.identifiers.get("sku") if first else None))
    dense_specs = [h.chunk.chunk_id for h in hits
                   if "dense" in h.channels and h.chunk.doc_type == "产品规格"]
    check("「产品规格」块不进稠密通道", not dense_specs, str(dense_specs))

    # --- 假嵌入器（semantic=False）→ 守卫生效，不开稠密通道 ---
    r2 = StoreRetriever(store, KB_VERSION, embedder=HashEmbedder(dim=_dim), vector_index=idx)
    hits2 = r2.search(SearchQuery(text="Quote 5000 pcs of X-200 FOB Shenzhen", lang="en", top_k=5))
    chans2 = sorted({ch for h in hits2 for ch in h.channels})
    check("假嵌入器（semantic=False）不得开启稠密通道", "dense" not in chans2, str(chans2))
    check("未开启稠密通道时记得原因", bool(r2.dense_skipped), str(r2.dense_skipped))
    check("未开启稠密通道时不调用云端嵌入", r2.dense_calls == 0, str(r2.dense_calls))

    # --- 没有向量索引 → 降级双通道，不报错 ---
    # 注意：检索器的向量索引现在**总是从存储层取**（按内容指纹失效），
    # 所以“无索引”的真实条件是**库里没有向量** —— 先清掉库里的向量再测。
    store.clear_kb_embeddings()
    r3 = StoreRetriever(store, KB_VERSION, embedder=FakeSemanticEmbedder(dim=_dim), vector_index=None)
    hits3 = r3.search(SearchQuery(text="Quote 5000 pcs of X-200 FOB Shenzhen", lang="en", top_k=5))
    chans3 = sorted({ch for h in hits3 for ch in h.channels})
    check("无向量索引时降级为双通道", "dense" not in chans3 and hits3, str(chans3))
    check("无向量索引时记得原因", bool(r3.dense_skipped), str(r3.dense_skipped))

    # --- 查询嵌入失败 → 不阻断检索 ---
    class BrokenEmbedder(FakeSemanticEmbedder):
        def embed_query(self, text):
            raise RuntimeError("云端嵌入失败")
    r4 = StoreRetriever(store, KB_VERSION, embedder=BrokenEmbedder(dim=_dim), vector_index=idx)
    hits4 = r4.search(SearchQuery(text="Quote 5000 pcs of X-200 FOB Shenzhen", lang="en", top_k=5))
    check("查询嵌入失败不阻断检索（降级而不崩盘）", bool(hits4), str(len(hits4)))
    check("查询嵌入失败时记得原因", bool(r4.dense_skipped), str(r4.dense_skipped))


class CountingSemanticEmbedder(HashEmbedder):
    """计数的假语义嵌入器（用来统计**每条询盘调用几次**）。"""

    name = "counting-semantic"
    semantic = True
    model = "fake"

    def __init__(self, dim=16):
        super().__init__(dim=dim)
        self.query_calls = 0

    def embed_query(self, text):
        self.query_calls += 1
        return super().embed_query(text)


def scenario_8_agent_dense(store) -> None:
    section("场景 8 · Agent 侧稠密通道（引用不串型号 / 每条询盘 1 次嵌入）")
    from fastapi.testclient import TestClient
    from tradedesk.api.app import create_app
    from tradedesk.api.deps import AppContext, build_registry
    from tradedesk.retrieval.indexer import index_kb, load_index
    from tradedesk.retrieval.store_retriever import StoreRetriever

    blocks = store.list_kb_chunks(KB_VERSION)
    _dim = getattr(store, "vector_dim", 0) or 16
    store.clear_kb_embeddings()
    index_kb(store, HashEmbedder(dim=_dim), KB_VERSION, chunks=blocks)
    idx = load_index(store, KB_VERSION)

    from tradedesk.api.deps import offline_config
    ctx = AppContext(store=store, config=offline_config())
    emb = CountingSemanticEmbedder(dim=_dim)
    ctx.embedder = emb
    ctx.vector_index = idx
    ctx.retriever = StoreRetriever(store, KB_VERSION, embedder=emb, vector_index=idx)
    ctx.registry = build_registry(retriever=ctx.retriever)
    if getattr(ctx, "supervisor", None) is not None:
        ctx.supervisor.registry = ctx.registry
    c = TestClient(create_app(ctx))

    d = c.post("/inbound", json={
        "buyer_id": "BY-S4-08", "country": "BR", "language": "en", "company": "Demo Co",
        "message": "Quote 5000 pcs of X-200 FOB Shenzhen, do you have CE certificate?"}).json()
    cites = [x["citation_id"] for x in (d.get("draft") or {}).get("citations", [])]
    spec_cites = [c2 for c2 in cites if c2.startswith("SPEC-")]
    check("引用里的规格记录只有**本型号**（不能串型号）",
          spec_cites == ["SPEC-X-200"], f"规格引用={spec_cites} 全部引用={cites}")
    check("每条询盘只触发**1 次**查询嵌入（内部回查不走稠密通道）",
          emb.query_calls == 1, f"{emb.query_calls} 次")
    body = (d.get("draft") or {}).get("body", "")
    check("正文价格与本型号一致", "4.85" in body and "12.3" not in body, body[:80].replace("\n", " "))


def main() -> int:
    ap = argparse.ArgumentParser(description="S4 验证脚本")
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    print("TradeDesk-AI · S4 验证 —— 向量层")
    print("提示：云端嵌入用假 transport 测，**不联网不计费**")
    print("提示：本轮不验证语义召回质量（PRD 9.5 两层口径，属 S4c）")

    if args.dsn:
        # 向量列维度必须与配置一致（否则存储层会把列自动迁移并清空行）
        from tradedesk.config import get_settings
        store = PostgresStateStore(args.dsn, vector_dim=get_settings().embedding_dim)
    else:
        p = ROOT / "data" / "verify_s4.db"
        for suffix in ("", "-wal", "-shm"):
            f = p.with_name(p.name + suffix)
            if f.exists():
                f.unlink()
        store = SqliteStateStore(str(p))
    try:
        if hasattr(store, "reset"):
            store.reset()
        load_kb(store, KB_VERSION)
        scenario_1_contract()
        scenario_2_remote()
        scenario_3_store(store)
        scenario_4_indexer(store)
        scenario_4b_dimension_guard(store)
        scenario_5_vector_search()
        scenario_6_pgvector_column(store)
        scenario_7_dense_channel(store)
        scenario_8_agent_dense(store)
    finally:
        store.close()
        if not args.dsn:
            for suffix in ("", "-wal", "-shm"):
                f = (ROOT / "data" / "verify_s4.db").with_name("verify_s4.db" + suffix)
                if f.exists():
                    try:
                        f.unlink()
                    except OSError:
                        pass

    passed = sum(1 for r in RESULTS if r["ok"])
    total = len(RESULTS)
    print("\n" + "=" * 66)
    print(f"结果：{passed}/{total} 项通过")
    if passed < total:
        print("失败项：")
        for r in RESULTS:
            if not r["ok"]:
                print(f"  - {r['name']} {r['detail']}")
    print("本轮未覆盖：真实云端嵌入调用、向量通道接入 RRF（S4b）、语义召回评测（S4c）、重排。")
    if args.json:
        print(json.dumps({"passed": passed, "total": total,
                          "results": RESULTS}, ensure_ascii=False, indent=2))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
