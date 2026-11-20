# -*- coding: utf-8 -*-
"""S3 验证脚本 —— 检索层（多语种召回 / 术语映射 / 合规时效 / 引用装配 / 库版本留痕）。

用法
    python scripts/verify_s3.py                 # SQLite
    python scripts/verify_s3.py --dsn <DSN>     # PostgreSQL

本轮验证什么
    知识库落库与标识完整性、多语种跨库召回、术语表映射、型号精确匹配、
    **过期证书/废止条款不得进入结果或引用**、未生效条款只标记不作要求、
    目的国过滤、引用可回指、**草稿数据确实来自知识库**（改库价 → 草稿价跟着变）、库版本留痕。
本轮不验证什么
    真实向量语义检索（S4 接 BGE-M3 + pgvector）；本轮是**词法 + 结构化双通道**，
    因此只能证明"检索管线与合规时效可用"，不能宣称语义召回能力（PRD 9.5 两层口径）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def _setup_stdout() -> None:
    """自适应输出编码：tty 用控制台原生编码，非 tty 用 UTF-8，无法编码的字符用 replace 兜底。"""
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
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradedesk.api.app import create_app                    # noqa: E402
from tradedesk.api.deps import AppContext                   # noqa: E402
from tradedesk.data.kb_seed import KB_VERSION, load_kb      # noqa: E402
from tradedesk.retrieval.base import SearchQuery           # noqa: E402
from tradedesk.retrieval.pipeline import map_terms, TERM_TABLE  # noqa: E402
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


def new_store(dsn: str | None):
    if dsn:
        return PostgresStateStore(dsn)
    return SqliteStateStore(str(ROOT / "data" / "verify_s3.db"))


def scenario_1_kb(store) -> None:
    section("场景 1 · 知识库落库与标识完整性")
    chunks = store.list_kb_chunks(KB_VERSION)
    check("知识库已落库（>=10 块）", len(chunks) >= 10, f"{len(chunks)} 块")
    types = {}
    for c in chunks:
        types[c.doc_type] = types.get(c.doc_type, 0) + 1
    check("四类知识齐全（产品/证书/法规/名单/术语）",
          set(types) >= {"产品条目", "证书", "法规条款", "名单条目", "术语表"}, str(types))
    no_id = [c.chunk_id for c in chunks if not c.identifiers]
    check("每块都有可回指标识（无标识块 = 0）", not no_id, str(no_id))
    check("每块都有库版本", all(c.kb_version == KB_VERSION for c in chunks))
    # 数值的唯一真源：每个型号恰好一条结构化规格记录
    spec = [c for c in chunks if c.doc_type == "产品规格"]
    skus = {c.identifiers.get("sku") for c in spec}
    per_sku_spec = {}
    for c in spec:
        per_sku_spec.setdefault(c.identifiers.get("sku"), []).append(c.chunk_id)
    check("每个型号恰好一条结构化规格记录（数值唯一真源）",
          all(len(v) == 1 for v in per_sku_spec.values()) and len(per_sku_spec) >= 3,
          str(per_sku_spec))
    # 数值不得进文本块（PRD 9.3：规格参数走属性过滤）
    numeric_in_text = []
    for c in chunks:
        t = c.text
        for key in ("price_low", "lead_time_days", "moq"):
            v = c.extra.get(key)
            if v and str(v) in t:
                numeric_in_text.append(f"{c.chunk_id}:{key}")
    check("产品属性数值不进文本块", not numeric_in_text, str(numeric_in_text))
    # 同一型号 4 语版本分别入库（FR-KB-01）
    per_sku_lang = {}
    for c in chunks:
        if c.doc_type == "产品条目":
            per_sku_lang.setdefault(c.identifiers.get("sku"), []).append(c.lang)
    multi = {k: sorted(set(v)) for k, v in per_sku_lang.items() if len(set(v)) >= 4}
    check("同一型号的 4 语版本分别入库", bool(multi), str(per_sku_lang))


def scenario_2_terms() -> None:
    section("场景 2 · 术语表映射（多语种 → 标准概念）")
    check("术语表概念数 >= 10", len(TERM_TABLE) >= 10, str(len(TERM_TABLE)))
    cases = [
        ("¿Cuál es la cantidad mínima de pedido?", "moq"),
        ("What is the lead time?", "lead_time"),
        ("请报一下价格", "price"),
        ("ما هو موعد التسليم؟", "lead_time"),
    ]
    for text, concept in cases:
        got = map_terms(text)
        check(f"术语映射：{text[:18]} → {concept}", concept in got, str(got))


def scenario_3_crosslingual(store) -> None:
    section("场景 3 · 跨语种召回（西/阿/中/英提问命中中文产品库）")
    from tradedesk.retrieval.store_retriever import StoreRetriever
    r = StoreRetriever(store, KB_VERSION)
    cases = [
        ("¿Cuánto cuesta el X-200 FOB Shenzhen? precio y plazo de entrega", "es", "X-200"),
        ("Please quote M-77 lifting clamp FOB Ningbo, price and lead time", "en", "M-77"),
        ("ما هو موعد التسليم والسعر للطراز X-200؟", "ar", "X-200"),
        ("X-200 的材质和安全载荷是多少？", "zh", "X-200"),
    ]
    for text, lang, expect in cases:
        hits = r.search(SearchQuery(text=text, lang=lang, top_k=4))
        ids = [h.chunk.identifiers.get("sku") for h in hits]
        check(f"{lang} 提问命中 {expect}", expect in ids, str(ids))


def scenario_4_structured_priority(store) -> None:
    section("场景 4 · 结构化通道优先（点名型号 → 该型号排第一）")
    from tradedesk.retrieval.store_retriever import StoreRetriever
    r = StoreRetriever(store, KB_VERSION)
    hits = r.search(SearchQuery(text="Please quote M-77 FOB Ningbo", lang="en", top_k=4))
    check("M-77 排第一", bool(hits) and hits[0].chunk.identifiers.get("sku") == "M-77",
          str([h.chunk.identifiers.get("sku") for h in hits]))
    check("命中理由含型号匹配", bool(hits) and any("型号" in r for r in hits[0].reasons),
          str(hits[0].reasons) if hits else "")


def scenario_5_expiry(store) -> None:
    section("场景 5 · 合规时效（过期证书/废止条款不得进入结果）")
    from tradedesk.retrieval.store_retriever import StoreRetriever
    r = StoreRetriever(store, KB_VERSION)
    hits = r.search(SearchQuery(text="M-77 test certificate TC-M77-118 SGS", lang="en", top_k=8))
    ids = [h.chunk.identifiers.get("cert_no") for h in hits]
    check("过期证书 TC-M77-118 不在结果里", "TC-M77-118" not in ids, str(ids))
    hits2 = r.search(SearchQuery(text="X-190 CE certificate CERT-CE-1890", lang="en", top_k=8))
    ids2 = [h.chunk.identifiers.get("cert_no") for h in hits2]
    check("已废止证书 CERT-CE-1890 不在结果里", "CERT-CE-1890" not in ids2, str(ids2))
    hits3 = r.search(SearchQuery(text="CPSC warning label proposal", lang="en", buyer_country="US", top_k=8))
    us = [h for h in hits3 if h.chunk.identifiers.get("clause_no") == "REG-US-204"]
    check("未生效条款可见但被标记", bool(us) and us[0].not_yet_effective is True,
          str([(h.chunk.doc_id, h.not_yet_effective) for h in hits3]))


def scenario_6_country_filter(store) -> None:
    section("场景 6 · 目的国过滤（法规按买家国别）")
    from tradedesk.retrieval.store_retriever import StoreRetriever
    r = StoreRetriever(store, KB_VERSION)
    br = [h.chunk.identifiers.get("clause_no") for h in
          r.search(SearchQuery(text="清关认证要求", lang="zh", buyer_country="BR", top_k=8))]
    check("巴西买家命中 REG-BR-077", "REG-BR-077" in br, str(br))
    check("巴西买家不命中阿联酋条款 REG-AE-099", "REG-AE-099" not in br, str(br))


def scenario_7_citations(store) -> None:
    section("场景 7 · 引用装配与可回指")
    from fastapi.testclient import TestClient
    app = create_app(AppContext(store=store))
    client = TestClient(app)
    d = client.post("/inbound", json={
        "buyer_id": "BY-S3-A", "message": "Quote 5000 pcs of X-200 FOB Shenzhen with CE certificate please.",
        "language": "en", "company": "Cite Co", "country": "BR"}).json()
    check("询盘产出草稿", d.get("status") == "draft_ready", str(d.get("status")))
    cites = d["draft"]["citations"]
    check("草稿带引用", len(cites) >= 2, f"{len(cites)} 条")
    check("每条引用都有标识", all(c.get("citation_id") for c in cites))
    known = {c.identifiers.get("cert_no") or c.identifiers.get("sku") or c.identifiers.get("clause_no")
              or c.identifiers.get("term_id") or c.doc_id for c in store.list_kb_chunks(KB_VERSION)}
    unknown = [c["citation_id"] for c in cites if c["citation_id"] not in known]
    check("每条引用都能在知识库里查到（可回指）", not unknown, str(unknown))
    check("引用带库版本留痕", all("库版本" in c.get("note", "") for c in cites),
          str([c.get("note", "") for c in cites]))
    check("过期证书不在引用里", "TC-M77-118" not in [c["citation_id"] for c in cites])
    check("未生效条款不在引用里", "REG-US-204" not in [c["citation_id"] for c in cites])
    return client


def scenario_8_data_from_kb(store) -> None:
    section("场景 8 · 草稿数据确实来自知识库（改库 → 草稿跟着变）")
    from tradedesk.domain.models import KBChunk
    from fastapi.testclient import TestClient
    app = create_app(AppContext(store=store))
    client = TestClient(app)
    before = client.post("/inbound", json={
        "buyer_id": "BY-S3-B", "message": "Quote 100 pcs of X-300 electric pallet truck price please.",
        "language": "en", "company": "Price Co", "country": "US"}).json()
    body_before = before["draft"]["body"] if before.get("draft") else ""
    check("草稿价格取自知识库", "12.3" in body_before, body_before[:120])

    # 改知识库里的价格，再来一单。**同一个进程**内就要生效（R11-07）
    chunk = next(c for c in store.list_kb_chunks(KB_VERSION) if c.identifiers.get("sku") == "X-300")
    extra = dict(chunk.extra); extra["price_low"] = 9.99
    chunk.extra = extra
    store.upsert_kb_chunk(chunk)
    after = client.post("/inbound", json={
        "buyer_id": "BY-S3-C", "message": "Quote 100 pcs of X-300 electric pallet truck price please.",
        "language": "en", "company": "Price Co2", "country": "US"}).json()
    body_after = after["draft"]["body"] if after.get("draft") else ""
    check("同一进程内改库后草稿价格随之变化",
          "9.99" in body_after and "12.3" not in body_after, body_after[:120])


def scenario_9_api(store) -> None:

    section("场景 9 · /kb/search 接口")
    from fastapi.testclient import TestClient
    app = create_app(AppContext(store=store))
    client = TestClient(app)
    r = client.get("/kb/search", params={"q": "¿plazo de entrega del X-200?", "lang": "es", "country": "BR"})
    check("/kb/search 返回 200", r.status_code == 200)
    body = r.json()
    check("返回命中列表", isinstance(body.get("hits"), list) and len(body["hits"]) >= 1,
          str(len(body.get("hits", []))))
    h = body["hits"][0]
    for key in ("citation_id", "identifiers", "score", "reasons", "expired", "not_yet_effective"):
        check(f"命中含 {key}", key in h)
    check("返回库版本", body.get("kb_version") == KB_VERSION)




def scenario_10_cert_and_country(store) -> None:
    section("场景 10 · 证书口径与国别未知（R11-10 / R12-01、R12-02 / R12-03）")
    import re
    from fastapi.testclient import TestClient
    from tradedesk.retrieval.store_retriever import StoreRetriever

    app = create_app(AppContext(store=store))
    client = TestClient(app)

    def ask(buyer, country, message, lang="en"):
        payload = {"buyer_id": buyer, "message": message, "language": lang,
                   "company": buyer + " Co", "country": country}
        return client.post("/inbound", json=payload).json()

    # R12-02：证书不按买家国别过滤 —— BR 买家问 X-200 仍要拿到 X-200 的 CE 证书
    d = ask("BY-C1", "BR", "Quote 5000 pcs of X-200 FOB Shenzhen, do you have CE certification?")
    body = d["draft"]["body"]
    check("BR 买家仍拿到本型号的 CE 证书", "CERT-CE-2041" in body, body[:120])
    check("体系证书不得被说成型号的认证", "CERT-ISO-9001" not in body, body[:160])

    # R12-01：DE 买家问 M-77，正文里的证书必须覆盖 M-77
    d2 = ask("BY-C2", "DE", "Quote 500 pcs of M-77 lifting clamp, do you have CE certificate?")
    body2 = d2["draft"]["body"]
    named = re.findall(r"(?:CERT|TC)-[A-Z0-9-]+", body2)
    check("M-77 的正文不出现别型号的证书", "CERT-CE-2041" not in named, str(named))
    check("M-77 的正文只出现它自己的证书", set(named) <= {"CERT-ISO-9001"}, str(named))
    cites2 = [c["citation_id"] for c in d2["draft"]["citations"]]
    check("正文提到的证书都在引用列表里", all(x in cites2 for x in named),
          f"{named} vs {cites2}")

    # R12-03：国别未知 → 转人工 + 不引用国别专属法规
    d3 = ask("BY-C3", "", "Quote 5000 pcs of X-200 FOB Shenzhen, what certifications do we need?")
    cites3 = [c["citation_id"] for c in d3["draft"]["citations"]]
    check("国别未知时引用里没有国别专属法规",
          not [x for x in cites3 if x.startswith("REG-")], str(cites3))
    check("国别未知时草稿标为需人工确认",
          bool(d3["draft"]["needs_human_confirmation"]),
          str(d3["draft"].get("needs_human_confirmation")))

    # 未点名型号 → 不猜型号与价格（宁可追问）
    d4 = ask("BY-C4", "US", "Send me your best price for the manual hydraulic hand truck.")
    body4 = d4["draft"]["body"] if d4.get("draft") else ""
    check("未点名型号时不报价格", "USD" not in body4, body4[:100])
    check("未点名型号时没有引用", not (d4.get("draft") or {}).get("citations"),
          str((d4.get("draft") or {}).get("citations")))

    # 属性过滤清空不得回退全集（R11-02）
    r = StoreRetriever(store, KB_VERSION)
    hs_hits = r.search(SearchQuery(text="M-77 lifting clamp price", lang="en",
                                    buyer_country="BR", hs_codes=["9999.99"], top_k=8))
    check("HS 不匹配的型号不得被检索出来",
          "M-77" not in [h.chunk.identifiers.get("sku") for h in hs_hits],
          str([h.chunk.identifiers.get("sku") for h in hs_hits]))

    # 结构化通道也不得返回别国法规（R11-01）
    ae = r.search(SearchQuery(text="REG-AE-099 final user screening requirement", lang="en",
                              buyer_country="BR", top_k=8))
    check("结构化通道也不返回别国法规",
          "REG-AE-099" not in [h.chunk.identifiers.get("clause_no") for h in ae],
          str([h.chunk.identifiers.get("clause_no") for h in ae]))

def main() -> int:
    ap = argparse.ArgumentParser(description="S3 验证脚本")
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    print("TradeDesk-AI · S3 验证 —— 检索层（多语种召回 / 合规时效 / 引用装配）")
    print("提示：本轮是词法 + 结构化双通道，**不是向量语义检索**（S4 才接 BGE-M3）")
    print("提示：名单为结构化演示数据，非真实名单")

    store = new_store(args.dsn)
    try:
        if hasattr(store, "reset"):
            store.reset()
        load_kb(store, KB_VERSION)
        scenario_1_kb(store)
        scenario_2_terms()
        scenario_3_crosslingual(store)
        scenario_4_structured_priority(store)
        scenario_5_expiry(store)
        scenario_6_country_filter(store)
        scenario_7_citations(store)
        scenario_8_data_from_kb(store)
        scenario_9_api(store)
        scenario_10_cert_and_country(store)
    finally:
        store.close()

    passed = sum(1 for r in RESULTS if r["ok"])
    total = len(RESULTS)
    print("\n" + "=" * 66)
    print(f"结果：{passed}/{total} 项通过")
    if passed < total:
        print("失败项：")
        for r in RESULTS:
            if not r["ok"]:
                print(f"  - {r['name']} {r['detail']}")
    print("本轮未覆盖：向量语义检索（S4）、真实模型草稿（S5）、前端联调（S6）。")
    print("名单数据为结构化演示数据，非真实名单。")
    print("=" * 66)
    if args.json:
        print(json.dumps({"passed": passed, "total": total, "results": RESULTS}, ensure_ascii=False, indent=2))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())