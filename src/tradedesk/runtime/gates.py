# -*- coding: utf-8 -*-
"""人审闸门（PRD 8.5 的 11 条）与闸门令牌服务。

两条铁律
1. Agent 只产出草稿；任何写操作（发送、推进阶段）必须先过闸门。
2. 闸门令牌由服务端签发、绑定 thread_id 与 draft_id、一次性消费、不可伪造。
   令牌是"人已经签过核"的凭证，不是"Agent 说自己签过了"。
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional

from ..domain.enums import Actor, EventType, GateId, Intent
from ..domain.models import Draft, Gate, GateToken, ThreadState
from ..store.base import StateStore

# ---- 文本探测（粗粒度但可解释；S4 会换成结构化字段判定） ----
PRICE_PATTERNS = [
    re.compile(r"(?:USD|EUR|\$|€)\s?\d[\d,\.]*", re.I),
    re.compile(r"\d[\d,\.]*\s?(?:usd|dollars|euros?)", re.I),
    re.compile(r"(?:单价|价格|报价|price|precio)\D{0,12}\d"),
]
DELIVERY_PATTERNS = [
    re.compile(r"\d+\s?[-–]\s?\d+\s?(?:天|日|days|días|أيام)", re.I),
    re.compile(r"(?:交期|交货期|lead\s?time|plazo)", re.I),
]
CONTRACT_PATTERNS = [re.compile(r"(?:合同|订单|采购单|contract|purchase\s?order|\bPO\b)", re.I)]
CERT_PATTERNS = [re.compile(r"(?:认证|证书|\bCE\b|\bISO\b|\bCOC\b|certificate|certificado|شهادة)", re.I)]
INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions", re.I),
    re.compile(r"(?:忽略|无视)(上面|以上|之前)", ),
    re.compile(r"(?:你现在是|you\s+are\s+now)\s+(?:a|an|the)\s+\w+", re.I),
    re.compile(r"(?:打印|输出|show|print|reveal)\D{0,10}(?:system\s?prompt|系统提示词)", re.I),
]


def _hit(patterns, text: str) -> bool:
    return any(p.search(text or "") for p in patterns)


def _amount_from_draft(draft: Optional[Draft]) -> float:
    """从草稿中抽取最大金额（用于 GATE-11 阈值判定）。"""
    if draft is None:
        return 0.0
    values = []
    for m in re.finditer(r"(?:USD|EUR|\$|€)\s?(\d[\d,\.]*)", draft.body, re.I):
        try:
            values.append(float(m.group(1).replace(",", "")))
        except ValueError:
            pass
    return max(values) if values else 0.0


class GateRule:
    def __init__(self, gate_id: GateId, title: str, must: bool, desc: str,
                 predicate: Callable[[ThreadState, Optional[Draft]], bool]) -> None:
        self.gate_id = gate_id
        self.title = title
        self.must = must
        self.desc = desc
        self.predicate = predicate


def _build_rules(low_conf_threshold: float, amount_threshold: float) -> List[GateRule]:
    return [
        GateRule(GateId.PRICE, "价格承诺", True, "草稿含价格/折扣/底价",
                 lambda t, d: bool(d) and _hit(PRICE_PATTERNS, d.body)),
        GateRule(GateId.DELIVERY, "交期承诺", True, "草稿含交期/产能承诺",
                 lambda t, d: bool(d) and _hit(DELIVERY_PATTERNS, d.body)),
        GateRule(GateId.SANCTION, "制裁 / 管制名单命中", True, "命中后默认不放行",
                 lambda t, d: "sanction_hit" in t.status_flags),
        GateRule(GateId.CONTRACT, "合同条款与签署", True, "合同必须人工确认",
                 lambda t, d: (bool(d) and _hit(CONTRACT_PATTERNS, d.body)) or (t.stage.value == "ORDERED" and not t.contract_version)),
        GateRule(GateId.CERT, "认证声明", False, "抽样 + 引用校验",
                 lambda t, d: bool(d) and (_hit(CERT_PATTERNS, d.body) or any(c.type == "证书" for c in d.citations))),
        GateRule(GateId.LOW_CONFIDENCE, "置信度低于阈值", False, f"阈值 {low_conf_threshold}",
                 lambda t, d: bool(d) and d.confidence < low_conf_threshold),
        GateRule(GateId.INJECTION, "Prompt 注入特征", False, "买家消息视为数据，不作指令",
                 lambda t, d: "injection_detected" in t.status_flags or (bool(d) and _hit(INJECTION_PATTERNS, d.body))),
        GateRule(GateId.AMBIGUOUS, "消息歧义", False, "信息不足时追问，不猜测",
                 lambda t, d: bool(d) and d.needs_human_confirmation),
        GateRule(GateId.COMPLAINT, "投诉意图", False, "默认转人工",
                 lambda t, d: Intent.COMPLAINT in t.intent_labels),
        GateRule(GateId.REGULATION, "目的国法规时效冲突", False, "按买家国别 + 当前日期过滤",
                 lambda t, d: "regulation_conflict" in t.status_flags),
        GateRule(GateId.THRESHOLD, "金额阈值 / 敏感客户", False, f"单笔 ≥ {amount_threshold} 需签核",
                 lambda t, d: _amount_from_draft(d) >= amount_threshold),
    ]


def evaluate_gates(thread: ThreadState, draft: Optional[Draft],
                   low_conf_threshold: float = 0.55,
                   amount_threshold: float = 5000.0) -> List[Gate]:
    """评估 11 条闸门，返回全部闸门及是否命中。"""
    rules = _build_rules(low_conf_threshold, amount_threshold)
    out: List[Gate] = []
    for r in rules:
        hit = False
        try:
            hit = bool(r.predicate(thread, draft))
        except Exception:  # 闸门自身异常不得放行
            hit = True
        out.append(Gate(gate_id=r.gate_id, title=r.title, must=r.must, hit=hit, desc=r.desc))
    return out


def blocking_gates(gates: List[Gate]) -> List[Gate]:
    """命中的闸门 = 必须人工处理的闸门。"""
    return [g for g in gates if g.hit]


class GateTokenService:
    """闸门令牌服务：签发 → 消费（一次性）→ 失效。"""

    def __init__(self, store: StateStore, ttl_seconds: int = 900) -> None:
        self.store = store
        self.ttl_seconds = ttl_seconds

    def issue(self, thread_id: str, draft_id: str, gates: List[GateId]) -> GateToken:
        token = GateToken(
            token=secrets.token_urlsafe(24),
            thread_id=thread_id,
            draft_id=draft_id,
            gates=list(gates),
            issued_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=self.ttl_seconds),
        )
        self.store.issue_gate_token(token)
        self.store.append_audit("system", "gate.token.issued", thread_id,
                                {"token": token.token[:8] + "…", "draft_id": draft_id,
                                 "gates": [g.value for g in gates]})
        return token

    def consume(self, token_id: str, thread_id: str, draft_id: str) -> bool:
        ok = self.store.consume_gate_token(token_id, thread_id, draft_id)
        self.store.append_audit(
            "human" if ok else "system",
            "gate.token.consumed" if ok else "write.rejected",
            thread_id,
            {"token": (token_id[:8] + "…") if token_id else None, "draft_id": draft_id, "accepted": ok},
        )
        return ok