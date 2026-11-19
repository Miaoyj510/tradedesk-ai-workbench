# -*- coding: utf-8 -*-
"""角色注册表（PRD 2.4 / FR-GRC-12）。

本期实现两个角色（Inquiry、Sentinel）；
Merch（商品运营）、Growth（推广）**只做角色声明与工具接口，不做实现**，
被调用时统一返回"本期未实现"，绝不静默降级成别的能力。
"""
from __future__ import annotations

from typing import Dict, List, Optional

from ..domain.agentspec import AgentSpec, AgentStatus, ToolMode, ToolSpec
from .base import AgentRequest, AgentResult, AgentStatus as ResultStatus, BaseAgent

MERCH_SPEC = AgentSpec(
    agent_id="merch.listing",
    name="商品运营 Agent",
    display_name="Merch",
    owner_role="平台运营",
    status=AgentStatus.DECLARED_ONLY,
    version="0.1.0",
    scope_included=["商品运营（Listing 生成与上架）"],
    scope_excluded=["本期不实现任何能力"],
    inputs=["thread", "context"],
    outputs=["suggestion"],
    tools=[
        ToolSpec(name="listing.generate", mode=ToolMode.READ_ONLY, implemented=False),
        ToolSpec(name="listing.publish", mode=ToolMode.WRITE, requires_gate=True, implemented=False),
    ],
    sla={},
    observability=["trace_id", "agent_id"],
)

GROWTH_SPEC = AgentSpec(
    agent_id="growth.campaign",
    name="推广 Agent",
    display_name="Growth",
    owner_role="广告投放",
    status=AgentStatus.DECLARED_ONLY,
    version="0.1.0",
    scope_included=["推广建议（不含自动调预算）"],
    scope_excluded=["本期不实现任何能力", "自动调整预算", "自动投放"],
    inputs=["thread", "context"],
    outputs=["suggestion"],
    tools=[
        ToolSpec(name="campaign.suggest", mode=ToolMode.READ_ONLY, implemented=False),
        ToolSpec(name="campaign.execute", mode=ToolMode.WRITE, requires_gate=True, implemented=False),
    ],
    sla={},
    observability=["trace_id", "agent_id"],
)


class Registry:
    """角色注册表：声明型角色的调用一律返回 not_implemented。"""

    def __init__(self) -> None:
        self._specs: Dict[str, AgentSpec] = {}
        self._agents: Dict[str, BaseAgent] = {}

    def register(self, agent: BaseAgent) -> None:
        problems = agent.spec.validate_spec()
        if problems:
            raise ValueError(f"AgentSpec 不自洽（{agent.agent_id}）：" + "；".join(problems))
        self._specs[agent.agent_id] = agent.spec
        self._agents[agent.agent_id] = agent

    def register_spec(self, spec: AgentSpec) -> None:
        problems = spec.validate_spec()
        if problems:
            raise ValueError(f"AgentSpec 不自洽（{spec.agent_id}）：" + "；".join(problems))
        self._specs[spec.agent_id] = spec

    def get(self, agent_id: str) -> Optional[BaseAgent]:
        return self._agents.get(agent_id)

    def spec(self, agent_id: str) -> Optional[AgentSpec]:
        return self._specs.get(agent_id)

    def specs(self) -> List[AgentSpec]:
        return list(self._specs.values())

    def call_declared(self, agent_id: str, request: AgentRequest) -> AgentResult:
        """调用一个仅声明的角色：明确返回"本期未实现"。"""
        spec = self._specs.get(agent_id)
        if spec is None:
            raise KeyError(f"未注册的角色：{agent_id}")
        return AgentResult(
            agent_id=agent_id,
            status=ResultStatus.NOT_IMPLEMENTED,
            notes=[f"{spec.display_name} 本期仅做角色声明，未实现任何能力"],
            next_action="本期不处理",
        )