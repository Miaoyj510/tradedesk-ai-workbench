# -*- coding: utf-8 -*-
"""闸门：11 条规则、命中判定、令牌一次性与绑定。"""
from datetime import datetime, timedelta, timezone

from tradedesk.domain.enums import GateId, Intent, Language
from tradedesk.domain.models import Draft, ThreadState
from tradedesk.runtime.gates import GateTokenService, blocking_gates, evaluate_gates
from tradedesk.store.sqlite_store import SqliteStateStore


def _thread(**kw):
    base = dict(thread_id="T-G", buyer_id="BY-1", language=Language.ES)
    base.update(kw)
    return ThreadState(**base)


def _draft(body="Hola", **kw):
    base = dict(thread_id="T-G", lang=Language.ES, body=body, confidence=0.9)
    base.update(kw)
    return Draft(**base)


def test_eleven_rules():
    gates = evaluate_gates(_thread(), None)
    assert len(gates) == 11
    # PRD 8.5 里标"强制人工签核"的 6 条：价格/交期/名单/合同/法规时效/金额阈值
    assert sum(1 for g in gates if g.must) == 6
    assert {g.gate_id for g in gates if g.must} == {
        GateId.PRICE, GateId.DELIVERY, GateId.SANCTION,
        GateId.CONTRACT, GateId.REGULATION, GateId.THRESHOLD,
    }


def test_price_and_delivery_hit():
    d = _draft("参考价 USD 4.85 por unidad, 交货期 25–30 天")
    hits = {g.gate_id for g in blocking_gates(evaluate_gates(_thread(), d))}
    assert GateId.PRICE in hits and GateId.DELIVERY in hits


def test_low_confidence_gate():
    d = _draft("Hola", confidence=0.41)
    hits = {g.gate_id for g in blocking_gates(evaluate_gates(_thread(), d))}
    assert GateId.LOW_CONFIDENCE in hits


def test_sanction_flag_triggers_gate():
    hits = {g.gate_id for g in blocking_gates(evaluate_gates(_thread(status_flags=["sanction_hit"]), None))}
    assert GateId.SANCTION in hits


def test_complaint_intent_triggers_gate():
    hits = {g.gate_id for g in blocking_gates(evaluate_gates(_thread(intent_labels=[Intent.COMPLAINT]), None))}
    assert GateId.COMPLAINT in hits


def test_amount_threshold_gate():
    d = _draft("USD 9000 per container")
    hits = {g.gate_id for g in blocking_gates(evaluate_gates(_thread(), d))}
    assert GateId.THRESHOLD in hits


def test_amount_threshold_gate_currency_after_number():
    """币种写在数字**后面**同样要命中。

    只认 "USD 8000" 的话，把币种挪到数字后面就能绕过金额阈值闸门。
    """
    for body in ("Total 8000 USD", "8000 USD in total", "我们会给你 8000 美元"):
        d = _draft(body)
        hits = {g.gate_id for g in blocking_gates(evaluate_gates(_thread(), d))}
        assert GateId.THRESHOLD in hits, body


def test_amount_threshold_gate_price_context():
    d = _draft("总价 8000")
    hits = {g.gate_id for g in blocking_gates(evaluate_gates(_thread(), d))}
    assert GateId.THRESHOLD in hits


def test_amount_threshold_ignores_non_amount_numbers():
    """天数、数量、重量不是金额 —— 误判会把正常草稿全卡死在闸门上。"""
    for body in ("Delivery in 250 days", "Total quantity 12000 units", "Weight 8000 kg"):
        d = _draft(body)
        hits = {g.gate_id for g in blocking_gates(evaluate_gates(_thread(), d))}
        assert GateId.THRESHOLD not in hits, body


def test_token_single_use(tmpdir_local):
    store = SqliteStateStore(str(tmpdir_local / "t.db"))
    svc = GateTokenService(store, ttl_seconds=60)
    tok = svc.issue("T-G", "D-1", [GateId.PRICE])
    assert svc.consume(tok.token, "T-G", "D-1") is True
    assert svc.consume(tok.token, "T-G", "D-1") is False


def test_token_binding(tmpdir_local):
    store = SqliteStateStore(str(tmpdir_local / "t2.db"))
    svc = GateTokenService(store, ttl_seconds=60)
    tok = svc.issue("T-G", "D-1", [GateId.PRICE])
    assert svc.consume(tok.token, "T-G", "OTHER") is False
    assert svc.consume(tok.token, "OTHER-THREAD", "D-1") is False


def test_token_expired(tmpdir_local):
    store = SqliteStateStore(str(tmpdir_local / "t3.db"))
    svc = GateTokenService(store, ttl_seconds=60)
    tok = svc.issue("T-G", "D-1", [GateId.PRICE])
    store.expire_gate_token(tok.token, datetime.now(timezone.utc) - timedelta(seconds=1))
    assert svc.consume(tok.token, "T-G", "D-1") is False