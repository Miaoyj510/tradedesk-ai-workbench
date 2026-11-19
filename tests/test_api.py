# -*- coding: utf-8 -*-
"""HTTP 接口层：健康检查、角色注册表、来信接入、闸门令牌守卫、写操作分离。"""
import pytest
from fastapi.testclient import TestClient

from tradedesk.api.app import create_app
from tradedesk.api.deps import AppContext
from tradedesk.store.sqlite_store import SqliteStateStore


@pytest.fixture
def client(tmpdir_local):
    store = SqliteStateStore(str(tmpdir_local / "api.db"))
    c = TestClient(create_app(AppContext(store=store)))
    yield c
    store.close()


def _inbound(c, **kw):
    payload = {
        "buyer_id": "BY-BR-01",
        "message": "Quote 5000 pcs of X-200 FOB Shenzhen, do you have CE certification?",
        "language": "en", "company": "Brasil Distribuidora Ltda", "country": "BR",
    }
    payload.update(kw)
    return c.post("/inbound", json=payload)


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_agents_registry_endpoint(client):
    specs = {a["agent_id"]: a for a in client.get("/agents").json()}
    assert specs["merch.listing"]["status"] == "declared_only"
    assert specs["growth.campaign"]["status"] == "declared_only"
    assert specs["inquiry.reply"]["status"] == "active"


def test_invoke_declared_agent_is_noop(client):
    r = client.post("/agents/merch.listing/invoke")
    assert r.status_code == 200
    assert r.json()["status"] == "not_implemented"
    assert r.json()["side_effects"] == "none"


def test_inbound_returns_draft_and_gates(client):
    d = _inbound(client).json()
    assert d["status"] == "draft_ready"
    assert d["draft"]["citations"]
    assert "GATE-01" in d["blocking_gates"]


def test_inbound_is_idempotent(client):
    before = len(client.get("/threads").json())
    _inbound(client)
    r = _inbound(client)
    assert r.json()["duplicate"] is True
    assert len(client.get("/threads").json()) == before + 1


def test_sanction_hit_blocks_draft(client):
    d = _inbound(client, buyer_id="BY-AE-01", language="ar",
                 company="Gulf Trading Partners LLC", country="AE",
                 message="نطلب عينة من الموديل M-77 وسعر 3000 قطعة").json()
    assert d["status"] == "blocked"
    assert d["draft"] is None
    assert "GATE-03" in d["blocking_gates"]


def test_signoff_without_token_is_403(client):
    d = _inbound(client).json()
    r = client.post(f"/threads/{d['thread_id']}/signoff",
                    json={"draft_id": d["draft_id"], "action": "approve"})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "gate_token_required"


def test_signoff_with_token_then_send(client):
    d = _inbound(client).json()
    tid, did = d["thread_id"], d["draft_id"]
    token = client.post(f"/threads/{tid}/gate-token", json={"draft_id": did}).json()["token"]
    r = client.post(f"/threads/{tid}/signoff", json={"draft_id": did, "action": "approve"},
                    headers={"X-Gate-Token": token})
    assert r.status_code == 200
    assert client.get(f"/threads/{tid}").json()["price_signed_off"] is True
    r2 = client.post(f"/threads/{tid}/send", json={"draft_id": did},
                     headers={"X-Gate-Token": token})
    assert r2.status_code == 403, "令牌一次性：第二次发送必须被拒"


def test_send_before_signoff_is_rejected(client):
    d = _inbound(client).json()
    tid, did = d["thread_id"], d["draft_id"]
    token = client.post(f"/threads/{tid}/gate-token", json={"draft_id": did}).json()["token"]
    r = client.post(f"/threads/{tid}/send", json={"draft_id": did},
                    headers={"X-Gate-Token": token})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "not_signed_off"


def test_advance_requires_contract_version(client):
    d = _inbound(client).json()
    tid = d["thread_id"]
    assert client.post(f"/threads/{tid}/advance", json={"to_stage": "QUOTING"}).status_code == 200
    r = client.post(f"/threads/{tid}/advance", json={"to_stage": "ORDERED"})
    assert r.status_code == 409


def test_unknown_stage_is_400(client):
    d = _inbound(client).json()
    r = client.post(f"/threads/{d['thread_id']}/advance", json={"to_stage": "NOPE"})
    assert r.status_code == 400


def test_events_are_replayable(client):
    d = _inbound(client).json()
    events = client.get(f"/threads/{d['thread_id']}/events").json()
    assert events[0]["event_type"] == "thread.created"
    assert [e["seq"] for e in events] == sorted(e["seq"] for e in events)


def test_unknown_agent_is_404(client):
    assert client.post("/agents/nope/invoke").status_code == 404