# -*- coding: utf-8 -*-
"""Handoff 契约：13 个必填字段，缺一即拒。"""
import pytest

from tradedesk.domain.enums import Language, RiskLevel, Stage
from tradedesk.runtime.handoff import (
    REQUIRED_FIELDS,
    HandoffRejected,
    build_handoff,
    validate_handoff,
)


def _kwargs(**kw):
    base = dict(
        from_agent="sentinel.risk", to_agent="inquiry.reply",
        thread_id="T-T1", buyer_id="BY-1", handoff_reason="风控通过",
        buyer_profile_digest="Brasil Distribuidora Ltda（巴西）",
        committed_items=[], open_questions=[], deadlines=[],
        stage=Stage.INQUIRY, evidence_refs=["CN-1"], risk_level=RiskLevel.LOW,
        language=Language.ES, idempotency_key="k1", pending_decision="生成草稿",
    )
    base.update(kw)
    return base


def test_required_fields_count():
    """PRD 8.4 的必填项是 15 个字段（表里 13 行，其中两行各含 2 个字段）。"""
    assert len(REQUIRED_FIELDS) == 15


def test_valid_handoff_passes():
    h = build_handoff(**_kwargs())
    assert validate_handoff(h) == []


@pytest.mark.parametrize("field", ["idempotency_key", "buyer_profile_digest", "pending_decision",
                                     "handoff_reason", "thread_id", "buyer_id"])
def test_blank_string_field_is_rejected(field):
    with pytest.raises(HandoffRejected) as e:
        build_handoff(**_kwargs(**{field: "  "}))
    assert field in e.value.missing


def test_empty_evidence_list_is_allowed():
    """风控无命中时 evidence_refs 为空是合法状态，不该拒绝交接。"""
    h = build_handoff(**_kwargs(evidence_refs=[]))
    assert validate_handoff(h) == []


def test_self_handoff_is_rejected():
    with pytest.raises(HandoffRejected) as e:
        build_handoff(**_kwargs(to_agent="sentinel.risk"))
    assert "from_agent!=to_agent" in e.value.missing