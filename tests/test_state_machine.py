# -*- coding: utf-8 -*-
"""状态机：合法迁移、前置条件、幂等、终态。"""
from tradedesk.domain.enums import Language, Stage
from tradedesk.domain.state_machine import (
    TransitionRejected,
    apply_transition,
    build_thread,
    check_transition,
    is_duplicate_message,
    is_terminal,
)


def _thread(stage=Stage.INQUIRY):
    th = build_thread("T-TEST", "BY-1", Language.EN, message_hash="h1")
    th.stage = stage
    return th


def test_terminal_stages():
    assert is_terminal(Stage.CLOSED) and is_terminal(Stage.LOST)
    assert not is_terminal(Stage.QUOTING)


def test_legal_chain():
    th = _thread()
    for target in (Stage.SAMPLING, Stage.QUOTING):
        th = apply_transition(th, target)
    assert th.stage is Stage.QUOTING
    assert th.version == 3


def test_illegal_transition_raises():
    th = _thread(Stage.QUOTING)
    try:
        apply_transition(th, Stage.ORDERED)
        assert False, "应当抛出 TransitionRejected"
    except TransitionRejected as e:
        assert "合同" in e.message


def test_order_requires_contract_version():
    th = _thread(Stage.QUOTING)
    ok, reason = check_transition(th, Stage.ORDERED)
    assert ok is False and "合同" in reason
    th.contract_version = "PI-1"
    ok2, _ = check_transition(th, Stage.ORDERED)
    assert ok2 is True


def test_closed_is_terminal():
    th = _thread(Stage.CLOSED)
    ok, reason = check_transition(th, Stage.QUOTING)
    assert ok is False and "终态" in reason


def test_same_stage_is_noop():
    th = _thread()
    ok, reason = check_transition(th, Stage.INQUIRY)
    assert ok is False and "无需迁移" in reason


def test_idempotency():
    th = _thread()
    assert is_duplicate_message(th, "h1") is True
    assert is_duplicate_message(th, "h2") is False


def test_transition_is_pure():
    th = _thread()
    before = th.model_copy(deep=True)
    apply_transition(th, Stage.SAMPLING)
    assert th.stage is Stage.INQUIRY          # 原对象未被修改
    assert before.stage is Stage.INQUIRY