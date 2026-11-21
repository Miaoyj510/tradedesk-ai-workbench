# -*- coding: utf-8 -*-
"""Agent 的统一输入输出契约（PRD 7.3 / 8.2）。

所有 Agent 都返回 AgentResult，用 status 表达"发生了什么"，
由 Supervisor 决定下一步；Agent 自己不发消息、不改线程状态。
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from ..domain.agentspec import AgentSpec
from ..domain.enums import Intent, Language, RiskLevel
from ..domain.models import Citation, Draft, ThreadState


class AgentStatus(str, Enum):
    OK = "ok"                        # 正常产出草稿
    BLOCKED = "blocked"              # 被风控拦截，不产出任何对外内容
    NEED_HUMAN = "need_human"        # 低置信 / 信息不足，需要人工确认或追问
    NOT_IMPLEMENTED = "not_implemented"  # 本期仅声明的角色
    ERROR = "error"


class AgentRequest(BaseModel):
    """Agent 的输入：只给它该看的，不多给（敏感价格按角色裁剪，见 PRD 11.2）。"""

    # 仅声明角色的"调用演示"没有线程，故允许为空；实现型 Agent 由 Supervisor 必传
    thread: Optional[ThreadState] = None
    message: str = ""
    language: Language = Language.EN
    context: Dict[str, Any] = Field(default_factory=dict)


class AgentResult(BaseModel):
    agent_id: str
    status: AgentStatus
    draft: Optional[Draft] = None
    intent_labels: List[Intent] = Field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.LOW
    evidence: List[Dict[str, Any]] = Field(default_factory=list)
    confidence: float = 0.0
    notes: List[str] = Field(default_factory=list)
    next_action: str = ""
    # 云端模型的用量（计费口径的基础）：仅在真模型路径下有值，
    # 模板路径为 None。供成本汇总与 PRD 12.4 的单位成本指标使用。
    llm_usage: Optional[Dict[str, Any]] = None


class BaseAgent:
    """所有 Agent 的基类：持有 spec，并强制"只声明能做什么"。"""

    def __init__(self, spec: AgentSpec) -> None:
        problems = spec.validate_spec()
        if problems:
            raise ValueError(f"AgentSpec 不自洽（{spec.agent_id}）：" + "；".join(problems))
        self.spec = spec

    @property
    def agent_id(self) -> str:
        return self.spec.agent_id

    def handle(self, request: AgentRequest) -> AgentResult:  # pragma: no cover - 抽象
        raise NotImplementedError

    # ---- 供 Supervisor 使用的护栏查询 ----
    def guard_active(self, guard: str) -> bool:
        return guard in self.spec.guards