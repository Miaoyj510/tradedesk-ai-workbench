# -*- coding: utf-8 -*-
"""检索层抽象：检索器、嵌入器、查询与命中。"""
from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional, Protocol

from pydantic import BaseModel, Field

from ..domain.models import KBChunk


class SearchQuery(BaseModel):
    """一次检索的输入。"""

    text: str
    lang: str = "zh"
    buyer_country: str = ""          # 买家所在国（时效过滤与国别过滤用）
    hs_codes: List[str] = Field(default_factory=list)
    sku: str = ""
    doc_types: List[str] = Field(default_factory=list)
    today: Optional[date] = None     # 注入"当前日期"，保证时效过滤可测
    top_k: int = 5


class SearchHit(BaseModel):
    """一条命中：块 + 分数 + 命中原因 + 引用信息。"""

    chunk: KBChunk
    score: float
    channel: str = "lexical"          # lexical / structured / dense
    reasons: List[str] = Field(default_factory=list)
    channels: List[str] = Field(default_factory=list)   # 命中过的通道（lexical/structured）
    expired: bool = False
    not_yet_effective: bool = False

    @property
    def citation_id(self) -> str:
        ids = self.chunk.identifiers
        # doc_ref 优先于 sku：一个 sku 对应多个块时，只写 sku 指不到块（R14-03）
        for key in ("cert_no", "clause_no", "entry_id", "term_id", "doc_ref", "sku", "doc_no"):
            if ids.get(key):
                return str(ids[key])
        return self.chunk.doc_id


class Retriever(Protocol):
    """检索器接口（PRD 13.4.5 选型轴④：检索编排可替换）。"""

    def search(self, query: SearchQuery) -> List[SearchHit]: ...


class Embedder(Protocol):
    """嵌入器接口：S4 接真实嵌入模型（BGE-M3 或云端嵌入 API）。

    两个必须声明的属性：
      name    —— 嵌入实现名（写进评测报告，不得省略）
      semantic —— **是否真的具备语义能力**。假嵌入（哈希/占位）必须为 False：
               管线看到它就不得开启精确语义召回，也不得在评测里报语义能力的数字（PRD 9.5 两层口径）。
    """

    name: str
    semantic: bool

    def embed(self, texts: List[str]) -> List[List[float]]: ...
    def embed_query(self, text: str) -> List[float]: ...
