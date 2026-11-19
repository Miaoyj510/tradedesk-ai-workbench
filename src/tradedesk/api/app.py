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
from .deps import AppContext, get_context, require_gate_token

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


class GateTokenBody(BaseModel):
    draft_id: str


class SendBody(BaseModel):
    draft_id: str


class AdvanceBody(BaseModel):
    to_stage: str


def create_app(context: Optional[AppContext] = None) -> FastAPI:
    ctx = context or get_context()
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
                     lang: Optional[str] = None) -> List[Dict[str, Any]]:
        items = app.state.ctx.store.list_threads(stage=stage, buyer_id=buyer_id)
        if lang:
            items = [t for t in items if t.language.value == lang]
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

    # ------------------------------------------------------------ 人工签核
    @app.post("/threads/{thread_id}/signoff")
    def sign_off(thread_id: str, body: SignOffBody,
                 token: str = Depends(require_gate_token),
                 sup: Supervisor = Depends(supervisor)) -> Dict[str, Any]:
        out = sup.sign_off(thread_id=thread_id, draft_id=body.draft_id, token=token,
                           action=body.action, reason=body.reason)
        if not out.get("ok"):
            raise HTTPException(status_code=403,
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
                sup: Supervisor = Depends(supervisor)) -> Dict[str, Any]:
        try:
            to_stage = Stage(body.to_stage)
        except ValueError:
            raise HTTPException(status_code=400,
                                detail={"code": "bad_stage", "message": "未知阶段"})
        out = sup.advance(thread_id=thread_id, to_stage=to_stage)
        if not out.get("ok"):
            raise HTTPException(status_code=409,
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
        token = app.state.ctx.tokens.issue(thread_id, body.draft_id, gates)
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


app = None  # 由 uvicorn 工厂创建：uvicorn tradedesk.api.app:create_app --factory