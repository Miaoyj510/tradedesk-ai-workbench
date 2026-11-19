# -*- coding: utf-8 -*-
"""状态机：合法迁移、前置条件、幂等、终态。"""
from datetime import datetime, timedelta, timezone

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


def test_transition_sets_stage_entered_at_to_transition_time():
    """阶段停留时长要按"进入本阶段的时间"算，不是按线程的最后活动时间。

    旧实现把 stage_entered_at 直接抄成 last_activity_at，而 last_activity_at
    在存储层从不自动推进 —— 于是阶段停留时长永远停在建档那一刻。
    SLA 守护与看板的"等待人审时长"都建在这个字段上。
    """
    th = _thread(Stage.QUOTING)
    th.last_activity_at = datetime.now(timezone.utc) - timedelta(days=5)
    before = datetime.now(timezone.utc)
    new = apply_transition(th, Stage.SAMPLING)
    after = datetime.now(timezone.utc)
    assert before <= new.stage_entered_at <= after
    assert new.stage_entered_at != th.last_activity_at
    assert new.last_activity_at == new.stage_entered_at


def test_transition_accepts_explicit_timestamp():
    """回放时传历史时间戳 → 结果确定可复现（这才是它保持纯函数的用法）。"""
    th = _thread(Stage.QUOTING)
    at = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    new = apply_transition(th, Stage.SAMPLING, at=at)
    assert new.stage_entered_at == at
    assert new.last_activity_at == at
    assert new.stage is Stage.SAMPLING


def test_order_guard_is_contract_only():
    """PRD 4.3：ORDERED 的进入条件只有"合同人工确认"。

    价格 / 交期签核发生在 QUOTING 阶段的外发路径上（GATE-01 / GATE-02），
    不是阶段迁移的前置条件 —— 别把它塞进迁移表。
    """
    th = _thread(Stage.QUOTING)
    assert th.price_signed_off is False and th.delivery_signed_off is False
    ok, _ = check_transition(th, Stage.ORDERED)
    assert ok is False                      # 缺合同版本，仍然拦住
    th.contract_version = "PI-1"
    ok2, _ = check_transition(th, Stage.ORDERED)
    assert ok2 is True                      # 补齐合同即可，不要求价格签核