# -*- coding: utf-8 -*-
"""S6-1a 测试：工作台读模型（BFF）与前端契约对齐。"""
import pytest
from fastapi.testclient import TestClient

from tradedesk.api.app import create_app
from tradedesk.api.deps import AppContext
from tradedesk.store.sqlite_store import SqliteStateStore

# 前端原型 data.js 导出的 key（读模型必须至少包含这些）
CONTRACT_KEYS = ["threads", "kpis", "stages", "costByLang", "costTrend",
                 "riskEvents", "gates", "kbAssets", "terms", "STAGE", "INTENT"]

THREAD_KEYS = ["id", "company", "country", "flag", "lang", "stage", "risk", "state",
               "sla", "wait", "intent", "title", "draft", "citations",
               "commitments", "open", "deadlines", "gates"]


@pytest.fixture
def client(tmpdir_local):
    store = SqliteStateStore(str(tmpdir_local / "wb.db"))
    c = TestClient(create_app(AppContext(store=store)))
    yield c, store
    store.close()


def _inbound(c, **kw):
    payload = {
        "buyer_id": "BY-BR-01",
        "message": "Quote 5000 pcs of X-200 FOB Shenzhen, do you have CE certification?",
        "language": "en", "company": "Brasil Distribuidora Ltda", "country": "BR",
    }
    payload.update(kw)
    return c.post("/inbound", json=payload)


def test_workbench_has_the_frontend_contract(client):
    c, _ = client
    _inbound(c)
    r = c.get("/workbench")
    assert r.status_code == 200
    d = r.json()
    for k in CONTRACT_KEYS:
        assert k in d, "缺少前端契约里的字段：" + k
    assert d["source"] == "live"
    assert d["kb_version"]


def test_workbench_thread_shape(client):
    c, _ = client
    _inbound(c)
    d = c.get("/workbench").json()
    assert d["threads"], "应当至少有一个线程"
    t = d["threads"][0]
    for k in THREAD_KEYS:
        assert k in t, "线程缺字段：" + k
    assert t["id"]
    assert t["stage"] == "INQUIRY"
    assert t["draft"]["body"]
    assert isinstance(t["citations"], list)
    assert isinstance(t["gates"], list) and len(t["gates"]) == 11, "闸门表应有 11 条"
    assert isinstance(t["deadlines"], list) and t["deadlines"]


def test_buyer_message_is_persisted_and_returned(client):
    """买家原话不能只存哈希（FR-EVA-02）。"""
    c, store = client
    _inbound(c, message="Please quote 3000 pcs of X-200 FOB Shenzhen")
    t = c.get("/workbench").json()["threads"][0]
    assert "3000 pcs" in t["buyer_message"]
    assert "3000 pcs" in t["title"]
    # 原话也要在事件流里留痕（不能只在内存里）
    evs = store.list_events(t["id"])
    assert any(e.event_type.value == "message.received" for e in evs)


def test_price_gate_hits_in_workbench(client):
    """含价格的草稿在工作台里也要命中 GATE-01。"""
    c, _ = client
    _inbound(c)
    t = c.get("/workbench").json()["threads"][0]
    g1 = [g for g in t["gates"] if g["id"] == "GATE-01"][0]
    assert g1["hit"], "含价格的草稿必须阻断出发"


def test_quality_is_not_faked(client):
    """分语种质量不是实时数据 → 不能给假数字。"""
    c, _ = client
    _inbound(c)
    d = c.get("/workbench").json()
    assert d["quality"] == [], "评测质量不得从实时接口伪造"
    assert any("quality" in n or "质量" in n for n in d["notes"])


def test_kb_assets_and_terms_come_from_real_kb(client):
    c, _ = client
    _inbound(c)
    d = c.get("/workbench").json()
    assert d["kbAssets"], "证据块应来自真实检索"
    assert any(a["id"] for a in d["kbAssets"])
    assert d["terms"], "术语表应来自知识库"
    t0 = d["terms"][0]
    for k in ("zh", "en", "es", "ar", "forbid"):
        assert k in t0


def test_gate_catalog_has_eleven_gates(client):
    c, _ = client
    _inbound(c)
    gates = c.get("/workbench").json()["gates"]
    assert len(gates) == 11
    must = [g for g in gates if g["must"]]
    assert len(must) == 6, "PRD 8.5：6 条强制人工签核"


def test_workbench_on_empty_store_does_not_crash(client):
    c, _ = client
    d = c.get("/workbench").json()
    assert d["threads"] == []
    assert d["kpis"]["pending"] == 0


def test_complaint_thread_shows_human_takeover(client):
    """投诉路径用的是事件（human.takeover）而不是线程标志 —— 只看标志会显示成“草稿待确认”。"""
    c, _ = client
    _inbound(c, buyer_id="BY-C1", language="en", country="US",
             message="The goods arrived damaged, I need compensation")
    t = [x for x in c.get("/workbench").json()["threads"]
         if x["buyer_message"].startswith("The goods arrived")][0]
    assert t["state"] == "human_takeover", t["state"]
    assert t["risk"] == "mid", t["risk"]


def test_first_response_metric_has_declared_caliber(client):
    """首响口径必须写清楚：“收到消息→草稿”不是“收到消息→发出”。"""
    c, _ = client
    _inbound(c)
    d = c.get("/workbench").json()
    assert "avgFirstResponse" in d["kpis"]
    assert "avgFirstResponseSigned" in d["kpis"]
    assert any("首响口径" in n or "avgFirstResponse" in n for n in d["notes"]), d["notes"]
