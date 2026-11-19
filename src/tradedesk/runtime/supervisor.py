# -*- coding: utf-8 -*-
"""Supervisor —— 调度器（PRD 8.2）：意图路由、任务分发、冲突仲裁、SLA 守护、幂等。

三条不可让步的规则
----------------
1. **风控前置**：Sentinel 先跑，命中高风险就不让 Inquiry 产生任何对外内容（不是事后过滤）。
2. **风控优先**：Inquiry 与 Sentinel 结论冲突时以 Sentinel 为准。
3. **写操作与发送分离**：签核与发送都必须带闸门令牌，令牌一次性、绑定线程与草稿。

本模块不负责 HTTP，HTTP 层在 api/app.py，只做参数搬运与鉴权。
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from ..agents.base import AgentRequest, AgentResult, AgentStatus
from ..agents.registry import Registry
from ..domain.enums import Actor, EventType, GateId, Intent, Language, RiskLevel, Stage
from ..domain.models import Draft, Gate, ThreadEvent, ThreadState
from ..domain.state_machine import TransitionRejected, apply_transition, build_thread
from ..store.base import StateStore
from .gates import GateTokenService, blocking_gates, evaluate_gates
from .handoff import HandoffRejected, build_handoff, validate_handoff

SLA_DEFAULTS = {"first_response_seconds": 300, "quote_seconds": 3600, "followup_seconds": 86400}


class IngestResult(BaseModel):
    """一次来信接入的结果。"""

    thread_id: str
    status: str                      # draft_ready | blocked | need_human | duplicate | handoff_rejected
    message: str = ""
    draft: Optional[Draft] = None
    intent_labels: List[Intent] = Field(default_factory=list)
    gates: List[Gate] = Field(default_factory=list)
    blocking_gates: List[Gate] = Field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.LOW
    evidence: List[Dict[str, Any]] = Field(default_factory=list)
    handoff_ok: bool = True
    handoff_missing: List[str] = Field(default_factory=list)
    duplicate: bool = False
    next_action: str = ""


class Supervisor:
    """调度器：把"来信"变成"可签核的草稿"或"必须人工处理的问题"。"""

    def __init__(self, store: StateStore, registry: Registry,
                 token_service: Optional[GateTokenService] = None,
                 low_confidence_threshold: float = 0.55,
                 amount_threshold: float = 5000.0) -> None:
        self.store = store
        self.registry = registry
        self.tokens = token_service or GateTokenService(store)
        self.low_confidence_threshold = low_confidence_threshold
        self.amount_threshold = amount_threshold
        # 草稿暂存内存（S2 未做草稿持久化；S4 接真检索后改为随事件落库）
        self.drafts: Dict[str, Draft] = {}

    # ------------------------------------------------------------------ 工具
    @staticmethod
    def _hash(message: str, buyer_id: str) -> str:
        return hashlib.sha256(f"{buyer_id}:{message}".encode("utf-8")).hexdigest()[:32]

    def _log(self, thread_id: str, event_type: EventType, payload: Dict[str, Any],
             actor: Actor = Actor.SYSTEM) -> None:
        self.store.append_event(ThreadEvent(thread_id=thread_id, seq=0,
                                           event_type=event_type, actor=actor, payload=payload))

    def _audit(self, actor: str, action: str, target: str, payload: Dict[str, Any]) -> None:
        self.store.append_audit(actor, action, target, payload)

    # ------------------------------------------------------- 1) 意图路由
    def route(self, message: str, language: Language) -> Dict[str, Any]:
        """意图路由：决定这条询盘交给谁处理（PRD 8.2）。

        规则：
        - 投诉类 → 直接转人工（Agent 不自动应答）
        - 售前询盘 → 先 Sentinel 后 Inquiry
        """
        inquiry = self.registry.get("inquiry.reply")
        intents: List[Intent] = []
        if inquiry is not None:
            from ..agents.inquiry import classify_intent
            intents = classify_intent(message)
        if Intent.COMPLAINT in intents:
            return {"target": "human", "reason": "投诉意图，默认转人工", "intents": intents}
        if Intent.PRICE in intents or Intent.SAMPLE in intents or Intent.SPEC in intents:
            return {"target": "inquiry", "reason": "售前询盘，先风控后应答", "intents": intents}
        return {"target": "inquiry", "reason": "非投诉类询盘", "intents": intents}

    # ------------------------------------------------------- 2) 来信接入
    def ingest(self, *, buyer_id: str, message: str, language: Language = Language.EN,
               company: str = "", country: str = "") -> IngestResult:
        """接入一条买家来信。返回草稿、闸门、风险与下一步动作。"""
        message_hash = self._hash(message, buyer_id)

        # 幂等：同一买家同一消息已处理过 → 不重复处理（FR-ORC-05）
        for t in self.store.list_threads(buyer_id=buyer_id):
            if t.last_message_hash == message_hash:
                self._log(t.thread_id, EventType.MESSAGE_DUPLICATE,
                          {"message_hash": message_hash, "stage": t.stage.value})
                return IngestResult(thread_id=t.thread_id, status="duplicate",
                                    message="重复消息，已忽略", duplicate=True,
                                    intent_labels=t.intent_labels)

        decision = self.route(message, language)
        intents = decision["intents"]

        thread = build_thread(thread_id=self._next_thread_id(), buyer_id=buyer_id,
                              language=language, message_hash=message_hash,
                              intent_labels=intents)
        self.store.create_thread(thread)
        self._log(thread.thread_id, EventType.THREAD_CREATED,
                  {"stage": thread.stage.value, "intents": [i.value for i in intents],
                   "message_hash": message_hash})

        customer = self.store.get_customer(buyer_id)
        profile = {"company": company or (customer.company if customer else buyer_id),
                   "country": country or (customer.country if customer else "")}

        # 路由到人工（投诉）
        if decision["target"] == "human":
            thread.status_flags = list(thread.status_flags) + ["complaint"]
            thread.version += 1
            self.store.save_thread(thread)
            self._log(thread.thread_id, EventType.HUMAN_TAKEOVER,
                      {"reason": decision["reason"]}, actor=Actor.SYSTEM)
            self._audit("system", "thread.routed_to_human", thread.thread_id,
                        {"reason": decision["reason"]})
            return IngestResult(thread_id=thread.thread_id, status="need_human",
                                message=decision["reason"], intent_labels=intents,
                                next_action="转人工处理（投诉/歧义），Agent 不自动应答")

        # 风控前置
        sentinel = self.registry.get("sentinel.risk")
        risk_result: Optional[AgentResult] = None
        if sentinel is not None:
            risk_result = sentinel.handle(AgentRequest(
                thread=thread, message=message, language=language,
                context={"customer": profile},
            ))
            self._log(thread.thread_id, EventType.GATE_EVALUATED,
                      {"agent": sentinel.agent_id, "risk_level": risk_result.risk_level.value,
                       "evidence": risk_result.evidence})

        # 风控优先：命中高风险 → 不让 Inquiry 产生任何对外内容
        if risk_result is not None and risk_result.risk_level is RiskLevel.HIGH:
            thread.status_flags = list(thread.status_flags) + ["sanction_hit"]
            thread.version += 1
            self.store.save_thread(thread)
            self._log(thread.thread_id, EventType.DRAFT_GENERATED,
                      {"suppressed": True, "reason": "风控命中，抑制输出"})
            self._audit("sentinel.risk", "risk.blocked", thread.thread_id,
                        {"risk_level": risk_result.risk_level.value,
                         "evidence": risk_result.evidence})
            gates = evaluate_gates(thread, None, self.low_confidence_threshold, self.amount_threshold)
            blocking = blocking_gates(gates)
            return IngestResult(thread_id=thread.thread_id, status="blocked",
                                message="名单命中：未生成任何回复，等待人工判定",
                                gates=gates, blocking_gates=blocking,
                                risk_level=risk_result.risk_level,
                                evidence=risk_result.evidence,
                                intent_labels=intents, next_action=risk_result.next_action)

        # Sentinel → Inquiry 的交接（契约校验）
        handoff_ok, handoff_missing = True, []
        if sentinel is not None:
            try:
                build_handoff(
                    from_agent=sentinel.agent_id, to_agent="inquiry.reply",
                    reason="风控通过，转询盘应答",
                    buyer_profile_digest=f"{profile['company']}（{profile['country']}）",
                    committed_items=thread.committed_items,
                    open_questions=thread.open_questions,
                    deadlines=thread.deadlines,
                    stage=thread.stage,
                    evidence_refs=[str(e.get("条目 ID", "")) for e in risk_result.evidence],
                    risk_level=risk_result.risk_level,
                    language=language,
                    idempotency_key=message_hash,
                    pending_decision="生成回复草稿",
                )
            except HandoffRejected as e:
                handoff_ok, handoff_missing = False, e.missing
                self._log(thread.thread_id, EventType.HANDOFF_REJECTED,
                          {"missing": e.missing, "from": sentinel.agent_id,
                           "to": "inquiry.reply"}, actor=Actor.SYSTEM)
                self._audit("harness", "handoff.rejected", thread.thread_id,
                            {"missing": e.missing})
                return IngestResult(thread_id=thread.thread_id, status="handoff_rejected",
                                    message="交接被拒：缺少必填字段 " + ", ".join(e.missing),
                                    handoff_ok=False, handoff_missing=e.missing,
                                    intent_labels=intents,
                                    next_action="修好交接上下文后再继续")

        # Inquiry 生成草稿
        inquiry = self.registry.get("inquiry.reply")
        if inquiry is None:
            raise RuntimeError("Inquiry 未注册")
        result = inquiry.handle(AgentRequest(thread=thread, message=message,
                                             language=language, context={"customer": profile}))
        draft = result.draft
        assert draft is not None
        self.drafts[thread.thread_id] = draft
        self._log(thread.thread_id, EventType.DRAFT_GENERATED,
                  {"draft_id": draft_id(thread), "intent_labels": [i.value for i in result.intent_labels],
                   "confidence": result.confidence, "citations": len(draft.citations)})

        # 闸门评估（人工签核前）
        gates = evaluate_gates(thread, draft, self.low_confidence_threshold, self.amount_threshold)
        blocking = blocking_gates(gates)
        self._log(thread.thread_id, EventType.GATE_EVALUATED,
                  {"gates": [g.gate_id.value for g in gates],
                   "blocking": [g.gate_id.value for g in blocking]})

        # 低置信 / 歧义 → 需要人工确认
        status = "draft_ready"
        if result.status is AgentStatus.NEED_HUMAN:
            status = "need_human"
            thread.status_flags = list(thread.status_flags) + ["needs_human_confirmation"]
        if blocking:
            thread.status_flags = list(thread.status_flags) + ["awaiting_signoff"]
        thread.intent_labels = result.intent_labels or thread.intent_labels
        thread.version += 1
        self.store.save_thread(thread)

        return IngestResult(
            thread_id=thread.thread_id, status=status,
            message="草稿已生成，等待人工签核" if blocking else "草稿已生成",
            draft=draft, intent_labels=result.intent_labels, gates=gates,
            blocking_gates=blocking,
            risk_level=risk_result.risk_level if risk_result else RiskLevel.LOW,
            evidence=risk_result.evidence if risk_result else [],
            handoff_ok=handoff_ok, handoff_missing=handoff_missing,
            next_action=result.next_action,
        )

    def get_draft(self, thread_id: str) -> Optional[Draft]:
        """取该线程最近一次的草稿。"""
        return self.drafts.get(thread_id)

    # ------------------------------------------------------- 3) 人工签核
    def sign_off(self, *, thread_id: str, draft_id: str, token: str,
                 action: str, reason: str = "", actor: str = "李静") -> Dict[str, Any]:
        """人工签核。**必须带有效闸门令牌**，否则拒绝（写操作与生成分离）。

        action: approve | modify | reject | transfer
        """
        thread = self.store.get_thread(thread_id)
        if thread is None:
            return {"ok": False, "code": "thread_not_found", "message": "线程不存在"}
        if action not in ("approve", "modify", "reject", "transfer"):
            return {"ok": False, "code": "bad_action", "message": "不支持的签核动作"}

        if not self.tokens.consume(token, thread_id, draft_id):
            self._log(thread_id, EventType.WRITE_REJECTED,
                      {"action": action, "reason": "闸门令牌无效/已使用/不匹配"}, actor=Actor.HUMAN)
            return {"ok": False, "code": "gate_token_invalid",
                    "message": "闸门令牌无效、已使用或与线程/草稿不匹配，写操作被拒绝"}

        if action == "approve":
            thread.price_signed_off = True
            thread.delivery_signed_off = True
            thread.status_flags = [f for f in thread.status_flags if f != "awaiting_signoff"]
            self._log(thread_id, EventType.SIGNED_OFF,
                      {"draft_id": draft_id, "action": action, "actor": actor}, actor=Actor.HUMAN)
            self._audit(actor, "signed_off", thread_id, {"draft_id": draft_id, "action": action})
        elif action == "modify":
            thread.price_signed_off = True
            thread.delivery_signed_off = True
            thread.status_flags = [f for f in thread.status_flags if f != "awaiting_signoff"]
            self._log(thread_id, EventType.SIGNED_OFF,
                      {"draft_id": draft_id, "action": action, "actor": actor}, actor=Actor.HUMAN)
            self._audit(actor, "signed_off_modified", thread_id, {"draft_id": draft_id})
        elif action == "reject":
            thread.status_flags = list(thread.status_flags) + ["rejected"]
            self._log(thread_id, EventType.SIGNED_OFF,
                      {"draft_id": draft_id, "action": action, "actor": actor,
                       "reason": reason}, actor=Actor.HUMAN)
            self._audit(actor, "signed_off_rejected", thread_id, {"reason": reason})
        else:  # transfer
            thread.status_flags = list(thread.status_flags) + ["human_takeover"]
            self._log(thread_id, EventType.HUMAN_TAKEOVER,
                      {"draft_id": draft_id, "actor": actor, "reason": reason}, actor=Actor.HUMAN)
            self._audit(actor, "handed_over", thread_id, {"reason": reason})

        thread.version += 1
        self.store.save_thread(thread)
        return {"ok": True, "action": action, "thread_id": thread_id,
                "status_flags": thread.status_flags}

    # ------------------------------------------------------- 4) 发送（写操作）
    def send(self, *, thread_id: str, draft_id: str, token: str,
             actor: str = "李静") -> Dict[str, Any]:
        """发送草稿。**必须带有效闸门令牌**（第二次令牌会被拒 —— 令牌一次性）。"""
        thread = self.store.get_thread(thread_id)
        if thread is None:
            return {"ok": False, "code": "thread_not_found", "message": "线程不存在"}
        if not thread.price_signed_off:
            self._audit(actor, "send.rejected", thread_id,
                        {"reason": "价格未签核", "draft_id": draft_id})
            return {"ok": False, "code": "not_signed_off",
                    "message": "价格/交期尚未签核，禁止外发"}
        if not self.tokens.consume(token, thread_id, draft_id):
            self._log(thread_id, EventType.WRITE_REJECTED,
                      {"action": "send", "reason": "闸门令牌无效/已使用"}, actor=Actor.HUMAN)
            return {"ok": False, "code": "gate_token_invalid",
                    "message": "闸门令牌无效、已使用或与线程/草稿不匹配，发送被拒绝"}
        thread.sent_at = datetime.now(timezone.utc)
        thread.status_flags = list(thread.status_flags) + ["sent"]
        thread.version += 1
        self.store.save_thread(thread)
        self._log(thread_id, EventType.SENT, {"draft_id": draft_id, "actor": actor}, actor=Actor.HUMAN)
        self._audit(actor, "message.sent", thread_id, {"draft_id": draft_id})
        return {"ok": True, "sent_at": thread.sent_at.isoformat(), "thread_id": thread_id}

    # ------------------------------------------------------- 5) 阶段推进
    def advance(self, *, thread_id: str, to_stage: Stage, actor: str = "李静") -> Dict[str, Any]:
        thread = self.store.get_thread(thread_id)
        if thread is None:
            return {"ok": False, "code": "thread_not_found", "message": "线程不存在"}
        try:
            new = apply_transition(thread, to_stage)
        except TransitionRejected as e:
            self._log(thread_id, EventType.TRANSITION_REJECTED,
                      {"to": to_stage.value, "reason": e.message}, actor=Actor.HUMAN)
            return {"ok": False, "code": "invalid_transition", "message": e.message}
        self.store.save_thread(new)
        self._log(thread_id, EventType.STAGE_CHANGED,
                  {"from": thread.stage.value, "to": to_stage.value}, actor=Actor.HUMAN)
        self._audit(actor, "stage.changed", thread_id,
                    {"from": thread.stage.value, "to": to_stage.value})
        return {"ok": True, "stage": new.stage.value}

    # ------------------------------------------------------- 6) SLA 守护
    def sla_scan(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """扫描超时线程：超时的标记并升级（写事件 + 审计）。"""
        now = now or datetime.now(timezone.utc)
        overdue: List[Dict[str, Any]] = []
        for t in self.store.list_threads():
            for d in t.deadlines:
                if d.due_at < now and "sla_overdue" not in t.status_flags:
                    t.status_flags = list(t.status_flags) + ["sla_overdue"]
                    t.version += 1
                    self.store.save_thread(t)
                    self._log(t.thread_id, EventType.STAGE_CHANGED,
                              {"sla_overdue": d.label, "due_at": d.due_at.isoformat()},
                              actor=Actor.SYSTEM)
                    self._audit("system", "sla.escalated", t.thread_id, {"label": d.label})
                    overdue.append({"thread_id": t.thread_id, "label": d.label,
                                    "due_at": d.due_at.isoformat(), "stage": t.stage.value})
        return overdue

    # ------------------------------------------------------- 内部
    def _next_thread_id(self) -> str:
        n = len(self.store.list_threads()) + 1
        return f"T-{2000 + n}"


def draft_id(thread_id: str) -> str:
    """草稿 ID：与线程绑定，便于闸门令牌校验。"""
    return f"DRAFT-{thread_id}"