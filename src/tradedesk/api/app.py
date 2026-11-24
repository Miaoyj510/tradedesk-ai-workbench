# -*- coding: utf-8 -*-
"""HTTP 接口层（S2）。

只做三件事：参数搬运、鉴权（闸门令牌）、调用 Supervisor。
业务规则一律不写在这里 —— 规则在 domain/runtime 层，便于纯函数测试。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import RedirectResponse
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


class LoginBody(BaseModel):
    """登录请求体。

    长度上限用于防滥用；**下界不在这里校验**——空值统一交给业务层，
    由 login() 返回 `missing_credentials`（401），与"错误凭据"走同一套错误语义。
    """
    username: str = Field(default="", max_length=64)
    password: str = Field(default="", max_length=128)


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


class ReleaseCreateBody(BaseModel):
    """建发布请求体（S6-3）。

    components **只填要改的组件**：没填的沿用当前稳定版，不会被隐性回退。
    审批三件套（审批人 / 变更说明 / 回滚预案）必填，否则服务端直接拒。
    """
    components: Dict[str, str] = Field(default_factory=dict)
    approver: str = ""
    change_notes: str = ""
    rollback_plan: str = ""
    regression_evidence: str = ""
    risk_evidence: str = ""
    pilot_companies: List[str] = Field(default_factory=list)
    langs: List[str] = Field(default_factory=list)
    release_id: str = ""


class ReleaseRollbackBody(BaseModel):
    release_id: str
    category: str


def mount_ui(app: FastAPI) -> bool:
    """把前端原型托管在 `/ui`（S6-2）。

    **为什么要同源**：前端调 `fetch('/workbench')` 是**相对路径**，
    同源才不需要配 apiBase、也不需要跨域 CORS。分开部署时才用 config.js 的 apiBase 指回后端。

    目录不存在时**跳过托管**（而不是启动报错）：API 本身不依赖前端目录，
    只是页面托管失败了一个。
    """
    from pathlib import Path

    from fastapi.staticfiles import StaticFiles
    ui_dir = Path(__file__).resolve().parents[3] / "ui-prototype"
    if not ui_dir.is_dir():
        print(f"[api] 未找到前端目录（{ui_dir}），跳过静态托管（API 仍可用）")
        return False
    app.mount("/ui", StaticFiles(directory=str(ui_dir), html=True), name="ui")

    @app.get("/", include_in_schema=False)
    def _ui_index():
        return RedirectResponse(url="/ui/index.html")

    print(f"[api] 前端已托管：/ui/  归属 {ui_dir}")
    return True


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

    # ------------------------------------------------------------ 登录（S6-4）
    @app.post("/auth/login")
    def auth_login(body: LoginBody) -> Dict[str, Any]:
        """账号密码登录，签发会话令牌。

        失败时统一返回“账号或密码不正确”（不区分账号是否存在）。
        **本地演示形态**：生产账号由 IT 通过 OIDC 提供（PRD D-11）。
        """
        from .auth import AuthError, login
        try:
            return login(body.username, body.password)
        except AuthError as exc:
            raise HTTPException(status_code=401,
                                detail={"code": exc.code, "message": exc.message})

    @app.post("/auth/logout")
    def auth_logout(x_session_token: str = Header(default="")) -> Dict[str, Any]:
        from .auth import logout
        return {"ok": logout(x_session_token)}

    @app.get("/auth/me")
    def auth_me(x_session_token: str = Header(default="")) -> Dict[str, Any]:
        """当前会话的用户（未登录返回 401）。"""
        from .auth import session_user
        user = session_user(x_session_token)
        if user is None:
            raise HTTPException(status_code=401,
                                detail={"code": "not_authenticated", "message": "会话已失效，请重新登录"})
        return {"user": user}

    # ------------------------------------------------------------ 鉴权中间件（S6-4）
    @app.middleware("http")
    async def _session_guard(request, call_next):
        """本地演示鉴权（默认关）。

        为什么用中间件而不是每个端点上加依赖：一个就能统一防住，也不要求每个路由都想一次。
        **默认关**（`TRADEDESK_AUTH_REQUIRED=0`），否则审核与验证脚本会被拒。
        """
        from .auth import auth_required, session_user
        if not auth_required():
            return await call_next(request)
        path = request.url.path
        public = (path in ("/health", "/auth/login", "/auth/roles", "/docs",
                           "/openapi.json", "/redoc", "/")
                  or path.startswith("/ui"))
        if public:
            return await call_next(request)
        token = request.headers.get("X-Session-Token") or ""
        if not session_user(token):
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=401,
                                content={"detail": {"code": "not_authenticated",
                                                   "message": "请先登录"}})
        return await call_next(request)

    # ------------------------------------------------------------ 角色目录（S6-4）
    @app.get("/auth/roles")
    def auth_roles() -> Dict[str, Any]:
        """角色与可见域定义。

        **不是鉴权接口**：返回的是"演示身份可以看到什么"的规则目录，
        本地 Demo 无鉴权（PRD 13.4.11），真实身份在生产环境用 OIDC（D-11）。
        """
        from .auth import role_catalog
        return role_catalog()

    # ------------------------------------------------------------ 发布与灰度（S6-3）
    def _release_service():
        return app.state.ctx.release_service

    def _release_guard(x_session_token: str, body_approver: str = ""):
        """发布写操作的权限与审批人（S6-3 越权闸门）。

        两条规矩：

        1. **审批人不得由请求体自称**。鉴权开启时审批人只能取自会话用户 ——
           否则谁都能自称"经理"把发布批了。
        2. **角色要有 `release` 能力**才能建发布 / 推进档位 / 回滚。

        本地演示默认**不强制鉴权**（没有身份体系）：这时审批人仍须由请求体给出 ——
        宁可让人多写一个字段，也不给一个假的默认审批人。
        """
        from .auth import auth_required, has_capability, session_user
        if not auth_required():
            return (body_approver or "").strip(), None
        user = session_user(x_session_token)
        if user is None:
            return "", HTTPException(
                status_code=401,
                detail={"code": "not_authenticated", "message": "请先登录"})
        if not has_capability(user, "release"):
            return "", HTTPException(
                status_code=403,
                detail={"code": "forbidden",
                        "message": "当前角色没有发布权限（建发布 / 推进档位 / 回滚）"})
        return str(user.get("username") or user.get("name") or ""), None

    def _release_error(exc):
        from ..release.service import http_status_for
        return HTTPException(status_code=http_status_for(exc.code),
                             detail={"code": exc.code, "message": exc.message})

    @app.get("/release/status")
    def release_status() -> Dict[str, Any]:
        """发布台账：基线组件、当前稳定版、全部发布与版本别名。"""
        return _release_service().status()

    @app.get("/release/resolve")
    def release_resolve(company: str = "", lang: str = "") -> Dict[str, Any]:
        """一次询盘实际命中哪一版（按企业分桶，语种可限定）。"""
        eff = _release_service().resolve(company, lang)
        return {"company": company, "lang": lang, "source": eff.source,
                "release_id": eff.release_id, "components": eff.components}

    @app.post("/release")
    def release_create(body: ReleaseCreateBody,
                       x_session_token: str = Header(default="")) -> Dict[str, Any]:
        """建一次灰度发布（审批人 / 变更说明 / 回滚预案必填）。"""
        from ..release.service import ReleaseError
        approver, denied = _release_guard(x_session_token, body.approver)
        if denied is not None:
            raise denied
        try:
            manifest = _release_service().create(
                components=body.components,
                approver=approver,
                change_notes=body.change_notes,
                rollback_plan=body.rollback_plan,
                regression_evidence=body.regression_evidence,
                risk_evidence=body.risk_evidence,
                pilot_companies=body.pilot_companies,
                langs=body.langs,
                release_id=body.release_id or None,
                actor=approver)
        except ReleaseError as exc:
            raise _release_error(exc)
        return manifest.model_dump(mode="json")

    @app.post("/release/{release_id}/advance")
    def release_advance(release_id: str,
                        x_session_token: str = Header(default="")) -> Dict[str, Any]:
        """推进一档（PILOT → P20 → P50 → P100）；观察窗没到会拒绝。"""
        from ..release.service import ReleaseError
        approver, denied = _release_guard(x_session_token)
        if denied is not None:
            raise denied
        try:
            manifest = _release_service().advance(release_id, actor=approver)
        except ReleaseError as exc:
            raise _release_error(exc)
        return manifest.model_dump(mode="json")

    @app.post("/release/rollback")
    def release_rollback(body: ReleaseRollbackBody,
                         x_session_token: str = Header(default="")) -> Dict[str, Any]:
        """按类别回滚：app / agent / knowledge / sanctions 互不影响。"""
        from ..release.service import ReleaseError
        approver, denied = _release_guard(x_session_token)
        if denied is not None:
            raise denied
        try:
            manifest = _release_service().rollback(
                body.release_id, body.category, actor=approver)
        except ReleaseError as exc:
            raise _release_error(exc)
        return manifest.model_dump(mode="json")

    # ------------------------------------------------------------ 前端静态托管（S6-2）
    mount_ui(app)

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