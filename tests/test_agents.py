# -*- coding: utf-8 -*-
"""Agent 层：AgentSpec 自洽性、意图分类、证据装配、风控核验、仅声明角色。"""
import pytest

from tradedesk.agents.base import AgentRequest, AgentStatus as ResultStatus
from tradedesk.agents.inquiry import INQUIRY_SPEC, InquiryAgent, classify_intent
from tradedesk.agents.registry import GROWTH_SPEC, MERCH_SPEC, Registry
from tradedesk.agents.sentinel import SENTINEL_SPEC, SentinelAgent
from tradedesk.domain.agentspec import AgentStatus, ToolMode
from tradedesk.domain.enums import Intent, Language, RiskLevel
from tradedesk.domain.models import CustomerProfile, ThreadState


def _thread(**kw):
    base = dict(thread_id="T-A1", buyer_id="BY-BR-01", language=Language.ES)
    base.update(kw)
    return ThreadState(**base)


# ---------------------------- AgentSpec ----------------------------
def test_agentspec_self_check_ok():
    assert INQUIRY_SPEC.validate_spec() == []
    assert SENTINEL_SPEC.validate_spec() == []


def test_agentspec_requires_scope():
    from tradedesk.domain.agentspec import AgentSpec
    bad = AgentSpec(agent_id="x", name="x", display_name="X", owner_role="r",
                    scope_included=[], scope_excluded=[])
    problems = bad.validate_spec()
    assert any("scope.included" in p for p in problems)


def test_agentspec_write_tool_must_require_gate():
    from tradedesk.domain.agentspec import AgentSpec, ToolSpec
    bad = AgentSpec(
        agent_id="x", name="x", display_name="X", owner_role="r",
        scope_included=["a"], scope_excluded=["b"], inputs=["i"], outputs=["o"],
        guards=["g"], tools=[ToolSpec(name="t", mode=ToolMode.WRITE, requires_gate=False)],
    )
    assert any("闸门令牌" in p for p in bad.validate_spec())


def test_declared_only_spec_must_not_implement_tools():
    assert MERCH_SPEC.validate_spec() == []
    assert GROWTH_SPEC.validate_spec() == []
    assert all(not t.implemented for t in MERCH_SPEC.tools)
    assert all(not t.implemented for t in GROWTH_SPEC.tools)


# ---------------------------- Inquiry ----------------------------
@pytest.mark.parametrize("text,expect", [
    ("What is your price for 100 pcs?", Intent.PRICE),
    ("Could you send a sample?", Intent.SAMPLE),
    ("What is the material and capacity?", Intent.SPEC),
    ("How much is the shipping to Rotterdam?", Intent.LOGISTICS),
    ("The goods arrived damaged, I need compensation", Intent.COMPLAINT),
])
def test_intent_classification_multilingual(text, expect):
    assert expect in classify_intent(text)


def test_intent_classification_chinese():
    assert Intent.PRICE in classify_intent("请报一下价格")
    assert Intent.SAMPLE in classify_intent("能寄个样品吗")


def test_inquiry_produces_draft_with_citations():
    agent = InquiryAgent()
    res = agent.handle(AgentRequest(
        thread=_thread(), language=Language.EN,
        message="Quote 5000 pcs of X-200 FOB Shenzhen, do you have CE certification?"))
    assert res.status is ResultStatus.OK
    assert res.draft is not None
    assert res.draft.citations, "草稿必须带引用"
    assert res.confidence >= 0.55


def test_inquiry_missing_product_asks_instead_of_guessing():
    agent = InquiryAgent()
    res = agent.handle(AgentRequest(thread=_thread(), language=Language.EN,
                                    message="Send me your best price."))
    assert res.status is ResultStatus.NEED_HUMAN
    assert res.draft is not None and res.draft.needs_human_confirmation
    assert "USD" not in res.draft.body, "信息不足时不得编造价格"
    assert res.draft.citations == []


def test_inquiry_excludes_pricing_decision():
    assert "报价决策" in INQUIRY_SPEC.scope_excluded
    assert "合同" in INQUIRY_SPEC.scope_excluded


# ---------------------------- Sentinel ----------------------------
def test_sentinel_blocks_on_list_hit():
    agent = SentinelAgent()
    res = agent.handle(AgentRequest(
        thread=_thread(), language=Language.AR,
        context={"customer": {"company": "Gulf Trading Partners LLC", "country": "AE"}}))
    assert res.risk_level is RiskLevel.HIGH
    assert res.status is ResultStatus.BLOCKED
    assert res.evidence and res.evidence[0].get("条目 ID")


def test_sentinel_passes_clean_buyer():
    agent = SentinelAgent()
    res = agent.handle(AgentRequest(
        thread=_thread(), language=Language.EN,
        context={"customer": {"company": "Brasil Distribuidora Ltda", "country": "BR"}}))
    assert res.risk_level is RiskLevel.LOW
    assert res.status is ResultStatus.OK


def test_sentinel_fuzzy_match_is_recorded():
    agent = SentinelAgent()
    res = agent.handle(AgentRequest(
        thread=_thread(), context={"customer": {"company": "Gulf Trading Partner LLC", "country": "AE"}}))
    assert res.risk_level is RiskLevel.HIGH
    assert "模糊匹配" in res.evidence[0]["命中字段"]


# ---------------------------- 注册表 ----------------------------
def test_registry_rejects_inconsistent_spec():
    from tradedesk.domain.agentspec import AgentSpec
    reg = Registry()
    with pytest.raises(ValueError):
        reg.register_spec(AgentSpec(agent_id="x", name="x", display_name="X",
                                    owner_role="r", scope_included=[], scope_excluded=[]))


def test_declared_agent_call_returns_not_implemented():
    reg = Registry()
    reg.register(InquiryAgent())
    reg.register_spec(MERCH_SPEC)
    res = reg.call_declared("merch.listing", AgentRequest(thread=None, message=""))
    assert res.status is ResultStatus.NOT_IMPLEMENTED
    assert res.draft is None