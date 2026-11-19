# -*- coding: utf-8 -*-
"""领域模型：线程状态、客户档案、承诺台账、草稿、引用、闸门、闸门令牌、审计。

对应 PRD 8.3（双层状态）与 8.4（Handoff 必填字段）。
所有模型使用 pydantic，便于序列化与校验；不依赖任何外部服务。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .enums import (
    Actor,
    EventType,
    GateId,
    Intent,
    Language,
    RiskLevel,
    Stage,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CommittedItem(BaseModel):
    """已承诺事项：跨天跨周线程里"我们已经答应过什么"的唯一真相。"""

    kind: str                      # price / delivery / sample / cert / other
    value: str
    owner: str = "李静"
    committed_at: datetime = Field(default_factory=utcnow)
    source: str = "human"          # human / agent_draft(未签核)


class OpenQuestion(BaseModel):
    """未决问题：待买家回答或待我方补充。"""

    text: str
    raised_by: str = "agent"       # agent / buyer
    raised_at: datetime = Field(default_factory=utcnow)


class Deadline(BaseModel):
    """时限：SLA 与业务时限。"""

    label: str
    due_at: datetime
    level: str = "ok"              # ok / warn / over


class Citation(BaseModel):
    """可回指标识：证书编号 / 名单条目 ID / 法规条款号 / SKU 版本。"""

    citation_id: str
    type: str                      # 产品条目 / 证书 / 法规条款 / 术语表 / 名单条目 / 内部记录
    note: str = ""
    valid_until: Optional[datetime] = None
    expired: bool = False


class Handoff(BaseModel):
    """Agent 间交接（PRD 8.4 的必填字段在 Agent 间传递的载体）。

    PRD 8.4 的必填项共 **15 个字段**（表里 13 行，其中两行各含 2 个字段）：
    from_agent / to_agent / thread_id / buyer_id / handoff_reason /
    buyer_profile_digest / committed_items / open_questions / deadlines /
    stage / evidence_refs / risk_level / language / idempotency_key / pending_decision
    """

    from_agent: str
    to_agent: str
    thread_id: str = ""
    buyer_id: str = ""
    handoff_reason: str = ""
    buyer_profile_digest: str = ""
    committed_items: List[CommittedItem] = Field(default_factory=list)
    open_questions: List[OpenQuestion] = Field(default_factory=list)
    deadlines: List[Deadline] = Field(default_factory=list)
    stage: Stage
    evidence_refs: List[str] = Field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.LOW
    language: Language
    idempotency_key: str
    pending_decision: str
    handed_off_at: datetime = Field(default_factory=utcnow)

    @property
    def reason(self) -> str:
        """别名：PRD 用 handoff_reason，旧调用用 reason。"""
        return self.handoff_reason


class CustomerProfile(BaseModel):
    """客户档案：跨线程共享（PRD 8.3 双层状态的客户层）。"""

    buyer_id: str
    company: str
    country: str = ""
    contact: str = ""
    risk_flags: List[str] = Field(default_factory=list)
    known_requirements: Dict[str, Any] = Field(default_factory=dict)
    # PRD 8.3：历史沟通摘要（"历史要点"落在它上面）与话术偏好
    contact_history_digest: str = ""
    preference_profile: Dict[str, Any] = Field(default_factory=dict)
    linked_threads: List[str] = Field(default_factory=list)
    last_seen_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ThreadState(BaseModel):
    """线程状态：跨天跨周长事务的唯一真相（PRD 8.3 线程层）。"""

    thread_id: str
    buyer_id: str
    stage: Stage = Stage.INQUIRY
    language: Language = Language.EN
    intent_labels: List[Intent] = Field(default_factory=list)
    committed_items: List[CommittedItem] = Field(default_factory=list)
    open_questions: List[OpenQuestion] = Field(default_factory=list)
    deadlines: List[Deadline] = Field(default_factory=list)
    last_message_hash: str = ""
    pending_action: Dict[str, Any] = Field(default_factory=dict)
    status_flags: List[str] = Field(default_factory=list)
    handoffs: List[Handoff] = Field(default_factory=list)
    contract_version: Optional[str] = None      # 进入 ORDERED 的前置条件
    price_signed_off: bool = False              # 价格是否已签核（未签核不得外发价格）
    delivery_signed_off: bool = False
    sent_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utcnow)
    last_activity_at: datetime = Field(default_factory=utcnow)
    stage_entered_at: datetime = Field(default_factory=utcnow)
    version: int = 1                            # 乐观锁版本号


class ThreadEvent(BaseModel):
    """append-only 事件日志：线程历史的唯一真相，可完整回放。"""

    thread_id: str
    seq: int
    event_type: EventType
    actor: Actor
    payload: Dict[str, Any] = Field(default_factory=dict)
    ts: datetime = Field(default_factory=utcnow)


class Gate(BaseModel):
    """闸门判定结果。"""

    gate_id: GateId
    title: str
    must: bool = False            # 强制（逐条签核）
    hit: bool = False             # 是否命中
    desc: str = ""


class Draft(BaseModel):
    """Inquiry 产出的结构化草稿（PRD FR-INQ-03/04）。"""

    thread_id: str
    lang: Language
    body: str
    intent_labels: List[Intent] = Field(default_factory=list)
    confidence: float = 0.0
    citations: List[Citation] = Field(default_factory=list)
    next_action: str = ""
    needs_human_confirmation: bool = False
    model: str = "stub"


class GateToken(BaseModel):
    """闸门令牌：写操作（签核 / 发送 / 推进阶段）必须携带，服务端签发、一次性消费。

    PRD 8.9「写工具必须绑定闸门 token」的落点：**令牌要写明它授权的是哪个动作**，
    否则一张为"发送"签发的令牌能被拿去推进阶段（FR-ORC-09 的漏洞）。
    """

    token: str
    thread_id: str
    draft_id: str
    action: str = ""          # sign_off / send_draft / advance_stage
    gates: List[GateId]
    issued_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    consumed_at: Optional[datetime] = None


class AuditEntry(BaseModel):
    """审计条目：只追加 + 哈希链，可验伪（PRD 11.4）。"""

    seq: int
    actor: str
    action: str
    target: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    ts: datetime = Field(default_factory=utcnow)
    prev_hash: str = ""
    hash: str = ""