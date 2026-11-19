# -*- coding: utf-8 -*-
"""Handoff 契约（PRD 8.4）：Agent 之间交接的 13 个必填字段。

为什么要有契约
--------------
多 Agent 最常见的事故是"上下文在交接时丢了" —— 上一个 Agent 知道的承诺、
未决问题、时限没带过去，下一个 Agent 就开始胡说。
这里把必填项固化成清单，缺一个就**拒绝交接**并记为 Harness 错误，
而不是让它带着残缺上下文继续跑。
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..domain.enums import Language, RiskLevel
from ..domain.models import CommittedItem, Deadline, Handoff, OpenQuestion

REQUIRED_FIELDS: List[str] = [
    "from_agent",
    "to_agent",
    "reason",
    "buyer_profile_digest",
    "committed_items",
    "open_questions",
    "deadlines",
    "stage",
    "evidence_refs",
    "risk_level",
    "language",
    "idempotency_key",
    "pending_decision",
]


class HandoffRejected(Exception):
    """交接被拒：缺必填字段。"""

    def __init__(self, missing: List[str]) -> None:
        super().__init__("交接缺少必填字段：" + ", ".join(missing))
        self.missing = missing


def missing_fields(handoff: Handoff) -> List[str]:
    """返回缺失/不合法的必填字段列表；空列表 = 交接合法。"""
    missing: List[str] = []
    for f in REQUIRED_FIELDS:
        value = getattr(handoff, f, None)
        if value is None:
            missing.append(f)
            continue
        if isinstance(value, str) and not value.strip():
            missing.append(f)
        # 列表型字段（承诺/未决/时限/证据）允许为空 —— "没有证据、没有未决问题"是合法状态，
        # 必填的含义是"必须显式携带该字段"，而不是"必须有内容"
    if handoff.from_agent == handoff.to_agent:
        missing.append("from_agent!=to_agent")
    return missing


def validate_handoff(handoff: Handoff) -> List[str]:
    return missing_fields(handoff)


def build_handoff(
    *,
    from_agent: str,
    to_agent: str,
    reason: str,
    buyer_profile_digest: str,
    committed_items: List[CommittedItem],
    open_questions: List[OpenQuestion],
    deadlines: List[Deadline],
    stage,
    evidence_refs: List[str],
    risk_level: RiskLevel,
    language: Language,
    idempotency_key: str,
    pending_decision: str,
) -> Handoff:
    """构造并校验 Handoff；不合格直接抛 HandoffRejected。"""
    handoff = Handoff(
        from_agent=from_agent,
        to_agent=to_agent,
        reason=reason,
        buyer_profile_digest=buyer_profile_digest,
        committed_items=list(committed_items),
        open_questions=list(open_questions),
        deadlines=list(deadlines),
        stage=stage,
        evidence_refs=list(evidence_refs),
        risk_level=risk_level,
        language=language,
        idempotency_key=idempotency_key,
        pending_decision=pending_decision,
    )
    missing = validate_handoff(handoff)
    if missing:
        raise HandoffRejected(missing)
    return handoff