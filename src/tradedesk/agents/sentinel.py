# -*- coding: utf-8 -*-
"""Sentinel —— 交易风控 Agent（PRD 7.3 / FR-GRC-01~11）。

职责：名单核验 + 受控国别判定 + 风险分级 + 证据链。
不做：代替人做放行决定（命中后默认阻断，必须人工签核）。

⚠ 本期名单为**结构化演示数据**（data/seed.py），不是真实名单；
  真实名单源需要商用授权（PRD 13.4.12）。界面与文档必须显式标注这一点。
"""
from __future__ import annotations

import difflib
from typing import Dict, List, Optional

from ..data.seed import DEMO_RESTRICTED_COUNTRIES, demo_sanctions
from ..domain.agentspec import AgentSpec, AgentStatus, ToolMode, ToolSpec
from ..domain.enums import RiskLevel
from .base import AgentRequest, AgentResult, AgentStatus as ResultStatus, BaseAgent

SENTINEL_SPEC = AgentSpec(
    agent_id="sentinel.risk",
    name="交易风控 Agent",
    display_name="Sentinel",
    owner_role="风控合规岗",
    status=AgentStatus.ACTIVE,
    version="1.0.0",
    scope_included=["名单核验", "受控国别判定", "风险分级", "证据链输出"],
    scope_excluded=["代替人做放行决定", "生成对外回复", "写操作"],
    inputs=["thread", "message", "context"],
    outputs=["risk_level", "evidence", "notes"],
    guards=["名单服务不可用时转人工，不静默放行", "命中即阻断，不生成任何对外内容"],
    gates=["GATE-03", "GATE-10", "GATE-11"],
    tools=[
        ToolSpec(name="risk.screen_entity", mode=ToolMode.READ_ONLY),
        ToolSpec(name="risk.screen_item", mode=ToolMode.READ_ONLY),
        ToolSpec(name="kb.regulation_query", mode=ToolMode.READ_ONLY),
    ],
    sla={"screening_seconds": 30},
    observability=["trace_id", "thread_id", "risk_level", "evidence", "list_version"],
)

MATCH_THRESHOLD = 0.82


class SentinelAgent(BaseAgent):
    """风控：主体名单 + 受控国别 + 受控物项。"""

    def __init__(self, sanctions: Optional[List[dict]] = None) -> None:
        super().__init__(SENTINEL_SPEC)
        self.sanctions = sanctions if sanctions is not None else demo_sanctions()
        self.restricted_countries = list(DEMO_RESTRICTED_COUNTRIES)

    def _screen_entity(self, company: str) -> Optional[dict]:
        """主体匹配：包含匹配 + 相似度匹配（演示口径）。"""
        target = (company or "").strip().lower()
        if not target:
            return None
        for entry in self.sanctions:
            if entry["name"].lower() == target:
                return {**entry, "matched_field": "公司全称（完全一致）", "similarity": 1.0}
        best = None
        for entry in self.sanctions:
            ratio = difflib.SequenceMatcher(None, target, entry["name"].lower()).ratio()
            if ratio >= MATCH_THRESHOLD and (best is None or ratio > best["similarity"]):
                best = {**entry, "matched_field": "公司名称（模糊匹配）", "similarity": round(ratio, 3)}
        return best

    def handle(self, request: AgentRequest) -> AgentResult:
        company = request.thread.buyer_id
        profile = request.context.get("customer") or {}
        company_name = profile.get("company", company)
        country = profile.get("country", "")

        hit = self._screen_entity(company_name)
        evidence: List[dict] = []
        notes: List[str] = []
        risk = RiskLevel.LOW

        if hit:
            risk = RiskLevel.HIGH
            evidence.append({
                "条目 ID": hit["entry_id"],
                "主体": hit["name"],
                "命中字段": hit["matched_field"],
                "相似度": hit["similarity"],
                "名单类型": hit["type"],
                "名单版本": hit["list_version"],
            })
            notes.append("命中受控名单 → 阻断，不生成任何对外内容")
        elif country and country.upper() in self.restricted_countries:
            risk = RiskLevel.MID
            evidence.append({
                "判定类型": "受控目的国",
                "国别": country.upper(),
                "依据": "演示名单口径（DEMO_RESTRICTED_COUNTRIES）",
            })
            notes.append("目的国属于受控范围 → 强制签核")

        # 受控物项（产品带 controlled 标记）
        for sku in request.context.get("controlled_skus", []) or []:
            risk = max(risk, RiskLevel.MID, key=lambda r: {"low": 0, "mid": 1, "high": 2}[r.value])
            evidence.append({"判定类型": "受控物项", "型号": sku})
            notes.append("涉及受控物项 → 强制签核")

        return AgentResult(
            agent_id=self.agent_id,
            status=ResultStatus.BLOCKED if risk is RiskLevel.HIGH else ResultStatus.OK,
            risk_level=risk, evidence=evidence, confidence=1.0 if evidence else 0.9,
            notes=notes or ["未发现风险信号"],
            next_action=("人工判定（默认不放行）" if risk is RiskLevel.HIGH
                         else ("强制签核" if risk is RiskLevel.MID else "可走常规签核")),
        )