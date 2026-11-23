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


# Prompt 注入特征（PRD 8.9 / FR-EVA-07）：买家消息一律当数据，不得当指令
INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions", re.I),
    re.compile(r"(?:忽略|无视)(上面|以上|之前|前面)", re.I),
    re.compile(r"(?:你现在是|you\s+are\s+now)\s+(?:a|an|the)\s+\w+", re.I),
    re.compile(r"(?:打印|输出|展示|show|print|reveal|list)\D{0,12}(?:system\s?prompt|系统提示词|提示词)", re.I),
    re.compile(r"(?:忽略|跳过)\D{0,8}(?:所有)?(?:规则|限制|闸门|审核|guard)", re.I),
]


def detect_injection(text: str) -> Optional[str]:
    """检测买家消息里的指令型注入；返回命中的特征描述，没有则 None。"""
    for p in INJECTION_PATTERNS:
        m = p.search(text or "")
        if m:
            return m.group(0)
    return None


# ---- 金额抽取（GATE-11）----
# 币种标记。金额可能写在数字**前面**（USD 8000），也可能写在**后面**（8000 USD），
# 两种写法都必须认，否则阈值闸门会被"把币种挪到数字后面"直接绕过。
CURRENCY_TOKEN = r"(?:usd|eur|cny|rmb|gbp|\$|€|£|dollars?|euros?|美元|欧元|元)"

# 数字紧挨币种：USD 8000 / $8,000 / 8000 USD / 8000 dollars / 8000元
AMOUNT_WITH_CURRENCY = re.compile(
    rf"(?:{CURRENCY_TOKEN})\s?(\d[\d,]*(?:\.\d+)?)"
    rf"|(\d[\d,]*(?:\.\d+)?)\s?(?:{CURRENCY_TOKEN})",
    re.I,
)

# 价格语境兜底：语境词后 8 个非数字字符内取最近的数字（总价 8000 / the price is 8000）。
# 窗口刻意收窄 —— "Total quantity 12000 units" 这种不该被当成金额。
AMOUNT_BY_CONTEXT = re.compile(
    r"(?:单价|价格|报价|总价|总额|amount|price|precio|total)\D{0,8}?(\d[\d,]*(?:\.\d+)?)",
    re.I,
)


def _to_float(raw: str) -> Optional[float]:
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def _amount_from_draft(draft: Optional[Draft]) -> float:
    """从草稿中抽取最大金额（用于 GATE-11 阈值判定）。

    拿不准就不算金额：宁可漏判，也不能把"250 天""12000 units"这类数字
    误当金额去卡闸门。已知局限：不同币种按同一阈值比较（阈值是 USD），
    本期不做汇率换算，见 PRD 13.4.11 的生产化清单。
    """
    if draft is None:
        return 0.0
    body = draft.body or ""
    values: List[float] = []
    for m in AMOUNT_WITH_CURRENCY.finditer(body):
        v = _to_float(m.group(1) or m.group(2) or "")
        if v is not None:
            values.append(v)
    for m in AMOUNT_BY_CONTEXT.finditer(body):
        v = _to_float(m.group(1))
        if v is not None:
            values.append(v)
    return max(values) if values else 0.0


# 驳回原因分类（PRD FR-HUM-07：≥ 5 类），驳回时必须选一类，回灌给 Agent
REJECT_REASONS: List[str] = [
    "价格不符",
    "交期不可接受",
    "规格/认证不符",
    "措辞不合适",
    "证据不足需补充",
    "合规存疑",
    "其他（已备注）",
]


def classify_reject_reason(reason: str) -> str:
    """把业务员填的原因归到一类（用于统计与回灌）。"""
    text = (reason or "").strip()
    if not text:
        raise ValueError("驳回必须带原因（FR-HUM-06）")
    for cat in REJECT_REASONS:
        if cat != "其他（已备注）" and cat in text:
            return cat
    if "其他" in text:
        return "其他（已备注）"
    # 没命中预设分类时按关键词兜底
    if any(k in text for k in ("价格", "价", "报价", "折扣")):
        return "价格不符"
    if any(k in text for k in ("交期", "发货", "排产")):
        return "交期不可接受"
    if any(k in text for k in ("规格", "认证", "证书", "型号")):
        return "规格/认证不符"
    if any(k in text for k in ("合规", "名单", "制裁", "管制")):
        return "合规存疑"
    if any(k in text for k in ("证据", "引用", "资料")):
        return "证据不足需补充"
    return "其他（已备注）"


class GateRule:
    def __init__(self, gate_id: GateId, title: str, must: bool, desc: str,
                 predicate: Callable[[ThreadState, Optional[Draft]], bool]) -> None:
        self.gate_id = gate_id
        self.title = title
        self.must = must
        self.desc = desc
        self.predicate = predicate


def _build_rules(low_conf_threshold: float, amount_threshold: float) -> List[GateRule]:
    """11 条闸门规则。

    `must=True` 对应 PRD 8.5 里"强制人工签核"的那 6 条：GATE-01/02/03/04/10/11
    —— 逐条签核、不可批量放行。其余 5 条命中后同样阻断流程，但走的是
    "转人工 / 提示 / 允许追问"的处置方式，不需要逐条签核记录。
    """
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
        GateRule(GateId.REGULATION, "目的国法规时效冲突", True, "按买家国别 + 当前日期过滤",
                 lambda t, d: "regulation_conflict" in t.status_flags),
        GateRule(GateId.THRESHOLD, "金额阈值 / 敏感客户", True, f"单笔 ≥ {amount_threshold} 需签核",
                 lambda t, d: _amount_from_draft(d) >= amount_threshold),
    ]


def gate_catalog(low_conf_threshold: float = 0.55,
                 amount_threshold: float = 5000.0) -> List[Gate]:
    """返回 11 条闸门的**定义**（不含命中状态）。

    给前端的闸门表用：它需要知道“有哪些闸门、哪些是强制项”，
    而不是只看本次命中的那几条。命中数由调用方自己累加。
    """
    rules = _build_rules(low_conf_threshold, amount_threshold)
    return [Gate(gate_id=r.gate_id, title=r.title, must=r.must, hit=False, desc=r.desc)
            for r in rules]


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

    def issue(self, thread_id: str, draft_id: str, gates: List[GateId],
              action: str = "") -> GateToken:

        token = GateToken(
            token=secrets.token_urlsafe(24),
            thread_id=thread_id,
            draft_id=draft_id,
            action=action,
            gates=list(gates),
            issued_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=self.ttl_seconds),
        )
        self.store.issue_gate_token(token)
        self.store.append_audit("system", "gate.token.issued", thread_id,
                                {"token": token.token[:8] + "…", "draft_id": draft_id,
                                 "action": action, "gates": [g.value for g in gates]})
        return token

    def consume(self, token_id: str, thread_id: str, draft_id: str,
                action: str = "") -> bool:
        """消费令牌。**动作必须匹配**：为"发送"签发的令牌不能拿去推进阶段。"""
        token = self.store.get_gate_token(token_id)
        # 不做"空动作短路"：action 为空的令牌是**万能令牌**，必须一律拒绝
        if token is not None and token.action != action:
            self.store.append_audit(
                "system", "write.rejected", thread_id,
                {"reason": "闸门令牌动作不匹配", "token_action": token.action,
                 "required_action": action})
            return False
        ok = self.store.consume_gate_token(token_id, thread_id, draft_id)
        self.store.append_audit(
            "human" if ok else "system",
            "gate.token.consumed" if ok else "write.rejected",
            thread_id,
            {"token": (token_id[:8] + "…") if token_id else None, "draft_id": draft_id, "accepted": ok},
        )
        return ok