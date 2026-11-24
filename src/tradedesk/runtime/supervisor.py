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
from ..domain.models import Deadline, Draft, Gate, LLMUsage, ThreadEvent, ThreadState
from ..domain.state_machine import TransitionRejected, apply_transition, build_thread
from ..store.base import ConcurrentUpdate, StateStore
from .gates import (
    GateTokenService,
    blocking_gates,
    classify_reject_reason,
    detect_injection,
    evaluate_gates,
)
from .handoff import HandoffRejected, build_handoff, validate_handoff

# SLA 按业务类型可配置（FR-ORC-08：≥ 3 类）
SLA_DEFAULTS = {
    "first_response": 300,    # 首响
    "quote": 3600,            # 报价
    "followup": 86400,        # 跟进
}
# 超时分级升级：业务员 → 经理（FR-ORC-02 / FR-HUM-09）
ESCALATION_CHAIN = ["业务员", "经理"]


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
                 amount_threshold: float = 5000.0,
                 sla_first_response: Optional[int] = None,
                 sla_quote: Optional[int] = None,
                 sla_followup: Optional[int] = None,
                 release_service=None) -> None:
        self.store = store
        self.registry = registry
        self.tokens = token_service or GateTokenService(store)
        self.low_confidence_threshold = low_confidence_threshold
        self.amount_threshold = amount_threshold
        # SLA 按业务类型可配置（FR-ORC-08）：首响 / 报价 / 跟进
        self.sla_first_response = sla_first_response if sla_first_response is not None else SLA_DEFAULTS["first_response"]
        self.sla_quote = sla_quote if sla_quote is not None else SLA_DEFAULTS["quote"]
        self.sla_followup = sla_followup if sla_followup is not None else SLA_DEFAULTS["followup"]
        # 发布/灰度服务（S6-3）：为 None = 不做灰度分流，行为与原来完全一致。
        self.release_service = release_service
        # 草稿暂存内存（S2 未做草稿持久化；S4 接真检索后改为随事件落库）
        self.drafts: Dict[str, Draft] = {}
        # 线程号自增（并发下唯一）
        import threading
        self._id_lock = threading.Lock()
        self._id_counter = 0
        self._id_base = 2000 + len(store.list_threads())

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
        else:
            # 未实现 Inquiry：显式返回"本期未实现"，不静默降级
            return {"target": "not_implemented", "reason": "Inquiry 未注册/未实现", "intents": intents}
        if Intent.COMPLAINT in intents:
            return {"target": "human", "reason": "投诉意图，默认转人工", "intents": intents}
        if Intent.PRICE in intents or Intent.SAMPLE in intents or Intent.SPEC in intents:
            return {"target": "inquiry", "reason": "售前询盘，先风控后应答", "intents": intents}
        return {"target": "inquiry", "reason": "非投诉类询盘", "intents": intents}

    def _sync_customer_profile(self, buyer_id: str, profile: Dict[str, str],
                               thread: ThreadState, message: str) -> None:
        """客户档案跨线程共享：同一买家的所有线程登记在 linked_threads，
        并把历史要点累积进 contact_history_digest（PRD 8.3）。"""
        from ..domain.models import CustomerProfile
        customer = self.store.get_customer(buyer_id)
        if customer is None:
            customer = CustomerProfile(
                buyer_id=buyer_id,
                company=profile.get("company", ""),
                country=profile.get("country", ""),
                contact_history_digest=f"{thread.thread_id}: {message[:80]}",
            )
        else:
            if profile.get("company") and customer.company != profile["company"]:
                customer.company = profile["company"]
            if profile.get("country") and customer.country != profile["country"]:
                customer.country = profile["country"]
            digest = customer.contact_history_digest or ""
            entry = f"{thread.thread_id}: {message[:80]}"
            if entry not in digest:
                customer.contact_history_digest = (digest + " | " + entry).strip(" |")[-2000:]
        if thread.thread_id not in customer.linked_threads:
            customer.linked_threads.append(thread.thread_id)
        customer.last_seen_at = datetime.now(timezone.utc)
        self.store.upsert_customer(customer)

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

        # 线程号由存储层持久发放（多实例/重启都不撞号）
        thread_id = self.store.allocate_thread_id()
        now = datetime.now(timezone.utc)
        thread = build_thread(thread_id=thread_id, buyer_id=buyer_id,
                              language=language, message_hash=message_hash,
                              intent_labels=intents)
        # SLA 时限由 SLA_DEFAULTS 生成（否则 Watchdog 永远扫不到东西）
        thread.deadlines = [
            Deadline(label="首响 SLA", due_at=now + timedelta(seconds=self.sla_first_response), level="ok"),
            Deadline(label="报价 SLA", due_at=now + timedelta(seconds=self.sla_quote), level="ok"),
            Deadline(label="跟进 SLA", due_at=now + timedelta(seconds=self.sla_followup), level="ok"),
        ]
        # 原子建线程：并发重复投递只会建出一条（撞唯一约束即视为重复）
        if not self.store.create_thread_if_absent(thread):
            existing = self.store.get_thread(thread_id)
            self._log(thread_id, EventType.MESSAGE_DUPLICATE,
                      {"message_hash": message_hash, "stage": "INQUIRY"})
            return IngestResult(thread_id=thread_id, status="duplicate",
                                message="重复消息，已忽略", duplicate=True,
                                intent_labels=intents)
        self._log(thread.thread_id, EventType.THREAD_CREATED,
                  {"stage": thread.stage.value, "intents": [i.value for i in intents],
                   "message_hash": message_hash})
        # 买家原话留痕（S6-1a）：之前只存了 message_hash（去重用），
        # **原话本身没落库**——而前端要展示“买家原话”（FR-EVA-02）。
        # 静态原型里那一栏是 mock 数据，接真数据时无法显示——这里补上。
        self._log(thread.thread_id, EventType.MESSAGE_RECEIVED,
                  {"message": message, "language": language.value,
                   "company": company or "", "country": country or ""})

        customer = self.store.get_customer(buyer_id)
        profile = {"company": company or (customer.company if customer else buyer_id),
                   "country": country or (customer.country if customer else "")}

        # 客户档案跨线程共享（PRD 8.3）：登记本线程、累积历史要点
        self._sync_customer_profile(buyer_id, profile, thread, message)

        # ---- 灰度分流（S6-3；PRD 13.4.8）----
        # 同一企业必须永远命中同一个版本：分桶是纯函数，不需要存分流表。
        # 没有任何发布记录时 source = "baseline"，**不产生额外事件** ——
        # 没启用灰度的部署，行为与引入灰度之前完全一致。
        effective = None
        if self.release_service is not None:
            try:
                effective = self.release_service.resolve(profile["company"], language.value)
            except Exception as exc:
                print(f"[supervisor] 灰度分流失败，回落稳定版：{exc}")
                effective = None
            if effective is not None and effective.source != "baseline":
                self._log(thread.thread_id, EventType.RELEASE_ASSIGNED,
                          {"release_id": effective.release_id, "source": effective.source,
                           "kb_version": effective.components.get("kb_version", ""),
                           "company": profile["company"], "language": language.value})
                self._audit("system", "release.assign", thread.thread_id,
                            {"release_id": effective.release_id, "source": effective.source,
                             "kb_version": effective.components.get("kb_version", ""),
                             "company": profile["company"]})

        # Prompt 注入检测（PRD 8.9）：买家消息是数据，不是指令
        injection = detect_injection(message)
        if injection:
            thread.status_flags = list(thread.status_flags) + ["injection_detected"]
            thread.version += 1
            self.store.save_thread(thread)
            self._log(thread.thread_id, EventType.HUMAN_TAKEOVER,
                      {"reason": "命中 Prompt 注入特征", "pattern": injection})
            self._audit("system", "security.injection_blocked", thread.thread_id,
                        {"pattern": injection})
            gates = evaluate_gates(thread, None, self.low_confidence_threshold, self.amount_threshold)
            return IngestResult(thread_id=thread.thread_id, status="blocked",
                                message="命中 Prompt 注入特征：已阻断并转人工，未生成任何回复",
                                gates=gates, blocking_gates=blocking_gates(gates),
                                risk_level=RiskLevel.HIGH,
                                evidence=[{"注入特征": injection}],
                                intent_labels=intents,
                                next_action="人工判定（安全记录已留痕）")

        # 路由到"本期未实现"（未注册/仅声明角色）→ 显式返回，不静默降级、也不抛 500
        if decision["target"] == "not_implemented":
            self._log(thread.thread_id, EventType.HUMAN_TAKEOVER,
                      {"reason": decision["reason"]}, actor=Actor.SYSTEM)
            self._audit("system", "route.not_implemented", thread.thread_id,
                        {"reason": decision["reason"]})
            return IngestResult(thread_id=thread.thread_id, status="not_implemented",
                                message="负责该意图的角色本期未实现，已转人工",
                                intent_labels=intents, next_action="转人工处理")

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
            thread.status_flags = list(thread.status_flags) + ["sanction_hit",
                                                             f"risk_{risk_result.risk_level.value}"]
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
                    thread_id=thread.thread_id, buyer_id=thread.buyer_id,
                    handoff_reason="风控通过，转询盘应答",
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
        # 检索上下文带上**发布版本**（S6-3）：Eva 按版本取检索器，
        # 灰度企业与稳定企业因此可能读到不同的知识库快照（这正是灰度要验的东西）。
        inquiry_context: Dict[str, Any] = {"customer": profile}
        if effective is not None:
            kb_version = str(effective.components.get("kb_version") or "")
            if kb_version:
                inquiry_context["kb_version"] = kb_version
        result = inquiry.handle(AgentRequest(thread=thread, message=message,
                                             language=language, context=inquiry_context))
        draft = result.draft
        assert draft is not None
        self.drafts[thread.thread_id] = draft
        self._log(thread.thread_id, EventType.DRAFT_GENERATED,
                  {"draft_id": draft_id(thread), "intent_labels": [i.value for i in result.intent_labels],
                   "confidence": result.confidence, "citations": len(draft.citations),
                   # 草稿随事件落库：进程重启后签核界面不能是空的（FR-ORC-04）
                   "draft": draft.model_dump(mode="json"),
                   # 用模型路径（S5-1a）：哪个模型出的稿、花了多少 token / 多少次调用
                   # —— 成本口径的基础数据必须在事件流里查得到，不能只在内存里。
                   "model": draft.model,
                   "llm_usage": result.llm_usage}, actor=Actor.AGENT)

        # 成本落库（S5-1b）：每次云端调用一行。
        # 为什么不只放事件表：成本要能按**语种 / 时间区间 / 线程**聚合，
        # 事件表是按线程顺序存的，拿它做聚合不合适且会被保留期清理。
        if result.llm_usage:
            try:
                self.store.record_llm_usage(LLMUsage(
                    thread_id=thread.thread_id,
                    draft_id=draft_id(thread),
                    lang=draft.lang.value,
                    provider=str(result.llm_usage.get("provider") or ""),
                    model=str(result.llm_usage.get("model") or ""),
                    prompt_tokens=int(result.llm_usage.get("prompt_tokens") or 0),
                    completion_tokens=int(result.llm_usage.get("completion_tokens") or 0),
                    cached_tokens=int(result.llm_usage.get("cached_tokens") or 0),
                    total_tokens=int(result.llm_usage.get("total_tokens") or 0),
                    http_calls=int(result.llm_usage.get("calls") or 0),
                    cost_cny=result.llm_usage.get("cost_cny"),
                    price_tier=str(result.llm_usage.get("price_tier") or ""),
                    degraded=bool(result.llm_usage.get("degraded")),
                ))
            except Exception as exc:  # 记账失败不得阻断主流程
                print(f"[supervisor] 成本记账失败（不影响草稿）：{exc}")
            if result.llm_usage.get("degraded"):
                # 模型越权被拒 → 这份稿是模板写的，签核人必须知道。
                thread.status_flags = list(thread.status_flags) + ["llm_degraded"]

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
        risk_tag = f"risk_{risk_result.risk_level.value}" if risk_result else "risk_low"
        thread.status_flags = [f for f in thread.status_flags if not f.startswith("risk_")] + [risk_tag]
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

    def signoff_view(self, thread_id: str) -> Dict[str, Any]:
        """签核界面需要的 5 类要素（FR-HUM-05）：草稿全文、引用证据、风险项、承诺台账、置信度。"""
        thread = self.store.get_thread(thread_id)
        if thread is None:
            return {}
        draft = self.get_draft(thread_id)
        gates = evaluate_gates(thread, draft, self.low_confidence_threshold, self.amount_threshold)
        return {
            "thread_id": thread_id,
            "draft_body": draft.body if draft else None,
            "citations": [c.model_dump(mode="json") for c in draft.citations] if draft else [],
            "risk_items": [{"gate_id": g.gate_id.value, "title": g.title, "hit": g.hit,
                            "must": g.must, "desc": g.desc}
                           for g in gates if g.hit],
            "committed_items": [{"kind": c.kind, "value": c.value} for c in thread.committed_items],
            "open_questions": [{"text": q.text, "raised_by": q.raised_by} for q in thread.open_questions],
            "confidence": draft.confidence if draft else 0.0,
            "gates": [g.model_dump(mode="json") for g in gates],
            "status_flags": thread.status_flags,
        }

    def get_draft(self, thread_id: str) -> Optional[Draft]:
        """取该线程最近一次的草稿。

        优先从事件日志读（跨进程/重启后仍在），内存只是加速缓存。
        """
        cached = self.drafts.get(thread_id)
        if cached is not None:
            return cached
        for e in reversed(self.store.list_events(thread_id)):
            if e.event_type.value == "draft.generated" and e.payload.get("draft"):
                draft = Draft.model_validate(e.payload["draft"])
                self.drafts[thread_id] = draft
                return draft
        return None

    # ------------------------------------------------------- 3) 人工签核
    def sign_off(self, *, thread_id: str, draft_id: str, token: str,
                 action: str, reason: str = "", actor: str = "李静",
                 body: Optional[str] = None, terms: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """人工签核。**必须带有效闸门令牌**，否则拒绝（写操作与生成分离）。

        action: approve | modify | reject | transfer
        """
        thread = self.store.get_thread(thread_id)
        if thread is None:
            return {"ok": False, "code": "thread_not_found", "message": "线程不存在"}
        if action not in ("approve", "modify", "reject", "transfer"):
            return {"ok": False, "code": "bad_action", "message": "不支持的签核动作"}

        if not self.tokens.consume(token, thread_id, draft_id, action="sign_off"):
            self._log(thread_id, EventType.WRITE_REJECTED,
                      {"action": action, "reason": "闸门令牌无效/已使用/不匹配"}, actor=Actor.HUMAN)
            return {"ok": False, "code": "gate_token_invalid",
                    "message": "闸门令牌无效、已使用或与线程/草稿不匹配，写操作被拒绝"}

        # 价格条款：签核留痕必须含单价/币种/贸易术语/有效期/阶梯量 + 条款版本（PRD 8.5 GATE-01）
        terms_payload = self._extract_terms(thread_id, body=body, extra=terms)

        if action == "approve":
            thread.price_signed_off = True
            thread.delivery_signed_off = True
            thread.status_flags = [f for f in thread.status_flags if f != "awaiting_signoff"]
            self._log(thread_id, EventType.SIGNED_OFF,
                      {"draft_id": draft_id, "action": action, "actor": actor,
                       **terms_payload}, actor=Actor.HUMAN)
            self._audit(actor, "signed_off", thread_id,
                        {"draft_id": draft_id, "action": action, **terms_payload})
        elif action == "modify":
            # "修改后通过"应当承载修改内容（FR-HUM-06）：带了正文就落为新的草稿版本；
            # 没带则放行但留痕说明"未提供修改内容"，避免人改的东西被静默丢弃却毫无痕迹
            modified = bool(body and body.strip())
            thread.price_signed_off = True
            thread.delivery_signed_off = True
            thread.status_flags = [f for f in thread.status_flags if f != "awaiting_signoff"]
            draft = self.get_draft(thread_id)
            if modified and draft is not None:
                draft.body = body
                self.drafts[thread_id] = draft
                self._log(thread_id, EventType.DRAFT_GENERATED,
                          {"draft_id": draft_id, "modified": True, "confidence": draft.confidence,
                           "citations": len(draft.citations),
                           "draft": draft.model_dump(mode="json")}, actor=Actor.HUMAN)
            self._log(thread_id, EventType.SIGNED_OFF,
                      {"draft_id": draft_id, "action": action, "actor": actor,
                       "modified": modified,
                       "note": None if modified else "未提供修改内容，按原草稿签核",
                       **terms_payload}, actor=Actor.HUMAN)
            self._audit(actor, "signed_off_modified", thread_id,
                        {"draft_id": draft_id, "modified": modified,
                         "note": None if modified else "未提供修改内容，按原草稿签核",
                         **terms_payload})
        elif action == "reject":
            # 驳回必须带原因，并归类回灌给 Agent（FR-HUM-06 / FR-HUM-07）
            try:
                reason_class = classify_reject_reason(reason)
            except ValueError as e:
                return {"ok": False, "code": "reject_reason_required", "message": str(e)}
            thread.status_flags = list(thread.status_flags) + ["rejected", f"reject:{reason_class}"]
            self._log(thread_id, EventType.SIGNED_OFF,
                      {"draft_id": draft_id, "action": action, "actor": actor,
                       "reason": reason, "reason_class": reason_class}, actor=Actor.HUMAN)
            self._audit(actor, "signed_off_rejected", thread_id,
                        {"reason": reason, "reason_class": reason_class})
            # 回灌：把驳回原因写进线程上下文，下一轮草稿能看到
            thread.pending_action = {"kind": "rejected", "reason": reason,
                                     "reason_class": reason_class,
                                     "at": datetime.now(timezone.utc).isoformat()}
        else:  # transfer
            thread.status_flags = list(thread.status_flags) + ["human_takeover"]
            self._log(thread_id, EventType.HUMAN_TAKEOVER,
                      {"draft_id": draft_id, "actor": actor, "reason": reason}, actor=Actor.HUMAN)
            self._audit(actor, "handed_over", thread_id, {"reason": reason})

        thread.last_activity_at = datetime.now(timezone.utc)
        thread.version += 1
        try:
            self.store.save_thread(thread)
        except ConcurrentUpdate as e:
            return {"ok": False, "code": "concurrent_update", "message": str(e)}
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
        if not self.tokens.consume(token, thread_id, draft_id, action="send_draft"):
            self._log(thread_id, EventType.WRITE_REJECTED,
                      {"action": "send", "reason": "闸门令牌无效/已使用"}, actor=Actor.HUMAN)
            return {"ok": False, "code": "gate_token_invalid",
                    "message": "闸门令牌无效、已使用或与线程/草稿不匹配，发送被拒绝"}
        now = datetime.now(timezone.utc)
        thread.sent_at = now
        thread.last_activity_at = now
        thread.status_flags = list(thread.status_flags) + ["sent"]
        thread.version += 1
        try:
            self.store.save_thread(thread)
        except ConcurrentUpdate as e:
            return {"ok": False, "code": "concurrent_update", "message": str(e)}
        self._log(thread_id, EventType.SENT, {"draft_id": draft_id, "actor": actor}, actor=Actor.HUMAN)
        self._audit(actor, "message.sent", thread_id, {"draft_id": draft_id})
        return {"ok": True, "sent_at": thread.sent_at.isoformat(), "thread_id": thread_id}

    def _extract_terms(self, thread_id: str, body: Optional[str] = None,
                       extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """签核留痕要记全价格要素与条款版本（PRD 8.5 GATE-01）。"""
        import re
        text = body or ""
        if not text:
            draft = self.get_draft(thread_id)
            text = draft.body if draft else ""
        price = None
        m = re.search(r"(?:USD|EUR|\$|€)\s?(\d[\d,\.]*)", text)
        if m:
            try:
                price = float(m.group(1).replace(",", ""))
            except ValueError:
                price = None
        terms = {
            "unit_price": price,
            "currency": "USD" if re.search(r"USD", text, re.I) else None,
            "terms": "FOB" if re.search(r"FOB", text, re.I) else None,
            "valid_until": None,
            "tier": None,
            "clause_version": "v1",
        }
        # 草稿出处与成本（S5-1b）：签核留痕要知道**这份稿是谁写的**。
        # 不记的话：一条“人工签核过的报价”留痕里看不出签核时那份稿是模板还是模型写的，
        # 就无法回溯“这份稿的质量问题是模型的还是模板的”。
        draft = self.get_draft(thread_id)
        if draft is not None:
            terms["draft_model"] = draft.model
            usage = draft.llm_usage or {}
            if usage.get("cost_cny") is not None:
                terms["llm_cost_cny"] = usage.get("cost_cny")
            if usage.get("degraded"):
                terms["llm_degraded"] = True
        if extra:
            terms.update({k: v for k, v in extra.items() if v is not None})
        return terms

    # ------------------------------------------------------- 5) 阶段推进
    def advance(self, *, thread_id: str, to_stage: Stage, token: str = "",
                actor: str = "李静") -> Dict[str, Any]:
        thread = self.store.get_thread(thread_id)
        if thread is None:
            return {"ok": False, "code": "thread_not_found", "message": "线程不存在"}
        # 推进阶段是写操作：必须带闸门令牌（PRD 8.9 / FR-ORC-09）
        if not token:
            self._audit(actor, "write.rejected", thread_id,
                        {"reason": "推进阶段缺少闸门令牌", "action": "advance_stage"})
            return {"ok": False, "code": "gate_token_required",
                    "message": "推进阶段必须携带闸门令牌（action=advance_stage）"}
        if not self.tokens.consume(token, thread_id, draft_id(thread_id), action="advance_stage"):
            self._log(thread_id, EventType.WRITE_REJECTED,
                      {"action": "advance_stage", "reason": "闸门令牌无效/已使用/不匹配"}, actor=Actor.HUMAN)
            return {"ok": False, "code": "gate_token_invalid",
                    "message": "闸门令牌无效、已使用或与线程/草稿不匹配，推进被拒绝"}
        try:
            new = apply_transition(thread, to_stage)
        except TransitionRejected as e:
            self._log(thread_id, EventType.TRANSITION_REJECTED,
                      {"to": to_stage.value, "reason": e.message}, actor=Actor.HUMAN)
            return {"ok": False, "code": "invalid_transition", "message": e.message}
        new.last_activity_at = datetime.now(timezone.utc)
        try:
            self.store.save_thread(new)
        except ConcurrentUpdate as e:
            return {"ok": False, "code": "concurrent_update", "message": str(e)}
        self._log(thread_id, EventType.STAGE_CHANGED,
                  {"from": thread.stage.value, "to": to_stage.value}, actor=Actor.HUMAN)
        self._audit(actor, "stage.changed", thread_id,
                    {"from": thread.stage.value, "to": to_stage.value})
        return {"ok": True, "stage": new.stage.value}

    # ------------------------------------------------------- 6) SLA 守护
    def sla_scan(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """扫描超时线程：标记 + **分级升级**（业务员 → 经理），写事件与审计。"""
        now = now or datetime.now(timezone.utc)
        overdue: List[Dict[str, Any]] = []
        for t in self.store.list_threads():
            for d in t.deadlines:
                if d.due_at >= now:
                    continue
                flags = list(t.status_flags)
                if "sla_overdue" not in flags:
                    flags.append("sla_overdue")
                # 分级升级：第一次扫描升到业务员，已升级过的再升一级到经理
                level = "业务员"
                if "sla_escalated_manager" in flags:
                    level = "经理"
                elif "sla_escalated_user" in flags:
                    flags.append("sla_escalated_manager")
                    level = "经理"
                else:
                    flags.append("sla_escalated_user")
                t.status_flags = flags
                t.version += 1
                self.store.save_thread(t)
                self._log(t.thread_id, EventType.STAGE_CHANGED,
                          {"sla_overdue": d.label, "due_at": d.due_at.isoformat(),
                           "escalated_to": level}, actor=Actor.SYSTEM)
                self._audit("system", "sla.escalated", t.thread_id,
                            {"label": d.label, "escalated_to": level})
                overdue.append({"thread_id": t.thread_id, "label": d.label,
                                "due_at": d.due_at.isoformat(), "stage": t.stage.value,
                                "escalated_to": level})
        return overdue

    # ------------------------------------------------------- 内部
    def _next_thread_id(self) -> str:
        """兼容旧调用：真正的取号在存储层（持久、多实例安全）。"""
        return self.store.allocate_thread_id()


def draft_id(thread_id: str) -> str:
    """草稿 ID：与线程绑定，便于闸门令牌校验。"""
    return f"DRAFT-{thread_id}"