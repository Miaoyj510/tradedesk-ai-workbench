# -*- coding: utf-8 -*-
"""AgentSpec：把每个 Agent 的能力边界写成可校验的结构（PRD 8.1）。

为什么要它
----------
"Agent 不做什么"必须能被机器检查，而不是写在文档里靠人自觉。
AgentSpec 把 scope.included / scope.excluded / guards / gates 固化下来，
注册表在加载时就校验；写了 excluded 的能力，运行期如果被触发则直接拒绝。
"""
from __future__ import annotations

from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class AgentStatus(str, Enum):
    ACTIVE = "active"                # 本期实现
    DECLARED_ONLY = "declared_only"  # 只声明角色与工具接口，不做实现


class ToolMode(str, Enum):
    READ_ONLY = "read_only"
    WRITE = "write"                  # 写工具必须绑定闸门令牌


class ToolSpec(BaseModel):
    name: str
    mode: ToolMode = ToolMode.READ_ONLY
    requires_gate: bool = False       # 是否需要闸门令牌才能调用
    implemented: bool = True          # 仅声明角色的工具：False


class AgentSpec(BaseModel):
    agent_id: str
    name: str
    display_name: str
    owner_role: str
    status: AgentStatus = AgentStatus.ACTIVE
    version: str = "1.0.0"

    scope_included: List[str] = Field(default_factory=list)
    scope_excluded: List[str] = Field(default_factory=list)

    inputs: List[str] = Field(default_factory=list)
    outputs: List[str] = Field(default_factory=list)

    guards: List[str] = Field(default_factory=list)
    gates: List[str] = Field(default_factory=list)
    tools: List[ToolSpec] = Field(default_factory=list)
    sla: Dict[str, int] = Field(default_factory=dict)
    observability: List[str] = Field(default_factory=list)

    def tool(self, name: str) -> Optional[ToolSpec]:
        for t in self.tools:
            if t.name == name:
                return t
        return None

    def validate_spec(self) -> List[str]:
        """返回问题清单；空列表表示规格自洽。"""
        problems: List[str] = []
        if not self.scope_included:
            problems.append("scope.included 不能为空：Agent 必须写明自己做什么")
        if not self.scope_excluded:
            problems.append("scope.excluded 不能为空：Agent 必须写明自己不做什么")
        overlap = set(self.scope_included) & set(self.scope_excluded)
        if overlap:
            problems.append(f"scope 同时包含又排除：{sorted(overlap)}")
        if self.status is AgentStatus.ACTIVE:
            if not self.inputs or not self.outputs:
                problems.append("实现型 Agent 必须声明 inputs / outputs")
            if not self.guards:
                problems.append("实现型 Agent 必须声明 guards（兜底规则）")
            for t in self.tools:
                if t.mode is ToolMode.WRITE and not t.requires_gate:
                    problems.append(f"写工具 {t.name} 必须要求闸门令牌（FR-ORC-09）")
        else:
            for t in self.tools:
                if t.implemented:
                    problems.append(f"仅声明角色的工具 {t.name} 不应标记为已实现")
        return problems