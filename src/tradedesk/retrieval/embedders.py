# -*- coding: utf-8 -*-
"""嵌入器接口占位：S4 接 BGE-M3（多语种稠密向量）。

为什么现在不接
--------------
本机无 GPU（Iris Xe 共享显存 128MB）、16GB 内存，跑 BGE-M3 的 CPU 推理会拖慢检索；
而且 Docker Hub 拉不到模型镜像。S4 会用「ONNX 量化版 / 远端嵌入 API / pgvector」
三条候选之一，并在此接口后面做替换（PRD 13.4.5 选型轴③）。
"""
from __future__ import annotations

from typing import List


class NullEmbedder:
    """占位实现：不产出向量，只把文本按字符 n-gram 稀疏化。

    它让"稠密通道"在管线里真实存在（可被 RRF 融合），但**不代表语义检索能力**。
    """

    name = "null-embedder"

    def embed(self, texts: List[str]) -> List[List[float]]:
        from .lexical import char_ngrams
        vocab = sorted({g for t in texts for g in char_ngrams(t)})
        idx = {g: i for i, g in enumerate(vocab)}
        out = []
        for t in texts:
            vec = [0.0] * len(vocab)
            for g in char_ngrams(t):
                if g in idx:
                    vec[idx[g]] = 1.0
            out.append(vec)
        return out
