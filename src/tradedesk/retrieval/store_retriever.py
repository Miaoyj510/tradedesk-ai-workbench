# -*- coding: utf-8 -*-
"""从存储层取块并跑检索管线（含稠密通道，S4-4b）。"""
from __future__ import annotations

import hashlib
import json
from typing import List, Optional

from ..domain.models import KBChunk
from .base import SearchHit, SearchQuery
from .pipeline import RetrievalPipeline


def _vectors_fingerprint(items) -> str:
    """向量集合的内容指纹（改向量就变）。"""
    h = hashlib.blake2b(digest_size=16)
    for cid, _model, vec in sorted(items, key=lambda x: x[0]):
        h.update(cid.encode("utf-8"))
        h.update(b"|")
        for x in vec:
            h.update(b"%.5f," % x)
        h.update(b"\n")
    return h.hexdigest()


def _chunks_fingerprint(chunks: List[KBChunk]) -> str:
    """块集合的内容指纹（改任何一块的内容都会变）。"""
    h = hashlib.blake2b(digest_size=16)
    for c in sorted(chunks, key=lambda x: x.chunk_id):
        h.update(json.dumps({
            "id": c.chunk_id, "doc": c.doc_id, "type": c.doc_type, "title": c.title,
            "text": c.text, "lang": c.lang, "ids": c.identifiers, "extra": c.extra,
            "countries": sorted(c.countries), "hs": sorted(c.hs_codes),
        }, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8"))
        h.update(b"|")
    return h.hexdigest()


class StoreRetriever:
    """检索器实现：块来自存储层（可持久化、可版本化），管线可替换（PRD 选型轴③）。

    稠密通道（向量）的三条守则（都是实测得出的，不是想得得的）：
    1. **语义守卫**：嵌入器 `semantic=False` 时（占位/哈希）**不开启稠密通道** ——
       假嵌入的分数看起来也很正常，会被误当成语义能力。未启用时降级为双通道并记得原因。
    2. **权重最低**：稠密通道的 RRF 参数（k=100）比结构化通道（k=10）大 —— 型号识别只认结构化通道。
    3. **规格块不进稠密通道**：“产品规格”块的文本是模板句（三个 SKU 高度相似），
       它们只走属性过滤，进向量库只是噪声。
    """

    def __init__(self, store, kb_version: str = "", embedder=None, vector_index=None) -> None:
        self.store = store
        self.kb_version = kb_version
        self.embedder = embedder
        self.vector_index = vector_index
        self._cache: List[KBChunk] = []
        self._cache_key: Optional[tuple] = None
        self._vec_index = None            # 按内容指纹失效的向量索引（R17-03）
        self._vec_fp = ""
        self.dense_calls = 0          # 稠密通道**云端 HTTP 调用次数**（计费口径，S5 结算）
        self.dense_skipped: Optional[str] = None   # 未启用稠密通道的原因

    def chunks(self) -> List[KBChunk]:
        """读库并缓存块（**按内容指纹失效**）。

        R11-07：缓存键只看块数时，“改价不改块数”进程内拿到的仍是旧内容 ——
        改了块、版本号没升就不会生效。这里按内容指纹判断是否重建。
        """
        chunks = self.store.list_kb_chunks(self.kb_version) if self.kb_version else self.store.list_kb_chunks()
        key = _chunks_fingerprint(chunks)
        if key != self._cache_key:
            self._cache = chunks
            self._cache_key = key
        return self._cache

    def search(self, query: SearchQuery) -> List[SearchHit]:
        blocks = self.chunks()
        dense = self.dense_hits(query, blocks=blocks) if getattr(query, "use_dense", True) else []
        return RetrievalPipeline(blocks, self.kb_version).search(query, dense_hits=dense)

    def _current_vector_index(self):
        """向量索引按**内容指纹**失效（R17-03）。

        旧写法把启动时的索引当一次性快照，发布流水线重建索引后应用不重启仍返回旧向量 ——
        这正是 S3 的 R11-07（改库后同进程不生效）在向量层的翻版。
        """
        items = self.store.list_kb_embeddings(self.kb_version)
        fp = _vectors_fingerprint(items)
        if self._vec_index is None or fp != self._vec_fp:
            from .vector_index import VectorIndex
            self._vec_index = VectorIndex([(cid, vec) for cid, _m, vec in items])
            self._vec_fp = fp
        return self._vec_index

    # ---- 稠密通道 ----
    def dense_hits(self, query: SearchQuery, blocks=None) -> List[SearchHit]:
        """稠密通道命中（公开：评测脚本需单独取这一通道做对比）。"""
        emb = self.embedder
        idx = self._current_vector_index()
        self.dense_skipped = None
        if emb is None:
            self.dense_skipped = "未配置嵌入器"
            return []
        if idx is None or len(idx) == 0:
            idx = self.vector_index      # 注入的索引作为兜底
            if idx is None or len(idx) == 0:
                self.dense_skipped = "未配置嵌入器或向量索引为空"
                return []
        if not getattr(emb, "semantic", False):
            self.dense_skipped = "嵌入器不具备语义能力（semantic=False），不开启稠密通道"
            return []
        try:
            q = emb.embed_query(query.text)
        except Exception as exc:
            self.dense_skipped = f"查询嵌入失败：{exc}"
            return []
        # 计量口径（R17-11）：计**实际云端 HTTP 调用次数**，不是 embed_query 次数 ——
        # 网络故障时会重试（默认 2 次），重试也要花钱，指数的上限会把成本基线低估到 1/3。
        self.dense_calls = getattr(emb, "calls", self.dense_calls + 1)
        top_k = max(query.top_k * 2, 8)
        # 优先用存储层下推的近邻检索（PostgreSQL 走 pgvector + HNSW），否则用内存索引
        # 稠密通道的任何异常（维度不符 / 库里没索引 / 查询失败）都只能降级，
        # 不得拖垮整个检索（访问稡型必须拿得到答复）。
        try:
            _pg = getattr(self.store, "search_kb_vectors", None)
            pairs = _pg(q, top_k) if callable(_pg) else idx.search(q, top_k=top_k)
        except Exception as exc:
            self.dense_skipped = f"近邻检索失败（降级为双通道）：{exc}"
            return []
        by_id = {c.chunk_id: c for c in (blocks if blocks is not None else self.chunks())}
        hits: List[SearchHit] = []
        for cid, score in pairs:
            c = by_id.get(cid)
            if c is None:
                continue
            if c.doc_type == "产品规格":
                continue        # 规格块只走属性过滤，不进语义通道
            hits.append(SearchHit(chunk=c, score=round(float(score), 6), channel="dense",
                                  reasons=[f"语义召回 {score:.4f}"]))
        return hits
