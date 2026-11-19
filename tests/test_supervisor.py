# -*- coding: utf-8 -*-
"""Supervisor：路由、风控优先、幂等、签核、阶段推进、SLA。"""
import pytest

from tradedesk.domain.enums import Intent, Language, RiskLevel, Stage
from tradedesk.runtime.supervisor import Supervisor, draft_id
from tradedesk.store.sqlite_store import SqliteStateStore


@pytest.fixture
def sup(tmpdir_local):
    store = SqliteStateStore(str(tmpdir_local / "sup.db"))
    from tradedesk.api.deps import build_registry
    s = Supervisor(store=store, registry=build_registry())
    s.store = store
    yield s
    store.close()


def test_route_complaint_to_human(sup):
    d = sup.route("The goods arrived damaged, I need compensation", Language.EN)
    assert d["target"] == "human"
    assert Intent.COMPLAINT in d["intents"]


def test_route_price_to_inquiry(sup):
    d = sup.route("Could you quote 100 pcs?", Language.EN)
    assert d["target"] == "inquiry"


def test_ingest_creates_thread_and_draft(sup):
    r = sup.ingest(buyer_id="BY-BR-01", message="Quote 5000 pcs of X-200 FOB Shenzhen.",
                   language=Language.EN, company="Brasil Distribuidora Ltda", country="BR")
    assert r.status == "draft_ready"
    assert r.draft is not None and r.draft.citations


def test_ingest_is_idempotent(sup):
    kw = dict(buyer_id="BY-BR-01", message="Quote 5000 pcs of X-200 FOB Shenzhen.",
              language=Language.EN, company="Brasil Distribuidora Ltda", country="BR")
    r1 = sup.ingest(**kw)
    r2 = sup.ingest(**kw)
    assert r2.duplicate is True
    assert r2.thread_id == r1.thread_id


def test_sanction_hit_suppresses_draft(sup):
    r = sup.ingest(buyer_id="BY-AE-01", message="Quote M-77 please, 3000 pcs.",
                   language=Language.AR, company="Gulf Trading Partners LLC", country="AE")
    assert r.status == "blocked"
    assert r.draft is None
    assert r.risk_level is RiskLevel.HIGH


def test_signoff_requires_valid_token(sup):
    r = sup.ingest(buyer_id="BY-BR-09", message="Quote 1000 pcs of M-77.",
                   language=Language.EN, company="Test Co", country="US")
    out = sup.sign_off(thread_id=r.thread_id, draft_id=draft_id(r.thread_id),
                       token="bogus", action="approve")
    assert out["ok"] is False and out["code"] == "gate_token_invalid"


def test_token_is_single_use(sup):
    r = sup.ingest(buyer_id="BY-BR-10", message="Quote 1000 pcs of M-77.",
                   language=Language.EN, company="Test Co", country="US")
    did = draft_id(r.thread_id)
    token = sup.tokens.issue(r.thread_id, did, [])
    assert sup.sign_off(thread_id=r.thread_id, draft_id=did, token=token.token,
                        action="approve")["ok"] is True
    assert sup.sign_off(thread_id=r.thread_id, draft_id=did, token=token.token,
                        action="approve")["ok"] is False


def test_send_before_signoff_is_rejected(sup):
    r = sup.ingest(buyer_id="BY-BR-11", message="Quote 1000 pcs of M-77.",
                   language=Language.EN, company="Test Co", country="US")
    did = draft_id(r.thread_id)
    token = sup.tokens.issue(r.thread_id, did, [])
    out = sup.send(thread_id=r.thread_id, draft_id=did, token=token.token)
    assert out["ok"] is False and out["code"] == "not_signed_off"


def test_advance_rejects_missing_contract_version(sup):
    r = sup.ingest(buyer_id="BY-BR-12", message="Quote 1000 pcs of X-200.",
                   language=Language.EN, company="Test Co", country="US")
    sup.advance(thread_id=r.thread_id, to_stage=Stage.QUOTING)
    out = sup.advance(thread_id=r.thread_id, to_stage=Stage.ORDERED)
    assert out["ok"] is False and out["code"] == "invalid_transition"


def test_sla_scan_flags_overdue(sup):
    from tradedesk.domain.models import Deadline
    r = sup.ingest(buyer_id="BY-BR-13", message="Quote 1000 pcs of X-200, urgent.",
                   language=Language.EN, company="Test Co", country="US")
    t = sup.store.get_thread(r.thread_id)
    t.deadlines = [Deadline(label="报价 SLA", due_at=datetime_now(-3600), level="over")]
    t.version += 1
    sup.store.save_thread(t)
    overdue = sup.sla_scan()
    assert any(o["thread_id"] == r.thread_id for o in overdue)
    assert "sla_overdue" in sup.store.get_thread(r.thread_id).status_flags


def datetime_now(offset_seconds: int):
    from datetime import datetime, timedelta, timezone
    return datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)