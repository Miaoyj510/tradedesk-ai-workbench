# -*- coding: utf-8 -*-
"""检索层单元测试：分词 / 术语映射 / 时效判定 / 双通道 / RRF / 引用装配。"""
from datetime import date, timedelta, datetime, timezone

import pytest

from tradedesk.data.kb_seed import KB_VERSION, build_demo_chunks, load_kb
from tradedesk.domain.models import KBChunk
from tradedesk.retrieval.base import SearchQuery
from tradedesk.retrieval.lexical import normalize_text, tokenize
from tradedesk.retrieval.pipeline import RetrievalPipeline, map_terms, time_status
from tradedesk.retrieval.store_retriever import StoreRetriever
from tradedesk.store.sqlite_store import SqliteStateStore

TODAY = date(2026, 11, 19)


def _pipeline():
    return RetrievalPipeline(build_demo_chunks(KB_VERSION), KB_VERSION)


# ---------------------------- 分词 ----------------------------
def test_normalize_arabic_variants():
    assert normalize_text("الأحد") != "الأحد"      # ا/أ 归一
    assert "ا" in normalize_text("أ")


def test_tokenize_mixes_scripts():
    toks = tokenize("X-200 precio FOB مورد")
    assert "x" in toks and "200" in toks
    assert any("precio" in t for t in toks)


def test_tokenize_chinese_bigrams():
    toks = tokenize("交期")
    assert "交" in toks and "期" in toks and "交期" in toks


# ---------------------------- 术语映射 ----------------------------
@pytest.mark.parametrize("text,concept", [
    ("¿Cuál es la cantidad mínima de pedido?", "moq"),
    ("What is the lead time?", "lead_time"),
    ("请报一下价格", "price"),
    ("ما هو موعد التسليم؟", "lead_time"),
    ("هل يمكن تأكيد السعر؟", "price"),
])
def test_term_mapping(text, concept):
    assert concept in map_terms(text)


def test_arabic_definite_article():
    assert "price" in map_terms("هل يمكن تأكيد السعر؟")


# ---------------------------- 时效 ----------------------------
def test_expired_certificate_is_expired():
    c = next(x for x in build_demo_chunks() if x.identifiers.get("cert_no") == "TC-M77-118")
    expired, not_yet = time_status(c, TODAY)
    assert expired is True and not_yet is False


def test_obsolete_certificate_is_expired():
    c = next(x for x in build_demo_chunks() if x.identifiers.get("cert_no") == "CERT-CE-1890")
    assert time_status(c, TODAY)[0] is True


def test_not_yet_effective_regulation():
    c = next(x for x in build_demo_chunks() if x.identifiers.get("clause_no") == "REG-US-204")
    expired, not_yet = time_status(c, TODAY)
    assert expired is False and not_yet is True


def test_effective_regulation_is_current():
    c = next(x for x in build_demo_chunks() if x.identifiers.get("clause_no") == "REG-EU-112")
    assert time_status(c, TODAY) == (False, False)


# ---------------------------- 检索 ----------------------------
def test_expired_chunk_never_returned():
    p = _pipeline()
    hits = p.search(SearchQuery(text="TC-M77-118 test certificate SGS", lang="en", today=TODAY, top_k=8))
    ids = [h.chunk.identifiers.get("cert_no") for h in hits]
    assert "TC-M77-118" not in ids
    assert "CERT-CE-1890" not in ids


def test_named_sku_ranks_first():
    p = _pipeline()
    hits = p.search(SearchQuery(text="Please quote M-77 FOB Ningbo", lang="en", today=TODAY, top_k=4))
    assert hits and hits[0].chunk.identifiers.get("sku") == "M-77"


def test_cross_language_hit():
    """西语提问 → 命中中文产品库里的型号（PRD 9.4 难点①）。"""
    p = _pipeline()
    hits = p.search(SearchQuery(text="¿Cuánto cuesta el X-200 y el plazo de entrega?",
                                lang="es", today=TODAY, top_k=4))
    assert "X-200" in [h.chunk.identifiers.get("sku") for h in hits]


def test_country_filter():
    p = _pipeline()
    hits = p.search(SearchQuery(text="清关认证要求", lang="zh", buyer_country="BR", today=TODAY, top_k=8))
    ids = [h.chunk.identifiers.get("clause_no") for h in hits]
    assert "REG-BR-077" in ids
    assert "REG-AE-099" not in ids


def test_not_yet_effective_is_flagged_not_hidden():
    p = _pipeline()
    hits = p.search(SearchQuery(text="CPSC warning label proposal", lang="en",
                                buyer_country="US", today=TODAY, top_k=8))
    us = [h for h in hits if h.chunk.identifiers.get("clause_no") == "REG-US-204"]
    assert us and us[0].not_yet_effective is True


def test_hits_are_unique():
    p = _pipeline()
    hits = p.search(SearchQuery(text="X-200 precio FOB Shenzhen entrega", lang="es",
                                today=TODAY, top_k=8))
    ids = [h.chunk.chunk_id for h in hits]
    assert len(ids) == len(set(ids))


def test_rrf_dedupes_across_channels():
    p = _pipeline()
    hits = p.search(SearchQuery(text="X-200", lang="en", today=TODAY, top_k=8))
    ids = [h.chunk.chunk_id for h in hits]
    assert len(ids) == len(set(ids))


# ---------------------------- 存储与入库 ----------------------------
def test_kb_load_and_read(tmpdir_local):
    store = SqliteStateStore(str(tmpdir_local / "kb.db"))
    n = load_kb(store, KB_VERSION)
    assert n >= 10
    chunks = store.list_kb_chunks(KB_VERSION)
    assert len(chunks) == n
    assert all(c.identifiers for c in chunks)
    store.close()


def test_store_retriever_uses_kb(tmpdir_local):
    store = SqliteStateStore(str(tmpdir_local / "kb2.db"))
    load_kb(store, KB_VERSION)
    r = StoreRetriever(store, KB_VERSION)
    hits = r.search(SearchQuery(text="X-200 precio", lang="es", today=TODAY, top_k=3))
    assert hits and hits[0].chunk.identifiers.get("sku") == "X-200"
    store.close()
