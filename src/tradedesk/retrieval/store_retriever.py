# -*- coding: utf-8 -*-
"""从存储层取块并跑检索管线。

缓存策略：**按内容指纹失效**，不按块数。

早先的缓存键是 (kb_version,) 加块数，于是"改了价格但块数没变"时，
长驻进程里的检索层拿到的还是旧内容（R11-07）—— 知识库改了，检索层不认。
现在每次读库后算一次内容指纹（块标识 + 正文 + 属性 + 时效），指纹变了才重建管线。

演示规模（十几块）下每次读库的成本可以忽略。S4 上量后应改成
"库版本号 + updated_at"，由存储层提供版本列，而不是在应用层反复算指纹。
"""
from __future__ import annotations

import hashlib
import json
from typing import List, Optional

from ..domain.models import KBChunk
from .base import SearchHit, SearchQuery
from .pipeline import RetrievalPipeline


def _fingerprint(chunks: List[KBChunk]) -> str:
    """内容指纹：块标识 + 正文 + 标识 + 属性 + 国别 + 时效。改任何一处，指纹就变。"""
    h = hashlib.blake2b(digest_size=16)
    for c in sorted(chunks, key=lambda x: x.chunk_id):
        payload = json.dumps({
            "id": c.chunk_id,
            "doc": c.doc_id,
            "type": c.doc_type,
            "lang": c.lang,
            "text": c.text,
            "ids": c.identifiers,
            "extra": c.extra,
            "hs": sorted(c.hs_codes),
            "countries": sorted(c.countries),
            "from": c.effective_from.isoformat() if c.effective_from else None,
            "to": c.effective_to.isoformat() if c.effective_to else None,
            "until": c.valid_until.isoformat() if c.valid_until else None,
        }, sort_keys=True, ensure_ascii=False, default=str)
        h.update(payload.encode("utf-8"))
        h.update(b"|")
    return h.hexdigest()


class StoreRetriever:
    """检索器实现：块来自存储层（可持久化、可版本化），管线可替换（PRD 选型轴④）。"""

    def __init__(self, store, kb_version: str = "") -> None:
        self.store = store
        self.kb_version = kb_version
        self._pipeline: Optional[RetrievalPipeline] = None
        self._fingerprint: str = ""

    def chunks(self) -> List[KBChunk]:
        if self.kb_version:
            return self.store.list_kb_chunks(self.kb_version)
        return self.store.list_kb_chunks()

    def _current(self) -> RetrievalPipeline:
        chunks = self.chunks()
        fp = _fingerprint(chunks)
        if self._pipeline is None or fp != self._fingerprint:
            self._pipeline = RetrievalPipeline(chunks, self.kb_version)
            self._fingerprint = fp
        return self._pipeline

    def search(self, query: SearchQuery) -> List[SearchHit]:
        return self._current().search(query)
