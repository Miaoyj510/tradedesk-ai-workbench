# -*- coding: utf-8 -*-
"""向量索引（S4-4a）：从存储层读向量，做余弦 TopK。

为什么这里是内存计算而不是一律推到数据库做
--------------------------
演示规模（几十块）下，把向量拉到进程内计算余弦是最简单也最快的；
而且这里能把“向量搜索”的**口径**定在一个可测的函数里。S4b 接 RRF 第三通道时，
换成数据库内计算（pgvector 的 `<=>`）只是换一个实现类，接口不变。

注意：这里的检索**不能被当成语义能力**。向量是什么计算出来的，就只能证明“这个嵌入模型能找到什么”；
语义能力要报 PRD 9.5 的两层口径（公开基准体检 + 自建询盘语料）。
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from .embedders import cosine


class VectorIndex:
    """内存向量索引（按 chunk_id 排序，保证结果稳定）。"""

    def __init__(self, items: Optional[Sequence[Tuple[str, List[float]]]] = None) -> None:
        self._ids: List[str] = []
        self._vectors: List[List[float]] = []
        if items:
            self.load(items)

    def load(self, items: Sequence[Tuple[str, List[float]]]) -> None:
        self._ids = [cid for cid, _ in items]
        self._vectors = [list(v) for _, v in items]

    def __len__(self) -> int:
        return len(self._ids)

    @property
    def ids(self) -> List[str]:
        return list(self._ids)

    def search(self, query: Sequence[float], top_k: int = 10,
               min_score: float = 0.0) -> List[Tuple[str, float]]:
        """返回 [(chunk_id, 余弦相似度)]，按相似度降序。"""
        if not query or not self._ids:
            return []
        scored: List[Tuple[str, float]] = []
        for cid, vec in zip(self._ids, self._vectors):
            s = cosine(query, vec)
            if s >= min_score:
                scored.append((cid, round(s, 6)))
        scored.sort(key=lambda x: (-x[1], x[0]))
        return scored[:top_k]
