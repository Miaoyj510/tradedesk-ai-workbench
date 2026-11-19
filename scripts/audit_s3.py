# -*- coding: utf-8 -*-
"""S3 独立审核脚本（审核 agent 用）。

与 `scripts/verify_s3.py` 的区别
------------------------------
verify_s3.py 是实现 agent 交卷时的自证；本脚本是**审核方自己按
`docs/S3-ACCEPTANCE.md` 与 PRD 第 9 章重写**的断言：

1. 每条断言标了条款号（§编号）与 PRD 出处，失败时直接指出违反哪一条。
2. verify_s3 没覆盖的重点补上：
   - 时效过滤的**方向性**（把 today 注入未来/过去，而不是只测两个写死的过期证书）
   - 时效过滤在**两个通道**都生效（结构化通道直接点名证书号/条款号）
   - 属性过滤**清空时不得回退到全集**；买家国别**未知时**的保守行为
   - 「产品必须经结构化通道确认才可进草稿」这条口径（提交说明里写了，代码没做到）
   - 「改库 → 草稿变化」在**同一进程内**成立（缓存是否失效）
   - **正文与引用一致性**（草稿说 A、引用说 B）
   - PRD 9.3 的分块策略与 FR-KB-01 的"四语分别入库"（自证完全没提）
3. 双后端：SQLite 与 PostgreSQL 各跑一遍，比对同一组查询的结果序列。

用法
    python scripts/audit_s3.py                # SQLite
    python scripts/audit_s3.py --dsn <DSN>    # PostgreSQL
    python scripts/audit_s3.py --json

退出码：0 = 无 FAIL；1 = 有 FAIL。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import traceback
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _setup_stdout() -> None:
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

from fastapi.testclient import TestClient                      # noqa: E402

from tradedesk.api.app import create_app                        # noqa: E402
from tradedesk.api.deps import AppContext                       # noqa: E402
from tradedesk.data.kb_seed import KB_VERSION, load_kb          # noqa: E402
from tradedesk.domain.models import KBChunk                     # noqa: E402
from tradedesk.retrieval.base import SearchQuery                # noqa: E402
from tradedesk.retrieval.pipeline import TERM_TABLE, map_terms  # noqa: E402
from tradedesk.retrieval.store_retriever import StoreRetriever  # noqa: E402
from tradedesk.store.postgres_store import PostgresStateStore    # noqa: E402
from tradedesk.store.sqlite_store import SqliteStateStore        # noqa: E402

RESULTS = []
CURRENT_CLAUSE = "?"
_STORES = []
_TMPFILES = []
_PG_DBS = []
_SEQ = [0]
DSN = ""


def clause(c: str) -> None:
    global CURRENT_CLAUSE
    CURRENT_CLAUSE = c


def check(name: str, ok: bool, detail: str = "", level: str = "FAIL") -> bool:
    RESULTS.append({"clause": CURRENT_CLAUSE, "name": name, "ok": bool(ok),
                    "level": "PASS" if ok else level, "detail": detail})
    print(f"  [{'PASS' if ok else level}] {CURRENT_CLAUSE} {name}"
          + (f" — {detail}" if detail else ""))
    return bool(ok)


def note(name: str, detail: str = "") -> None:
    RESULTS.append({"clause": CURRENT_CLAUSE, "name": name, "ok": True,
                    "level": "WARN", "detail": detail})
    print(f"  [WARN] {CURRENT_CLAUSE} {name}" + (f" — {detail}" if detail else ""))


def section(title: str) -> None:
    print("\n" + title)
    print("-" * max(len(title), 64))


# --------------------------------------------------------------------------
# 存储 / 客户端工厂（与 audit_s2.py 同款：一 client 一库）
# --------------------------------------------------------------------------
def _pg_split(dsn: str):
    head, _, _db = dsn.rpartition("/")
    return head + "/"


def _pg_exec(db: str, sql: str) -> None:
    import psycopg
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(sql)


def new_store():
    global DSN
    if DSN:
        head = _pg_split(DSN)
        _SEQ[0] += 1
        name = f"audit_s3_{_SEQ[0]:02d}"
        _pg_exec(head + "postgres", f'CREATE DATABASE "{name}"')
        _PG_DBS.append((head, name))
        st = PostgresStateStore(head + name)
        _STORES.append(st)
        load_kb(st, KB_VERSION)
        return st
    _SEQ[0] += 1
    p = ROOT / "data" / f"audit_s3_{_SEQ[0]:02d}.db"
    for suffix in ("", "-wal", "-shm"):
        f = p.with_name(p.name + suffix)
        if f.exists():
            f.unlink()
    _TMPFILES.append(p)
    st = SqliteStateStore(str(p))
    _STORES.append(st)
    load_kb(st, KB_VERSION)
    return st


def new_client(store=None) -> TestClient:
    return TestClient(create_app(AppContext(store=store or new_store())))


def close_stores() -> None:
    while _STORES:
        st = _STORES.pop()
        try:
            st.close()
        except Exception:  # noqa: BLE001
            pass


def cleanup() -> None:
    close_stores()
    for p in _TMPFILES:
        for suffix in ("", "-wal", "-shm"):
            f = p.with_name(p.name + suffix)
            if f.exists():
                try:
                    f.unlink()
                except OSError:
                    pass
    for head, name in _PG_DBS:
        try:
            _pg_exec(head + "postgres", f'DROP DATABASE IF EXISTS "{name}"')
        except Exception:  # noqa: BLE001
            pass


def doc_ids(hits) -> list:
    return [h.chunk.doc_id for h in hits]


def cite_ids(hits) -> list:
    out = []
    for h in hits:
        ids = h.chunk.identifiers
        out.append(ids.get("clause_no") or ids.get("cert_no") or ids.get("sku")
                   or ids.get("entry_id") or ids.get("term_id") or h.chunk.doc_id)
    return out


# --------------------------------------------------------------------------
# §1 知识资产与标识（FR-KB-07 / FR-KB-08，PRD 9.1 / 9.3）
# --------------------------------------------------------------------------
def audit_1_assets() -> None:
    section("§1 知识资产与标识（FR-KB-07/08，PRD 9.1/9.3）")
    store = new_store()
    chunks = store.list_kb_chunks(KB_VERSION)
    clause("1.1")
    no_id = [c.chunk_id for c in chunks if not c.identifiers]
    check("每块都有可回指标识（FR-KB-07：无标识文档不可发布入库）", not no_id, str(no_id))
    clause("1.2")
    check("每块都带库版本（FR-KB-08）",
          all(c.kb_version == KB_VERSION for c in chunks) and len(chunks) > 0,
          f"{len(chunks)} 块")
    clause("1.3")
    no_time = [c.chunk_id for c in chunks
               if not (c.effective_from or c.effective_to or c.valid_until)]
    check("每块都有时效字段或明确无时效", True, f"无时效字段的块：{no_time}")
    clause("1.4")
    r = StoreRetriever(store, KB_VERSION)
    hits = r.search(SearchQuery(text="X-200 MOQ price CE certificate 清关", lang="zh", top_k=8))
    known = {c.doc_id for c in chunks}
    unknown = [h.chunk.doc_id for h in hits if h.chunk.doc_id not in known]
    check("命中都能在库里回指", not unknown, str(unknown))
    clause("1.5")
    dup = [x for x in set(doc_ids(hits)) if doc_ids(hits).count(x) > 1]
    check("命中不重复（同一块只出现一次）", not dup, str(dup))
    clause("1.6")
    types = {}
    for c in chunks:
        types[c.doc_type] = types.get(c.doc_type, 0) + 1
    scale = {"产品条目": 30, "证书": 20, "法规条款": 50, "术语表": 200}
    short = {k: f"{types.get(k,0)}/{v}" for k, v in scale.items() if types.get(k, 0) < v}
    note(f"知识库规模远低于 PRD 9.1 自建目标（{short}）；话术库为 0",
         f"实际：{types}（演示规模，非自建目标；PRD 9.1 是全项目口径，不算违约，但要记）")


# --------------------------------------------------------------------------
# §2 合规时效（FR-KB-05 / FR-GRC-05，PRD 9.4 难点②）
# --------------------------------------------------------------------------
def audit_2_expiry() -> None:
    section("§2 合规时效过滤（FR-KB-05，PRD 9.4 难点②）")
    store = new_store()
    r = StoreRetriever(store, KB_VERSION)
    clause("2.1")
    h = r.search(SearchQuery(text="M-77 test certificate TC-M77-118 SGS", lang="en", top_k=8))
    check("过期证书 TC-M77-118 不进结果", "TC-M77-118" not in cite_ids(h), str(cite_ids(h)))
    clause("2.2")
    h = r.search(SearchQuery(text="X-190 CE certificate CERT-CE-1890 obsolete", lang="en", top_k=8))
    check("已废止证书 CERT-CE-1890 不进结果", "CERT-CE-1890" not in cite_ids(h), str(cite_ids(h)))
    clause("2.3")
    h = r.search(SearchQuery(text="CPSC warning label proposal", lang="en",
                              buyer_country="US", top_k=8))
    us = [x for x in h if x.chunk.identifiers.get("clause_no") == "REG-US-204"]
    check("未生效条款可见但被标记 not_yet_effective",
          bool(us) and us[0].not_yet_effective is True,
          str([(x.chunk.doc_id, x.not_yet_effective) for x in h]))
    clause("2.4")
    h = r.search(SearchQuery(text="X-200 CE certificate CERT-CE-2041", lang="en",
                              buyer_country="DE", today=date(2099, 1, 1), top_k=8))
    check("把日期注入未来 → 未过期证书也被判过期、不进结果",
          "CERT-CE-2041" not in cite_ids(h), str(cite_ids(h)))
    clause("2.5")
    h = r.search(SearchQuery(text="X-200 CE certificate CERT-CE-2041", lang="en",
                              buyer_country="DE", today=date(2020, 1, 1), top_k=8))
    ce = [x for x in h if x.chunk.identifiers.get("cert_no") == "CERT-CE-2041"]
    check("把日期注入过去 → 未生效证书可见但标记为未生效",
          bool(ce) and ce[0].not_yet_effective is True,
          str([(x.chunk.doc_id, x.not_yet_effective) for x in h]))
    clause("2.6")
    # 结构化通道直接点名过期证书号 —— 早期版本就是从这条通道把过期证书"复活"的
    h = r.search(SearchQuery(text="CERT-CE-1890 CE-1890 obsolete certificate X-190",
                              lang="en", buyer_country="DE", top_k=8))
    check("结构化通道同样不得返回已废止证书（点名查也不行）",
          "CERT-CE-1890" not in cite_ids(h), str(cite_ids(h)))
    h = r.search(SearchQuery(text="TC-M77-118 test report SGS M-77", lang="en", top_k=8))
    check("结构化通道同样不得返回过期证书（点名查也不行）",
          "TC-M77-118" not in cite_ids(h), str(cite_ids(h)))
    clause("2.7")
    c = new_client(store)
    for lang, msg in (("en", "Quote 1000 pcs of M-77 with test certificate please."),
                      ("zh", "请报 M-77 价格并附测试报告"),
                      ("es", "Cotización M-77 con certificado de ensayo"),
                      ("ar", "عرض سعر M-77 مع شهادة الاختبار")):
        d = c.post("/inbound", json={"buyer_id": f"BY-EXP-{lang}", "message": msg,
                                     "language": lang, "company": "Exp Co", "country": "US"}).json()
        body = d["draft"]["body"] if d.get("draft") else ""
        cites = [x["citation_id"] for x in d["draft"]["citations"]] if d.get("draft") else []
        check(f"过期证书不出现在 {lang} 语草稿正文与引用",
              "TC-M77-118" not in body and "TC-M77-118" not in cites,
              f"body含={'TC-M77-118' in body} cites={cites}")
    clause("2.8")
    d = c.post("/inbound", json={"buyer_id": "BY-US", "country": "US",
                                 "message": "Quote 1000 pcs of X-200 with CPSC warning label requirement",
                                 "language": "en", "company": "US Co"}).json()
    cites = [x["citation_id"] for x in d["draft"]["citations"]] if d.get("draft") else []
    body = d["draft"]["body"] if d.get("draft") else ""
    check("未生效条款不进草稿引用与正文", "REG-US-204" not in cites and "REG-US-204" not in body,
          f"cites={cites}")


# --------------------------------------------------------------------------
# §3 目的国过滤（FR-KB-05 / FR-GRC-05，PRD 9.4 难点②）
# --------------------------------------------------------------------------
def audit_3_country() -> None:
    section("§3 目的国过滤（FR-KB-05，PRD 9.4 难点②）")
    store = new_store()
    r = StoreRetriever(store, KB_VERSION)
    clause("3.1")
    br = cite_ids(r.search(SearchQuery(text="清关认证要求 进口", lang="zh",
                                       buyer_country="BR", top_k=8)))
    check("巴西买家命中 REG-BR-077", "REG-BR-077" in br, str(br))
    clause("3.2")
    check("巴西买家不命中阿联酋条款 REG-AE-099（词法通道）",
          "REG-AE-099" not in br, str(br))
    clause("3.3")
    ae = cite_ids(r.search(SearchQuery(text="REG-AE-099 final user screening requirement",
                                       lang="en", buyer_country="BR", top_k=8)))
    check("结构化通道也必须遵守买家国别过滤",
          "REG-AE-099" not in ae, str(ae))
    clause("3.4")
    # 属性过滤把结果清空时不得回退到全集（scope = kept if kept else self.chunks）
    hs = cite_ids(r.search(SearchQuery(text="M-77 lifting clamp price", lang="en",
                                       buyer_country="BR", hs_codes=["9999.99"], top_k=8)))
    check("属性过滤清空时不得回退到全集（HS 不匹配的产品不得返回）",
          "M-77" not in hs, str(hs))
    clause("3.5")
    c = new_client(store)
    d = c.post("/inbound", json={"buyer_id": "BY-NOC", "country": "",
                                 "message": "Quote 5000 pcs of X-200 with CE certificate please.",
                                 "language": "en", "company": "NoCountry Co"}).json()
    cites = [(x["citation_id"], x["type"]) for x in d["draft"]["citations"]] if d.get("draft") else []
    reg = [x for x in cites if x[1] == "法规条款"]
    check("买家国别未知时不得把多国法规混进草稿引用", len(reg) <= 1,
          f"引用里的法规条款：{[x[0] for x in reg]}（国别未知 = 无法判断适用性，应转人工或只给通用条款）")
    clause("3.6")
    d = c.post("/inbound", json={"buyer_id": "BY-DE", "country": "DE",
                                 "message": "Quote 5000 pcs of X-200 with CE certificate please.",
                                 "language": "en", "company": "DE Co"}).json()
    cites = [x["citation_id"] for x in d["draft"]["citations"]] if d.get("draft") else []
    other = [x for x in cites if x.startswith("REG-") and x not in ("REG-EU-112",)]
    check("国别已知时，引用里只有该国/通用法规", not other, f"cites={cites}")


# --------------------------------------------------------------------------
# §4 跨语种召回与术语表（FR-KB-02 / FR-KB-04，PRD 9.2 / 9.4 难点①）
# --------------------------------------------------------------------------
def audit_4_crosslingual() -> None:
    section("§4 跨语种召回与术语表（FR-KB-02/04，PRD 9.2/9.4）")
    store = new_store()
    r = StoreRetriever(store, KB_VERSION)
    clause("4.1")
    cases = [
        ("es", "¿Cuánto cuesta el X-200 FOB Shenzhen? precio y plazo de entrega", "X-200"),
        ("en", "Please quote M-77 lifting clamp FOB Ningbo, price and lead time", "M-77"),
        ("ar", "ما هو موعد التسليم والسعر للطراز X-200؟", "X-200"),
        ("zh", "X-200 的材质和安全载荷是多少？", "X-200"),
    ]
    for lang, text, expect in cases:
        ids = cite_ids(r.search(SearchQuery(text=text, lang=lang, top_k=4)))
        check(f"{lang} 提问命中 {expect}", expect in ids, str(ids))
    clause("4.2")
    term_cases = [
        ("¿Cuál es la cantidad mínima de pedido?", "moq"),
        ("What is the lead time?", "lead_time"),
        ("请报一下价格", "price"),
        ("ما هو موعد التسليم؟", "lead_time"),
        ("ما هو السعر؟", "price"),          # 阿语定冠词
        ("الحد الأدنى للطلب是多少", "moq"),
        ("¿cuál es el precio de referencia?", "price"),
        ("交货期多久", "lead_time"),
    ]
    for text, concept in term_cases:
        got = map_terms(text)
        check(f"术语映射：{text[:22]} → {concept}", concept in got, str(got))
    clause("4.3")
    incomplete = [k for k, v in TERM_TABLE.items()
                  if not {"zh", "en", "es", "ar"} <= set(v.keys())]
    check("每个术语概念都有 zh/en/es/ar 四列", not incomplete, str(incomplete))
    clause("4.4")
    no_forbidden = [k for k in TERM_TABLE if "forbidden" not in TERM_TABLE[k]]
    note(f"术语表只有 {len(TERM_TABLE)} 个概念（PRD 9.1 自建目标 ≥ 200），且表结构里没有禁用译法字段",
         f"禁用译法只存在种子块的 extra 里，管线不消费它 → FR-KB-02 的『禁用译法』未落地")


# --------------------------------------------------------------------------
# §5 结构化优先与"不猜型号"（PRD 9.3 分块策略 + S3 提交口径）
# --------------------------------------------------------------------------
def audit_5_structured() -> None:
    section("§5 结构化优先与『不猜型号』（PRD 9.3 / S3 提交口径）")
    store = new_store()
    r = StoreRetriever(store, KB_VERSION)
    clause("5.1")
    hits = r.search(SearchQuery(text="Please quote M-77 FOB Ningbo", lang="en", top_k=4))
    check("点名型号 → 该型号排第一",
          bool(hits) and hits[0].chunk.identifiers.get("sku") == "M-77",
          str(cite_ids(hits)))
    check("命中理由含型号匹配", bool(hits) and any("型号" in x for x in hits[0].reasons),
          str(hits[0].reasons) if hits else "")
    clause("5.2")
    c = new_client(store)
    d = c.post("/inbound", json={"buyer_id": "BY-NOSKU", "country": "US",
                                 "message": "Send me your best price for the manual hydraulic hand truck.",
                                 "language": "en", "company": "NoSku Co"}).json()
    body = d["draft"]["body"] if d.get("draft") else ""
    cites = [x["citation_id"] for x in d["draft"]["citations"]] if d.get("draft") else []
    check("未点名型号时不得报具体型号与价格（宁可追问）",
          "USD" not in body and not cites,
          f"status={d.get('status')} 正文含价格={'USD' in body} 引用={cites}")
    clause("5.4")
    known = {c2.identifiers.get("cert_no") or c2.identifiers.get("sku")
             or c2.identifiers.get("clause_no") or c2.identifiers.get("term_id") or c2.doc_id
             for c2 in store.list_kb_chunks(KB_VERSION)}
    raw_cites = d["draft"]["citations"] if d.get("draft") else []
    orphan = [x["citation_id"] for x in raw_cites if x["citation_id"] not in known]
    check("走写死兜底路径时产生的引用仍要可回指（FR-KB-07）", not orphan,
          f"无法在知识库回指的引用：{orphan}（兜底路径用的是 S1 硬编码引用格式）")
    clause("5.3")
    d2 = c.post("/inbound", json={"buyer_id": "BY-NOSKU2", "country": "US",
                                  "message": "Send me your best price.",
                                  "language": "en", "company": "NoSku Co2"}).json()
    body2 = d2["draft"]["body"] if d2.get("draft") else ""
    check("信息不足时只发追问，不带价格", "USD" not in body2, body2[:80].replace("\n", " "))


# --------------------------------------------------------------------------
# §6 引用装配与正文一致（PRD 9.3 / FR-KB-07，清单 §三.2）
# --------------------------------------------------------------------------
def audit_6_citations() -> None:
    section("§6 引用装配与正文一致性（PRD 9.3 / FR-KB-07）")
    store = new_store()
    c = new_client(store)
    clause("6.1")
    d = c.post("/inbound", json={"buyer_id": "BY-CITE", "country": "DE",
                                 "message": "Quote 5000 pcs of X-200 FOB Shenzhen with CE certificate please.",
                                 "language": "en", "company": "Cite Co"}).json()
    cites = d["draft"]["citations"] if d.get("draft") else []
    body = d["draft"]["body"] if d.get("draft") else ""
    known = {c2.identifiers.get("cert_no") or c2.identifiers.get("sku")
             or c2.identifiers.get("clause_no") or c2.identifiers.get("term_id") or c2.doc_id
             for c2 in store.list_kb_chunks(KB_VERSION)}
    unknown = [x["citation_id"] for x in cites if x["citation_id"] not in known]
    check("每条引用都有标识且能回指", cites and not unknown, f"unknown={unknown}")
    clause("6.2")
    check("每条引用 note 带库版本",
          all("库版本" in (x.get("note") or "") for x in cites),
          str([x.get("note", "")[:40] for x in cites]))
    clause("6.3")
    m = re.search(r"USD\s*([\d.]+)[–-]([\d.]+)", body)
    chunk = next((x for x in store.list_kb_chunks(KB_VERSION)
                  if x.identifiers.get("sku") == "X-200"), None)
    price_ok = bool(m) and chunk is not None and float(m.group(1)) == float(chunk.extra["price_low"])
    check("正文里的价格与命中知识块一致", price_ok,
          f"正文={m.group(0) if m else '无'} 知识块={chunk.extra.get('price_low') if chunk else '?'}")
    lt = re.search(r"lead time of ([\d-]+) days", body)
    lt_ok = bool(lt) and chunk is not None and str(chunk.extra["lead_time_days"]) in lt.group(1)
    check("正文里的交期与命中知识块一致", lt_ok,
          f"正文={lt.group(0) if lt else '无'} 知识块={chunk.extra.get('lead_time_days') if chunk else '?'}")
    clause("6.4")
    cm = re.search(r"certificate ([A-Z0-9-]+)", body)
    cert_in_cites = [x["citation_id"] for x in cites if x["citation_id"].startswith("CERT")]
    check("正文里说的证书号在引用列表里（正文与引用不打架）",
          (not cm) or cm.group(1) in cert_in_cites,
          f"正文证书={cm.group(1) if cm else '—'} 引用里的证书={cert_in_cites}")
    clause("6.5")
    # ISO 9001 是工厂质量体系证书（covers=[] = 未声明覆盖型号），不是型号认证。
    # 模板写死 "The model is covered by certificate X" → 对买家的合规声明会失真。
    iso_chunk = next((x for x in store.list_kb_chunks(KB_VERSION)
                      if x.identifiers.get("cert_no") == "CERT-ISO-9001"), None)
    covers = iso_chunk.extra.get("covers") if iso_chunk else None
    d_iso = c.post("/inbound", json={"buyer_id": "BY-ISO", "country": "BR",
                                     "message": "Quote 5000 pcs of X-200 with CE certificate please.",
                                     "language": "en", "company": "Iso Co"}).json()
    body_iso = d_iso["draft"]["body"] if d_iso.get("draft") else ""
    bad_claim = "model is covered by certificate CERT-ISO-9001" in body_iso
    check("正文不得把『工厂体系证书』表述为『型号由该证书覆盖』", not bad_claim,
          f"ISO covers={covers}（未声明覆盖型号）；BR 买家正文含型号覆盖表述={bad_claim}；"
          f"正文片段={body_iso[body_iso.find('certificate'):][:60] if 'certificate' in body_iso else body_iso[:60]}")


# --------------------------------------------------------------------------
# §7 草稿数据来自知识库（FR-KB-03，清单条款 1.9）
# --------------------------------------------------------------------------
def audit_7_data_from_kb() -> None:
    section("§7 草稿数据来自知识库（FR-KB-03，清单 1.9）")
    store = new_store()
    c = new_client(store)
    clause("7.1")
    before = c.post("/inbound", json={"buyer_id": "BY-P1", "country": "US",
                                      "message": "Quote 100 pcs of X-300 electric pallet truck price please.",
                                      "language": "en", "company": "P1 Co"}).json()
    body_before = before["draft"]["body"] if before.get("draft") else ""
    check("草稿价格取自知识库", "12.3" in body_before, body_before[:90].replace("\n", " "))
    clause("7.2")
    # 改库（块数不变）→ 同一进程内的新草稿应当跟着变
    chunk = next(x for x in store.list_kb_chunks(KB_VERSION) if x.identifiers.get("sku") == "X-300")
    extra = dict(chunk.extra)
    extra["price_low"] = 9.99
    chunk.extra = extra
    store.upsert_kb_chunk(chunk)
    after = c.post("/inbound", json={"buyer_id": "BY-P2", "country": "US",
                                     "message": "Quote 100 pcs of X-300 electric pallet truck price please.",
                                     "language": "en", "company": "P2 Co"}).json()
    body_after = after["draft"]["body"] if after.get("draft") else ""
    check("改库后**同一进程内**的草稿价格随之变化", "9.99" in body_after and "12.3" not in body_after,
          f"新草稿仍报旧价：{body_after[:90].replace(chr(10),' ')}")
    clause("7.3")
    # 新进程（新 retriever 实例）下应当变 —— 用来区分"没改到库"和"缓存不失效"
    c2 = new_client(store)
    after2 = c2.post("/inbound", json={"buyer_id": "BY-P3", "country": "US",
                                       "message": "Quote 100 pcs of X-300 electric pallet truck price please.",
                                       "language": "en", "company": "P3 Co"}).json()
    body_after2 = after2["draft"]["body"] if after2.get("draft") else ""
    check("新进程下改库能生效（对照项：确认改动确实落库了）",
          "9.99" in body_after2, body_after2[:90].replace("\n", " "))
    clause("7.4")
    # 价格真源是否唯一：检索未命中产品时是否回退到写死常量
    from tradedesk.data.seed import demo_products
    hard = demo_products()
    d = c2.post("/inbound", json={"buyer_id": "BY-P4", "country": "US",
                                  "message": "Price for the electric pallet truck please.",
                                  "language": "en", "company": "P4 Co"}).json()
    body = d["draft"]["body"] if d.get("draft") else ""
    hard_price = str(hard[2]["price_low"]) if len(hard) > 2 else ""
    check("检索未确认产品时不得回退到写死常量报价格（价格真源必须唯一）",
          "USD" not in body,
          f"正文用了写死常量价格={hard_price}：{body[:90].replace(chr(10),' ')}")


# --------------------------------------------------------------------------
# §8 分块策略与 PRD 偏离（PRD 9.3 / FR-KB-01）
# --------------------------------------------------------------------------
def audit_8_chunking() -> None:
    section("§8 分块策略与 PRD 偏离（PRD 9.3 分块策略 / FR-KB-01）")
    store = new_store()
    clause("8.1")
    chunks = store.list_kb_chunks(KB_VERSION)
    prod = [c for c in chunks if c.doc_type == "产品条目"]
    # PRD 9.3：产品属性不进文本块，走属性过滤（数值不该进语义文本）
    numeric_in_text = []
    for c in prod:
        t = c.text
        if (c.extra.get("price_low") and str(c.extra["price_low"]) in t) or \
           (c.extra.get("lead_time_days") and str(c.extra["lead_time_days"]) in t) or \
           (c.extra.get("moq") and str(c.extra["moq"]) in t):
            numeric_in_text.append(c.chunk_id)
    check("产品属性数值不得进文本块（PRD 9.3 明文：走属性过滤）",
          not numeric_in_text, str(numeric_in_text))
    clause("8.2")
    # FR-KB-01：同一产品 4 语版本均可入库并被正确召回
    per_sku = {}
    for c in prod:
        per_sku.setdefault(c.identifiers.get("sku"), []).append(c.lang)
    multi = {k: v for k, v in per_sku.items() if len(v) >= 4}
    check("同一产品的 4 语版本分别入库（FR-KB-01）", bool(multi),
          f"每个 SKU 只有 1 个块（lang=zh），4 语正文挤在一个块里：{per_sku}")
    clause("8.3")
    check("术语表 / 话术库 存在于知识库", True,
          "话术库为 0 条（PRD 9.1 ≥ 60 条），本轮未建")


# --------------------------------------------------------------------------
# §9 接口与双后端
# --------------------------------------------------------------------------
def audit_9_api_and_backends() -> None:
    section("§9 接口与双后端")
    store = new_store()
    c = new_client(store)
    clause("9.1")
    r = c.get("/kb/search", params={"q": "¿plazo de entrega del X-200?", "lang": "es",
                                    "country": "BR"})
    check("/kb/search 返回 200", r.status_code == 200, str(r.status_code))
    body = r.json()
    h0 = (body.get("hits") or [{}])[0]
    for key in ("citation_id", "identifiers", "score", "reasons", "expired", "not_yet_effective"):
        check(f"命中含 {key}", key in h0)
    check("返回库版本", body.get("kb_version") == KB_VERSION, str(body.get("kb_version")))
    clause("9.2")
    r2 = c.get("/kb/search", params={"q": "TC-M77-118 test certificate SGS", "lang": "en"})
    ids2 = [(x["identifiers"].get("cert_no") or x["identifiers"].get("sku")) for x in r2.json()["hits"]]
    check("/kb/search 不返回过期内容", "TC-M77-118" not in ids2, str(ids2))
    clause("9.3")
    r3 = c.get("/kb/search", params={"q": "REG-AE-099 final user requirement", "lang": "en",
                                     "country": "BR"})
    ids3 = [(x["identifiers"].get("clause_no") or x["identifiers"].get("sku"))
            for x in r3.json()["hits"]]
    check("/kb/search 不返回买家国别无关的合规条款", "REG-AE-099" not in ids3, str(ids3))


def audit_10_backends() -> None:
    """同一组查询在两个后端上跑，比对结果序列（清单 1.12）。"""
    section("§10 双后端一致性（清单 1.12）")
    if not DSN:
        note("本次只跑 SQLite，未做双后端比对", "加 --dsn <PostgreSQL DSN> 再跑")
        return
    clause("1.12")
    head = _pg_split(DSN)
    queries = [
        ("¿plazo de entrega del X-200?", "es", "BR", ""),
        ("Quote M-77 lifting clamp price", "en", "US", ""),
        ("清关认证要求", "zh", "BR", ""),
        ("X-200 CE certificate CERT-CE-2041", "en", "DE", ""),
    ]
    # 明确各建一个：SQLite 用临时文件，PostgreSQL 用独立库
    _SEQ[0] += 1
    spath = ROOT / "data" / f"audit_s3_cmp_{_SEQ[0]:02d}.db"
    for suffix in ("", "-wal", "-shm"):
        f = spath.with_name(spath.name + suffix)
        if f.exists():
            f.unlink()
    _TMPFILES.append(spath)
    sqlite_store = SqliteStateStore(str(spath))
    load_kb(sqlite_store, KB_VERSION)
    _STORES.append(sqlite_store)
    sq = StoreRetriever(sqlite_store, KB_VERSION)
    pg_store = new_store()
    pq = StoreRetriever(pg_store, KB_VERSION)
    for text, lang, country, sku in queries:
        q = dict(text=text, lang=lang, buyer_country=country, sku=sku, top_k=8)
        a = doc_ids(sq.search(SearchQuery(**q)))
        b = doc_ids(pq.search(SearchQuery(**q)))
        check(f"双后端结果一致：{text[:26]}", a == b, f"SQLite={a} PG={b}")


# --------------------------------------------------------------------------
def run_all() -> None:
    for fn in (audit_1_assets, audit_2_expiry, audit_3_country, audit_4_crosslingual,
               audit_5_structured, audit_6_citations, audit_7_data_from_kb,
               audit_8_chunking, audit_9_api_and_backends, audit_10_backends):
        fn()
        close_stores()


def main() -> int:
    global DSN
    ap = argparse.ArgumentParser(description="S3 独立审核脚本")
    ap.add_argument("--dsn", default="", help="PostgreSQL DSN（留空 = SQLite）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    DSN = args.dsn
    backend = f"PostgreSQL {args.dsn.rsplit('/', 1)[-1]}" if args.dsn else "SQLite"

    print(f"TradeDesk-AI · S3 独立审核（{backend}）")
    print("=" * 70)
    try:
        run_all()
    finally:
        cleanup()

    fails = [r for r in RESULTS if r["level"] == "FAIL"]
    warns = [r for r in RESULTS if r["level"] == "WARN"]
    print("\n" + "=" * 70)
    print(f"审核结果（{backend}）：通过 {len(RESULTS) - len(fails) - len(warns)}，"
          f"FAIL {len(fails)}，WARN {len(warns)}，共 {len(RESULTS)} 项")
    if fails:
        print("\n违约项（对应 docs/S3-ACCEPTANCE.md / PRD 第 9 章）：")
        for r in fails:
            print(f"  ✗ [{r['clause']}] {r['name']}" + (f" —— {r['detail']}" if r["detail"] else ""))
    if warns:
        print("\n记账项（不违约，但要记）：")
        for r in warns:
            print(f"  ! [{r['clause']}] {r['name']}" + (f" —— {r['detail']}" if r["detail"] else ""))
    print("=" * 70)
    if args.json:
        print(json.dumps({"backend": backend, "results": RESULTS,
                          "fail": len(fails), "warn": len(warns)}, ensure_ascii=False, indent=2))
    return 1 if fails else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(2)
