# -*- coding: utf-8 -*-
"""从存储层取块并跑检索管线（演示规模：每次按 kb_version 缓存）。"""
from __future__ import annotations

from typing import List, Optional

from ..domain.models import KBChunk
from .base import SearchHit, SearchQuery
from .pipeline import RetrievalPipeline


class StoreRetriever:
    """检索器实现：块来自存储层（可持久化、可版本化），管线可替换（PRD 选型轴④）。"""

    def __init__(self, store, kb_version: str = "") -> None:
        self.store = store
        self.kb_version = kb_version
        self._cache: List[KBChunk] = []
        self._cache_key: Optional[tuple] = None

    def chunks(self) -> List[KBChunk]:
        key = (self.kb_version,)
        chunks = self.store.list_kb_chunks(self.kb_version) if self.kb_version else self.store.list_kb_chunks()
        if self._cache_key != key or len(chunks) != len(self._cache):
            self._cache = chunks
            self._cache_key = key
        return self._cache

    def search(self, query: SearchQuery) -> List[SearchHit]:
        return RetrievalPipeline(self.chunks(), self.kb_version).search(query)
