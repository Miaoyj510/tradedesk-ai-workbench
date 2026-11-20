# -*- coding: utf-8 -*-
"""检索管线七步（PRD 9.3）：归一 → 术语映射 → 查询扩展 → 属性过滤 → 双通道召回 → RRF 融合 → 引用装配。

每一步都是独立函数，可单测；管线本身不碰 IO（块由调用方喂进来），便于换检索实现。
"""
from __future__ import annotations

import math
import re
from collections import Counter
from datetime import date
from typing import Dict, Iterable, List, Optional, Tuple

from ..domain.models import KBChunk
from .base import SearchHit, SearchQuery
from .lexical import char_ngrams, normalize_text, tokenize

# ---------------------------------------------------------------------------
# 术语表：同一概念的多语说法（PRD 9.2）。检索侧用来把买家说法映射到标准概念。
# ---------------------------------------------------------------------------
TERM_TABLE: Dict[str, Dict[str, List[str]]] = {
    "moq": {"zh": ["最小起订量", "起订量"], "en": ["minimum order quantity", "moq"],
            "es": ["cantidad mínima de pedido", "pedido mínimo"],
            "ar": ["الحد الأدنى للطلب", "الحد الأدنى للكمية المطلوبة"]},
    "lead_time": {"zh": ["交期", "交货期", "货期"], "en": ["lead time", "delivery time"],
                  "es": ["plazo de entrega", "tiempo de entrega"],
                  "ar": ["مدة التسليم", "مدة التوريد", "موعد التسليم", "مدة التوصيل"]},
    "price": {"zh": ["价格", "报价", "单价"], "en": ["price", "quotation", "quote", "unit price"],
              "es": ["precio", "cotización", "precio de referencia"],
              "ar": ["سعر", "عرض سعر"]},
    "material": {"zh": ["材质", "材料"], "en": ["material"], "es": ["material"], "ar": ["خامة"]},
    "capacity": {"zh": ["载荷", "承重", "起重量"], "en": ["capacity", "safe working load", "swl"],
                 "es": ["capacidad", "carga de trabajo segura"], "ar": ["حمولة"]},
    "certification": {"zh": ["认证", "证书", "合规"], "en": ["certificate", "certification", "ce"],
                      "es": ["certificado", "certificación"], "ar": ["شهادة"]},
    "fob": {"zh": ["装运港", "离岸价"], "en": ["fob", "port of loading"],
            "es": ["fob", "puerto de carga"], "ar": ["فوب", "ميناء التحميل"]},
    "sample": {"zh": ["样品", "打样"], "en": ["sample"], "es": ["muestra"], "ar": ["عينة"]},
    "shipping": {"zh": ["运费", "海运", "物流"], "en": ["shipping", "freight", "logistics"],
                 "es": ["envío", "flete", "logística"], "ar": ["شحن", "شحن بحري"]},
    "port": {"zh": ["港口", "目的港"], "en": ["port", "destination port"],
             "es": ["puerto"], "ar": ["ميناء"]},
    "payment": {"zh": ["付款", "定金", "账期"], "en": ["payment", "deposit", "t/t"],
                "es": ["pago", "anticipo"], "ar": ["دفع", "عربون"]},
    "complaint": {"zh": ["投诉", "破损", "索赔"], "en": ["complaint", "damaged", "compensation"],
                  "es": ["queja", "reclamación", "dañado"], "ar": ["شكوى", "تلف"]},
}


def _all_surface_forms() -> Dict[str, str]:
    """表面形式 → 概念键。"""
    out: Dict[str, str] = {}
    for concept, langs in TERM_TABLE.items():
        for forms in langs.values():
            for f in forms:
                out[normalize_text(f)] = concept
    return out


SURFACE_TO_CONCEPT = _all_surface_forms()


def map_terms(text: str) -> List[str]:
    """步骤 2：把买家说法映射到标准概念。"""
    tokens = tokenize(text)
    concepts = []
    for t in tokens:
        c = SURFACE_TO_CONCEPT.get(t)
        if c is None and t.startswith("ال") and len(t) > 4:
            # 阿拉伯语定冠词：السعر → سعر（否则带 ال 的说法永远匹配不上）
            c = SURFACE_TO_CONCEPT.get(t[2:])
        if c and c not in concepts:
            concepts.append(c)
    # 多字表面形式也要匹配（tokenize 之后单字可能丢失上下文）
    norm = normalize_text(text)
    for surface, concept in SURFACE_TO_CONCEPT.items():
        if " " in surface and surface in norm and concept not in concepts:
            concepts.append(concept)
    return concepts


def expand_tokens(text: str, concepts: Iterable[str]) -> List[str]:
    """步骤 3：查询扩展 —— 原词 + 概念的标准说法（跨语种桥）。"""
    tokens = tokenize(text)
    for c in concepts:
        for langs in TERM_TABLE.get(c, {}).values():
            for form in langs:
                tokens.extend(tokenize(form))
    return tokens


# 国别过滤只作用于这两类（FR-KB-05：「法规与名单条目……按买家所在国 + 当前日期过滤」）。
# 证书的 countries 是"适用法域"，不是"买家国别专属的法规"；按买家国别把证书滤掉，
# 会让买家自己的产品证书消失、正文改由一张不覆盖任何型号的体系证书顶替（R12-02）。
COUNTRY_SCOPED_TYPES = ("法规条款", "名单条目")


def filter_by_attributes(chunks: List[KBChunk], query: SearchQuery) -> List[KBChunk]:
    """步骤 4a：结构化过滤（国别 / HS 编码 / 型号 / 文档类型）。"""
    out = []
    for c in chunks:
        if query.doc_types and c.doc_type not in query.doc_types:
            continue
        if c.doc_type in COUNTRY_SCOPED_TYPES and c.countries and query.buyer_country:
            if query.buyer_country.upper() not in [x.upper() for x in c.countries]:
                continue
        if c.hs_codes and query.hs_codes:
            if not set(c.hs_codes) & set(query.hs_codes):
                continue
        if query.sku:
            ids = " ".join(str(v) for v in c.identifiers.values())
            if query.sku.upper() not in ids.upper() and query.sku.upper() not in c.text.upper():
                continue
        out.append(c)
    return out


def time_status(chunk: KBChunk, today: date) -> Tuple[bool, bool]:
    """步骤 4b：时效判定 → (已过期, 尚未生效)。

    - 法规：effective_to 已过 → 过期（不得输出为要求）
    - 证书：valid_until 已过 → 过期（不得引用）
    - 未到生效日 → 尚未生效（可见但不得当作"当前要求"）
    """
    expired = False
    not_yet = False
    d = today
    if chunk.effective_to is not None and chunk.effective_to.date() < d:
        expired = True
    if chunk.valid_until is not None and chunk.valid_until.date() < d:
        expired = True
    if chunk.effective_from is not None and chunk.effective_from.date() > d:
        not_yet = True
    return expired, not_yet


class BM25:
    """轻量 BM25（k1=1.5, b=0.75）。"""

    def __init__(self, docs: List[List[str]], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.docs = docs
        self.avgdl = (sum(len(d) for d in docs) / len(docs)) if docs else 0.0
        self.df: Counter = Counter()
        for d in docs:
            for t in set(d):
                self.df[t] += 1
        self.N = len(docs)

    def score(self, query_tokens: List[str], i: int) -> float:
        doc = self.docs[i]
        tf = Counter(doc)
        dl = len(doc) or 1
        s = 0.0
        for t in query_tokens:
            if t not in tf:
                continue
            idf = math.log(1 + (self.N - self.df[t] + 0.5) / (self.df[t] + 0.5))
            f = tf[t]
            s += idf * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1)))
        return s

    def doc_tokens(self, chunk: KBChunk) -> List[str]:
        """块的可检索文本：标题 + 正文 + 标识 + 标签（字段加权见 bm25_doc）。"""
        parts = [chunk.title, chunk.text]
        parts.extend(str(v) for v in chunk.identifiers.values())
        parts.extend(chunk.tags)
        return tokenize(" ".join(p for p in parts if p))


def _structured_hits(chunks: List[KBChunk], query: SearchQuery) -> List[SearchHit]:
    """步骤 5b：结构化通道 —— 型号 / 证书号 / HS 编码的精确命中。

    已过期（effective_to / valid_until 已过）的块**一律不参与**：否则过期证书会
    从结构化通道"复活"（R9 同类问题的检索侧版本）。
    """
    hits: List[SearchHit] = []
    q = normalize_text(query.text)
    today = query.today or date.today()
    # 型号型 token（X-200 / M-77 / CE-2041 这类）：结构化通道只认这类**精确匹配**。
    # 早期版本用"字符片段重合"判命中，结果所有块都命中 → 通道退化成 KB 顺序，
    # 无论问哪个型号都把库里第一个产品顶到第一。
    model_tokens = set(re.findall(r"[a-z]{1,3}-[a-z0-9]{2,6}", q))
    for c in chunks:
        expired, _ = time_status(c, today)
        if expired:
            continue
        ids_lower = {str(v).lower() for v in c.identifiers.values()}
        reasons = []
        if query.sku and query.sku.lower() in ids_lower:
            reasons.append(f"型号精确命中 {query.sku}")
        matched_models = model_tokens & ids_lower
        if matched_models:
            reasons.append("型号命中 " + ",".join(sorted(matched_models)))
        for v in ids_lower:
            if len(v) >= 4 and v in q:
                reasons.append(f"标识精确命中 {v}")
        if query.hs_codes and set(query.hs_codes) & set(c.hs_codes):
            reasons.append(f"HS 编码命中 {','.join(query.hs_codes)}")
        if reasons:
            hits.append(SearchHit(chunk=c, score=1.0, channel="structured", reasons=reasons))
    return hits


def _identity_key(chunk: KBChunk) -> str:
    """去重用的身份键：同一型号的多语版本算同一条，但规格记录单独计。"""
    sku = chunk.identifiers.get("sku")
    return f"{chunk.doc_type}:{sku}" if sku else f"chunk:{chunk.chunk_id}"


def _lexical_hits(chunks, tokens, concepts=None):
    """步骤 5a：词法通道（BM25 + 字段加权）。

    抽成独立函数，避免 retrieve 与 search 各写一遍 BM25 打分 —— 两份实现迟早漂移。
    """
    bm25 = BM25([bm25_doc(c) for c in chunks])
    out = []
    for i, c in enumerate(chunks):
        sc = bm25.score(tokens, i)
        if sc <= 0:
            continue
        h = SearchHit(chunk=c, score=sc, channel="lexical",
                      reasons=[f"词法命中 score={sc:.3f}"])
        if concepts:
            hit_concepts = [x for x in concepts if x in SURFACE_TO_CONCEPT.values()]
            if hit_concepts:
                h.reasons.append("术语映射：" + ",".join(sorted(set(hit_concepts))))
        out.append(h)
    out.sort(key=lambda h: h.score, reverse=True)
    return out

def rrf_merge(channels: List[List[SearchHit]], k: int = 60,
              k_by_channel: Optional[List[int]] = None) -> List[SearchHit]:
    """步骤 6：RRF 融合 —— 排名倒数加权，跨通道稳定。

    不同通道用不同的 k：**精确匹配通道的 k 更小**（默认 10），于是"型号/证书号精确命中"
    能压过词法通道的长尾排名 —— 这是检索里"结构化优先"的常规做法。
    """
    scores: Dict[str, float] = {}
    best: Dict[str, SearchHit] = {}
    reasons: Dict[str, List[str]] = {}
    channels_seen: Dict[str, int] = {}
    chan_map: Dict[str, List[str]] = {}
    for ci, ch in enumerate(channels):
        kk = k_by_channel[ci] if k_by_channel and ci < len(k_by_channel) else k
        for rank, hit in enumerate(ch, start=1):
            cid = hit.chunk.chunk_id
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (kk + rank)
            if hit.channel not in chan_map.setdefault(cid, []):
                chan_map[cid].append(hit.channel)
            reasons.setdefault(cid, []).extend(hit.reasons)
            channels_seen[cid] = channels_seen.get(cid, 0) + 1
            prev = best.get(cid)
            if prev is None or hit.score > prev.score:
                best[cid] = hit
    out = []
    for cid, sc in scores.items():
        h = best[cid].model_copy(deep=True)
        h.score = round(sc, 6)
        h.reasons = reasons.get(cid, [])
        h.channels = chan_map.get(cid, [])
        if len(h.channels) > 1:
            h.reasons.append("多通道命中")
        out.append(h)
    out.sort(key=lambda x: x.score, reverse=True)
    return out


class RetrievalPipeline:
    """检索管线：喂入块集合，返回带引用的命中列表。"""

    def __init__(self, chunks: List[KBChunk], kb_version: str = "") -> None:
        self.chunks = [c for c in chunks if not kb_version or c.kb_version == kb_version]
        self.kb_version = kb_version

    # ---- 步骤 1：归一 + 分词
    def query_tokens(self, text: str) -> List[str]:
        return tokenize(text)

    # ---- 步骤 2/3：术语映射 + 扩展
    def enrich(self, text: str) -> Tuple[List[str], List[str]]:
        concepts = map_terms(text)
        return concepts, expand_tokens(text, concepts)

    # ---- 步骤 4：过滤
    def filter(self, query: SearchQuery) -> Tuple[List[KBChunk], List[KBChunk]]:
        """返回 (可用块, 已过期被剔除的块)。"""
        today = query.today or date.today()
        kept, dropped = [], []
        for c in filter_by_attributes(self.chunks, query):
            expired, _ = time_status(c, today)
            if expired:
                dropped.append(c)
            else:
                kept.append(c)
        return kept, dropped

    # ---- 步骤 5：双通道召回
    def retrieve(self, query: SearchQuery, tokens: List[str]) -> Dict[str, List[SearchHit]]:
        # 与 search() 同口径：属性过滤对两个通道同样生效
        kept, _ = self.filter(query)
        scope = kept
        lexical = _lexical_hits(scope, tokens)
        return {"lexical": lexical[: max(query.top_k * 3, 10)],
                "structured": _structured_hits(scope, query)}

    # ---- 主流程
    def search(self, query: SearchQuery, dense_hits: Optional[List[SearchHit]] = None) -> List[SearchHit]:
        """主检索入口。

        三个通道（PRD 9.3）：
          - 词法（BM25 + 字段加权）：k=60
          - 结构化（型号/证书号/条款号精确命中）：k=10（**最高权重**）
          - 稠密（向量语义）：k=100（**最低权重**，只是召回辅助）

        为什么稠密通道权重最低：实测显示**向量检索分不清型号**（西语问 X-200，X-300 的向量排第一），
        若给它大权重，会出现"买家问 X-200 却报 X-300 的价格"——这类错对买家不可接受。
        型号识别的唯一权威是**结构化通道**。
        """
        today = query.today or date.today()
        concepts, tokens = self.enrich(query.text)
        kept, dropped = self.filter(query)
        # 属性过滤是**硬约束**，两个通道一视同仁（R11-01 / R11-02）：
        # 过期、别国、HS 不对的块都不参与召回。
        # 过滤后为空就返回空 —— 不能"那就不滤了"：那等于让 HS 编码不匹配的产品
        # 照样被检索出来、报上价格。
        if not kept:
            return []
        scope = kept
        lexical = _lexical_hits(scope, tokens, concepts)[: max(query.top_k * 3, 10)]
        structured = _structured_hits(scope, query)
        # 精确匹配通道权重更高（k=10 vs 词法 k=60）
        # 精确命中压过词法长尾；稠密通道权重最低（只是召回辅助）
        channels = [lexical, structured]
        k_by_channel = [60, 10]
        if dense_hits:
            channels.append(dense_hits)
            k_by_channel.append(100)
        merged = rrf_merge(channels, k_by_channel=k_by_channel)
        # 步骤 7：引用装配 + 时效标注
        #   过期块直接丢弃（不得进入结果、更不得被引用）；
        #   尚未生效的条款保留但标记（可见，但不得当作"当前要求"输出）。
        out: List[SearchHit] = []
        for h in merged:
            expired, not_yet = time_status(h.chunk, today)
            if expired:
                continue
            h.expired = False
            h.not_yet_effective = not_yet
            if h.chunk.identifiers:
                h.reasons.append("引用标识：" + ",".join(f"{k}={v}" for k, v in h.chunk.identifiers.items()))
            out.append(h)
        # 同一产品的多语版本只留一条（提问语种优先）：否则一次召回会把四个语言
        # 版本都顶上来，把证书与法规挤出结果（拆 4 语后的副作用）。
        best: Dict[str, SearchHit] = {}
        for h in out:
            key = _identity_key(h.chunk)
            cur = best.get(key)
            if cur is None or (h.chunk.lang == query.lang and cur.chunk.lang != query.lang):
                best[key] = h
        ordered = sorted(best.values(), key=lambda h: h.score, reverse=True)
        return ordered[: query.top_k]


def bm25_doc(chunk: KBChunk) -> List[str]:
    """块的可检索词：**字段加权** —— 标题与标识重复 3 次，正文 1 次。

    多语种知识块的正文里四语并列，若等权则任意两个产品得分几乎一样（区分度差）；
    标题/标识是判别性信息，加权后"问的是哪个型号"才能拉开差距。
    """
    title_toks = tokenize(chunk.title)
    id_toks = [t for v in chunk.identifiers.values() for t in tokenize(str(v))]
    body_toks = tokenize(chunk.text)
    tag_toks = tokenize(" ".join(chunk.tags))
    return title_toks * 3 + id_toks * 3 + body_toks + tag_toks
