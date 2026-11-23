# -*- coding: utf-8 -*-
"""HTTP 接口层（S2）。

只做三件事：参数搬运、鉴权（闸门令牌）、调用 Supervisor。
业务规则一律不写在这里 —— 规则在 domain/runtime 层，便于纯函数测试。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

from ..domain.enums import Stage
from ..runtime.supervisor import IngestResult, Supervisor, draft_id
from .deps import AppContext, get_context, make_gate_token_guard

APP_VERSION = "0.2.0-s2"


class InboundMessage(BaseModel):
    buyer_id: str
    message: str
    language: str = "en"
    company: str = ""
    country: str = ""


class SignOffBody(BaseModel):
    draft_id: str
    action: str = Field(pattern="^(approve|modify|reject|transfer)$")
    reason: str = ""
    body: Optional[str] = None      # 『修改后通过』的修改后正文（FR-HUM-06）
    terms: Optional[Dict[str, Any]] = None   # 价格要素（FR-HUM-01 / PRD 8.5 GATE-01）

    def check(self) -> None:
        if self.action == "reject" and not self.reason.strip():
            raise ValueError("驳回必须带原因（FR-HUM-06）")


GATE_ACTIONS = ("sign_off", "send_draft", "advance_stage")


class GateTokenBody(BaseModel):
    draft_id: str
    # 允许空串：空 action 的令牌可以被签发，但它**不能授权任何写操作**
    # （消费时严格比对 token.action != action）—— 对应审核 R9 的 P1-2
    action: str = Field(default="sign_off", pattern="^(sign_off|send_draft|advance_stage|)$")


class SendBody(BaseModel):
    draft_id: str


class AdvanceBody(BaseModel):
    to_stage: str


def create_app(context: Optional[AppContext] = None) -> FastAPI:
    ctx = context or get_context()
    # 闸门令牌守卫绑定到本 app 的存储：拒绝（越权尝试）也要写审计（PRD 11.4）
    require_gate_token = make_gate_token_guard(ctx)
    app = FastAPI(
        title="TradeDesk AI Workbench",
        version=APP_VERSION,
        description="外贸业务员的 AI 数字员工工作台 —— 询盘应答 + 交易风控 + 人工签核闸门",
    )
    app.state.ctx = ctx

    def supervisor() -> Supervisor:
        return app.state.ctx.supervisor

    # ------------------------------------------------------------ 健康检查
    @app.get("/health")
    def health() -> Dict[str, Any]:
        backend = type(app.state.ctx.store).__name__
        return {
            "status": "ok",
            "version": APP_VERSION,
            "backend": backend,
            "agents": [s.agent_id for s in app.state.ctx.registry.specs()],
            "time": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        }

    # ------------------------------------------------------------ 知识库检索
    @app.get("/kb/search")
    def kb_search(q: str, lang: str = "zh", country: str = "",
                  sku: str = "", top_k: int = 5) -> Dict[str, Any]:
        """检索管线查询（演示与联调用）：返回命中、引用标识、命中原因与时效标记。"""
        from ..retrieval.base import SearchQuery
        query = SearchQuery(text=q, lang=lang, buyer_country=country, sku=sku, top_k=top_k)
        hits = app.state.ctx.retriever.search(query)
        return {
            "query": q, "country": country, "kb_version": app.state.ctx.kb_version,
            "hits": [
                {
                    "chunk_id": h.chunk.chunk_id,
                    "doc_id": h.chunk.doc_id,
                    "doc_type": h.chunk.doc_type,
                    "title": h.chunk.title,
                    "citation_id": h.citation_id,
                    "identifiers": h.chunk.identifiers,
                    "score": h.score,
                    "channel": h.channel,
                    "reasons": h.reasons,
                    "expired": h.expired,
                    "not_yet_effective": h.not_yet_effective,
                    "effective_from": h.chunk.effective_from.isoformat() if h.chunk.effective_from else None,
                    "effective_to": h.chunk.effective_to.isoformat() if h.chunk.effective_to else None,
                    "valid_until": h.chunk.valid_until.isoformat() if h.chunk.valid_until else None,
                }
                for h in hits
            ],
        }

    # ------------------------------------------------------------ 角色注册表
    @app.get("/agents")
    def agents() -> List[Dict[str, Any]]:
        out = []
        for s in app.state.ctx.registry.specs():
            out.append({
                "agent_id": s.agent_id,
                "display_name": s.display_name,
                "status": s.status.value,
                "scope_included": s.scope_included,
                "scope_excluded": s.scope_excluded,
                "tools": [{"name": t.name, "mode": t.mode.value, "implemented": t.implemented}
                          for t in s.tools],
            })
        return out

    # ------------------------------------------------------------ 调用仅声明角色
    @app.post("/agents/{agent_id}/invoke")
    def invoke_declared(agent_id: str) -> Dict[str, Any]:
        """调用一个仅声明的角色（Merch / Growth）→ 明确返回"本期未实现"。

        这个端点存在的意义：让"只声明不实现"这件事**可被验证**，
        而不是只写在文档里 —— 调用它不会产生任何副作用。
        """
        from ..agents.base import AgentRequest, AgentStatus as ResultStatus
        from ..domain.enums import Language
        reg = app.state.ctx.registry
        if reg.spec(agent_id) is None:
            raise HTTPException(status_code=404,
                                detail={"code": "unknown_agent", "message": f"未注册的角色：{agent_id}"})
        agent = reg.get(agent_id)
        if agent is not None:
            return {"agent_id": agent_id, "status": "ok",
                    "note": "该角色本期已实现，正常路径请用 /inbound"}
        result = reg.call_declared(agent_id, AgentRequest(
            thread=None, message="", language=Language.EN, context={}))
        return {"agent_id": agent_id, "status": result.status.value,
                "notes": result.notes, "next_action": result.next_action,
                "side_effects": "none"}

    # ------------------------------------------------------------ 线程
    @app.get("/threads")
    def list_threads(stage: Optional[str] = None, buyer_id: Optional[str] = None,
                     lang: Optional[str] = None, risk: Optional[str] = None,
                     waiting_minutes: Optional[int] = None) -> List[Dict[str, Any]]:
        """线程看板：按 阶段 / 语种 / 风险等级 / 等待人审时长 四类筛选（FR-ORC-10）。"""
        items = app.state.ctx.store.list_threads(stage=stage, buyer_id=buyer_id)
        if lang:
            items = [t for t in items if t.language.value == lang]
        if risk:
            items = [t for t in items if f"risk_{risk}" in t.status_flags]
        if waiting_minutes is not None:
            now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
            items = [t for t in items
                     if (now - t.last_activity_at).total_seconds() / 60 >= waiting_minutes]
        return [_thread_view(t) for t in items]

    @app.get("/threads/{thread_id}")
    def get_thread(thread_id: str) -> Dict[str, Any]:
        t = app.state.ctx.store.get_thread(thread_id)
        if t is None:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": "线程不存在"})
        return _thread_view(t)

    @app.get("/threads/{thread_id}/events")
    def thread_events(thread_id: str) -> List[Dict[str, Any]]:
        return [
            {"seq": e.seq, "event_type": e.event_type.value, "actor": e.actor.value,
             "ts": e.ts.isoformat(), "payload": e.payload}
            for e in app.state.ctx.store.list_events(thread_id)
        ]

    # ------------------------------------------------------------ 工作台读模型（S6-1a）
    @app.get("/workbench")
    def workbench(sup: Supervisor = Depends(supervisor)) -> Dict[str, Any]:
        """工作台的聚合读模型（BFF）。

        前端原型只吃一份 mock（`assets/data.js`）。接真数据时若让前端自己拼 N+1 个接口，
        闸门判定会被前端重算一遍（而闸门是后端的领域逻辑）——这里统一组装。
        """
        from .workbench import build_workbench
        return build_workbench(app.state.ctx)

    # ------------------------------------------------------------ 成本口径（PRD 12.3 / 12.4）
    @app.get("/usage/costs")
    def usage_costs(lang: Optional[str] = None,
                    thread_id: Optional[str] = None) -> Dict[str, Any]:
        """按语种 / 线程汇总云端模型用量与成本。

        **口径提示**：`cost_cny` 只累加**单价已核实**的行；未核实的行不计入，
        而是用 `priced_count` 显示覆盖率（如嵌入模型 embo-01 的单价就还没核出来）。
        """
        return app.state.ctx.store.llm_usage_summary(lang=lang, thread_id=thread_id)

    # ------------------------------------------------------------ 来信接入
    @app.post("/inbound")
    def inbound(msg: InboundMessage, sup: Supervisor = Depends(supervisor)) -> Dict[str, Any]:
        from ..domain.enums import Language
        try:
            lang = Language(msg.language)
        except ValueError:
            raise HTTPException(status_code=400,
                                detail={"code": "bad_language", "message": "language 必须是 en/es/ar/zh"})
        result: IngestResult = sup.ingest(
            buyer_id=msg.buyer_id, message=msg.message, language=lang,
            company=msg.company, country=msg.country,
        )
        return _ingest_view(result)

    # ------------------------------------------------------------ 签核详情（5 要素）
    @app.get("/threads/{thread_id}/signoff-view")
    def signoff_view(thread_id: str, sup: Supervisor = Depends(supervisor)) -> Dict[str, Any]:
        """签核界面需要呈现的 5 类要素（FR-HUM-05）。"""
        view = sup.signoff_view(thread_id)
        if not view:
            raise HTTPException(status_code=404,
                                detail={"code": "not_found", "message": "线程不存在"})
        return view

    # ------------------------------------------------------------ 人工签核
    @app.post("/threads/{thread_id}/signoff")
    def sign_off(thread_id: str, body: SignOffBody,
                 token: str = Depends(require_gate_token),
                 sup: Supervisor = Depends(supervisor)) -> Dict[str, Any]:
        try:
            body.check()
        except ValueError as e:
            raise HTTPException(status_code=422,
                                detail={"code": "reject_reason_required", "message": str(e)})
        out = sup.sign_off(thread_id=thread_id, draft_id=body.draft_id, token=token,
                           action=body.action, reason=body.reason,
                           body=body.body, terms=body.terms)
        if not out.get("ok"):
            status = 409 if out.get("code") == "concurrent_update" else 403
            raise HTTPException(status_code=status,
                                detail={"code": out.get("code"), "message": out.get("message")})
        return out

    # ------------------------------------------------------------ 发送
    @app.post("/threads/{thread_id}/send")
    def send(thread_id: str, body: SendBody,
             token: str = Depends(require_gate_token),
             sup: Supervisor = Depends(supervisor)) -> Dict[str, Any]:
        out = sup.send(thread_id=thread_id, draft_id=body.draft_id, token=token)
        if not out.get("ok"):
            raise HTTPException(status_code=403,
                                detail={"code": out.get("code"), "message": out.get("message")})
        return out

    # ------------------------------------------------------------ 阶段推进
    @app.post("/threads/{thread_id}/advance")
    def advance(thread_id: str, body: AdvanceBody,
                token: str = Depends(require_gate_token),
                sup: Supervisor = Depends(supervisor)) -> Dict[str, Any]:
        try:
            to_stage = Stage(body.to_stage)
        except ValueError:
            raise HTTPException(status_code=400,
                                detail={"code": "bad_stage", "message": "未知阶段"})
        out = sup.advance(thread_id=thread_id, to_stage=to_stage, token=token)
        if not out.get("ok"):
            status = 409 if out.get("code") in ("invalid_transition", "concurrent_update") else 403
            raise HTTPException(status_code=status,
                                detail={"code": out.get("code"), "message": out.get("message")})
        return out

    # ------------------------------------------------------------ SLA 扫描
    @app.post("/sla/scan")
    def sla_scan(sup: Supervisor = Depends(supervisor)) -> Dict[str, Any]:
        overdue = sup.sla_scan()
        return {"overdue_count": len(overdue), "overdue": overdue}

    # ------------------------------------------------------------ 闸门令牌签发
    @app.post("/threads/{thread_id}/gate-token")
    def issue_gate_token(thread_id: str, body: GateTokenBody,
                         sup: Supervisor = Depends(supervisor)) -> Dict[str, Any]:
        """签发闸门令牌（演示用：真实产品里这一步发生在人点"签核"时）。"""
        t = app.state.ctx.store.get_thread(thread_id)
        if t is None:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": "线程不存在"})
        draft = sup.get_draft(thread_id)
        gates = []
        if draft is not None:
            from ..runtime.gates import evaluate_gates
            gates = [g.gate_id for g in evaluate_gates(t, draft,
                                                       app.state.ctx.config.low_confidence_threshold,
                                                       app.state.ctx.config.amount_threshold_usd)
                     if g.hit]
        try:
            token = app.state.ctx.tokens.issue(thread_id, body.draft_id, gates, action=body.action)
        except ValueError as e:
            raise HTTPException(status_code=422,
                                detail={"code": "gate_action_required", "message": str(e)})
        return {"token": token.token, "expires_at": token.expires_at.isoformat(),
                "draft_id": body.draft_id}

    return app


def _thread_view(t) -> Dict[str, Any]:
    return {
        "thread_id": t.thread_id,
        "buyer_id": t.buyer_id,
        "stage": t.stage.value,
        "language": t.language.value,
        "intent_labels": [i.value for i in t.intent_labels],
        "status_flags": t.status_flags,
        "committed_items": [{"kind": c.kind, "value": c.value} for c in t.committed_items],
        "open_questions": [{"text": q.text, "raised_by": q.raised_by} for q in t.open_questions],
        "deadlines": [{"label": d.label, "due_at": d.due_at.isoformat(), "level": d.level}
                      for d in t.deadlines],
        "price_signed_off": t.price_signed_off,
        "delivery_signed_off": t.delivery_signed_off,
        "sent_at": t.sent_at.isoformat() if t.sent_at else None,
        "last_activity_at": t.last_activity_at.isoformat(),
        "version": t.version,
    }


def _ingest_view(r: IngestResult) -> Dict[str, Any]:
    return {
        "thread_id": r.thread_id,
        "draft_id": draft_id(r.thread_id),
        "status": r.status,
        "message": r.message,
        "duplicate": r.duplicate,
        "risk_level": r.risk_level.value,
        "intent_labels": [i.value for i in r.intent_labels],
        "draft": (r.draft.model_dump(mode="json") if r.draft else None),
        "gates": [g.model_dump(mode="json") for g in r.gates],
        "blocking_gates": [g.gate_id.value for g in r.blocking_gates],
        "evidence": r.evidence,
        "handoff_ok": r.handoff_ok,
        "handoff_missing": r.handoff_missing,
        "next_action": r.next_action,
    }


# 模块级 app：Dockerfile 的 CMD 与 HEALTHCHECK 都指向 tradedesk.api.app:app
app = create_app()