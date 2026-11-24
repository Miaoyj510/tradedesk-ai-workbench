# -*- coding: utf-8 -*-
"""领域枚举：阶段、风险、意图、语种、闸门、角色。

与 PRD 4.3 / 8.5 一一对应；枚举值一旦发布只增不改（历史数据依赖它们）。
"""
from __future__ import annotations

from enum import Enum


class Stage(str, Enum):
    """询盘线程的 8 个状态（PRD 4.3）。"""

    INQUIRY = "INQUIRY"      # 询盘
    SAMPLING = "SAMPLING"    # 样品
    QUOTING = "QUOTING"      # 报价
    ORDERED = "ORDERED"      # 订单
    DOCS = "DOCS"            # 单证（本期只留状态位）
    PAYMENT = "PAYMENT"      # 收款（本期只留状态位）
    CLOSED = "CLOSED"        # 已关闭
    LOST = "LOST"            # 已流失


class RiskLevel(str, Enum):
    LOW = "low"
    MID = "mid"
    HIGH = "high"


class Intent(str, Enum):
    """询盘五类（PRD 9.6，由 MASSIVE 通用意图重映射而来）。"""

    PRICE = "price"
    SAMPLE = "sample"
    SPEC = "spec"
    LOGISTICS = "logistics"
    COMPLAINT = "complaint"


class Language(str, Enum):
    EN = "en"
    ES = "es"
    AR = "ar"
    ZH = "zh"


class GateId(str, Enum):
    """11 条人审闸门（PRD 8.5）。"""

    PRICE = "GATE-01"          # 价格承诺（强制）
    DELIVERY = "GATE-02"       # 交期承诺（强制）
    SANCTION = "GATE-03"       # 制裁/管制名单命中（强制，默认不放行）
    CONTRACT = "GATE-04"       # 合同条款与签署（强制）
    CERT = "GATE-05"           # 认证声明
    LOW_CONFIDENCE = "GATE-06" # 置信度低于阈值
    INJECTION = "GATE-07"      # Prompt 注入特征
    AMBIGUOUS = "GATE-08"      # 消息歧义（允许追问）
    COMPLAINT = "GATE-09"      # 投诉意图
    REGULATION = "GATE-10"     # 目的国法规时效冲突
    THRESHOLD = "GATE-11"      # 金额阈值 / 敏感客户


class Actor(str, Enum):
    AGENT = "agent"
    HUMAN = "human"
    SYSTEM = "system"


class EventType(str, Enum):
    THREAD_CREATED = "thread.created"
    MESSAGE_RECEIVED = "message.received"
    MESSAGE_DUPLICATE = "message.duplicate"
    STAGE_CHANGED = "stage.changed"
    TRANSITION_REJECTED = "stage.rejected"
    DRAFT_GENERATED = "draft.generated"
    GATE_EVALUATED = "gate.evaluated"
    GATE_TOKEN_ISSUED = "gate.token.issued"
    GATE_TOKEN_CONSUMED = "gate.token.consumed"
    WRITE_REJECTED = "write.rejected"
    HANDOFF_REJECTED = "handoff.rejected"
    SIGNED_OFF = "signed_off"
    SENT = "sent"
    HUMAN_TAKEOVER = "human.takeover"
    RELEASE_ASSIGNED = "release.assigned"   # 本条询盘被分流到哪个发布版本（S6-3）