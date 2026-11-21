# -*- coding: utf-8 -*-
"""知识库向量索引流程（S4-4a）。

一个型号一条结构化规格记录 + 四个语言版本 —— 它们都要向量化，
所以索引器处理的是“块”而不是“商品”。

两个设计点
----------
1. **幂等**：已经用同一个模型版本嵌入过的块不重复嵌入。
   云端 API 是计费的（单位询盘 Token 成本 ≤ 0.2 元/条是硬指标），启动时每次全量重算就是浪费。
2. **失败不阻断启动**：向量索引建失败时，检索层降级为“词法 + 结构化”双通道，
   并且**不得**对外宣称有语义召回能力（PRD 9.5 两层口径）。
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional

from ..domain.models import KBChunk
from .base import Embedder


def chunk_text(chunk: KBChunk) -> str:
    """送进嵌入模型的文本：标题 + 正文。

    注意：PRD 9.3 要求“产品属性不进文本块”，所以这里嵌入的文本里**不会出现价格/交期/MOQ**
    这样的数值——向量通道不会拿到“约等于”的数字（PRD 9.4 难点③）。
    """
    parts = [chunk.title, chunk.text]
    return " ".join(p for p in parts if p).strip()


def embedder_tag(embedder: Embedder) -> str:
    """嵌入版本标签：用于判断需不需要重新嵌入。"""
    model = getattr(embedder, "model", "") or ""
    dim = getattr(embedder, "dim", "") or ""
    return f"{embedder.name}/{model}/{dim}".strip("/")


def chunk_fingerprint(chunk: KBChunk) -> str:
    """块内容指纹（失效键的一部分）。

    R15-01：失效键只有 name/model/dim 时，块正文改了但库版本号没升 → 向量不重算 →
    向量通道返回旧内容。这正是 S3 那个 R11-07（“改库后同进程不生效”）在向量层的翻版，
    所以这里直接照抄内容指纹的做法。
    """
    import hashlib
    h = hashlib.blake2b(digest_size=16)
    payload = json.dumps({
        "title": chunk.title, "text": chunk.text, "lang": chunk.lang,
        "ids": chunk.identifiers, "extra": chunk.extra,
    }, sort_keys=True, ensure_ascii=False, default=str)
    h.update(payload.encode("utf-8"))
    return h.hexdigest()


def index_kb(store, embedder: Embedder, kb_version: str,
             chunks: Optional[List[KBChunk]] = None,
             force: bool = False) -> Dict[str, int]:
    """给知识库的块生成向量并落库。返回 {"indexed": n, "skipped": m}。"""
    blocks = chunks if chunks is not None else store.list_kb_chunks(kb_version)
    # 注：规格块仍然被嵌入（白花三次钱）—— N1 记的是建议，但验收契款 S15-15 要求「索引全部块」，
    # 改成不嵌入会让「块内容改了向量必须重算」这条断言失效（因为第一个块就是规格块）。
    tag = embedder_tag(embedder)
    if not force:
        # 失效键 = 模型版本 + **块内容指纹**（R15-01）：块正文改了（版本号没升）也必须重算向量
        existing = {cid: (model, fp) for cid, model, fp in store.list_kb_embedding_meta(kb_version)}
        todo = []
        for c in blocks:
            model, fp = existing.get(c.chunk_id, ("", ""))
            if model != tag or fp != chunk_fingerprint(c):
                todo.append(c)
    else:
        todo = list(blocks)
    if not todo:
        return {"indexed": 0, "skipped": len(blocks)}
    texts = [chunk_text(c) for c in todo]
    vectors = embedder.embed(texts)
    if len(vectors) != len(todo):
        raise RuntimeError(f"嵌入数量不匹配：块 {len(todo)} 个，向量 {len(vectors)} 条")
    for c, v in zip(todo, vectors):
        store.upsert_kb_embedding(c.chunk_id, kb_version, tag, v, chunk_fingerprint(c))
    return {"indexed": len(todo), "skipped": len(blocks) - len(todo)}


def load_index(store, kb_version: str) -> "VectorIndex":
    """从存储层把向量载入内存索引。"""
    from .vector_index import VectorIndex
    items = [(cid, vec) for cid, _model, vec in store.list_kb_embeddings(kb_version)]
    return VectorIndex(items)
