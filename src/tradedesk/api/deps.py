# -*- coding: utf-8 -*-
"""FastAPI 依赖：存储、注册表、调度器，以及**闸门令牌校验**。

闸门令牌走请求头 `X-Gate-Token`；缺失或无效 → 403，且不产生任何对外动作。
"""
from __future__ import annotations

from typing import Optional

from fastapi import Header, HTTPException, Request

from ..agents.inquiry import InquiryAgent
from ..data.kb_seed import KB_VERSION, load_kb
from ..agents.registry import MERCH_SPEC, GROWTH_SPEC, Registry
from ..agents.sentinel import SentinelAgent
from ..config import Config, get_settings
from ..runtime.gates import GateTokenService
from ..retrieval.store_retriever import StoreRetriever
from ..runtime.supervisor import Supervisor
from ..store.base import StateStore
from ..store.sqlite_store import SqliteStateStore


def build_registry(retriever=None) -> Registry:
    """注册本期两个实现型角色 + 两个仅声明型角色。"""
    reg = Registry()
    reg.register(InquiryAgent(retriever=retriever))
    reg.register(SentinelAgent())
    reg.register_spec(MERCH_SPEC)
    reg.register_spec(GROWTH_SPEC)
    return reg


class AppContext:
    """把依赖装到一个对象里，测试时方便替换。"""

    def __init__(self, store: StateStore, registry: Optional[Registry] = None,
                 config: Optional[Config] = None) -> None:
        self.store = store
        self.config = config or get_settings()
        # 知识库：库里没有或版本对不上时才重新灌种子（生产应走版本化发布，见 PRD 13.4.8）
        self.kb_version = self.config.kb_version or KB_VERSION
        try:
            existing = self.store.list_kb_chunks(self.kb_version)
            if not existing:
                load_kb(self.store, self.kb_version)
        except Exception as exc:
            # 灌种子失败不阻断启动：检索层降级为"空库"，Agent 走低置信兜底
            print(f"[deps] 知识库种子灌入失败（检索将返回空结果）：{exc}")
        self.retriever = StoreRetriever(self.store, self.kb_version)
        self.registry = registry or build_registry(retriever=self.retriever)
        self.tokens = GateTokenService(store, ttl_seconds=self.config.gate_token_ttl_s)
        self.supervisor = Supervisor(
            store=store, registry=self.registry, token_service=self.tokens,
            low_confidence_threshold=self.config.low_confidence_threshold,
            amount_threshold=self.config.amount_threshold_usd,
            # SLA 按业务类型可配置（FR-ORC-08）：首响 / 报价 / 跟进
            sla_first_response=self.config.sla_first_response_s,
            sla_quote=self.config.sla_quote_s,
            sla_followup=self.config.sla_followup_s,
        )


def get_context() -> AppContext:
    """默认上下文：SQLite（本地）。生产形态见 PRD 13.4。"""
    cfg = get_settings()
    store = SqliteStateStore(cfg.db_path)
    return AppContext(store=store, config=cfg)


def make_gate_token_guard(ctx: "AppContext"):
    """闸门令牌守卫工厂。

    没有令牌直接 403（FR-ORC-09 / NFR-SEC-04），**并且必须留痕** ——
    不带令牌的越权尝试恰恰是最该留痕的一类（PRD 11.4「越权尝试」）。
    """

    def require_gate_token(
        x_gate_token: Optional[str] = Header(default=None),
        request: Request = None,  # type: ignore[assignment]
    ) -> str:
        # 路径参数在路由匹配后才有值，从 request 里取（Path 参数不允许默认值）
        thread_id = ""
        try:
            thread_id = request.path_params.get("thread_id", "") if request is not None else ""
        except Exception:
            thread_id = ""
        if not x_gate_token or not x_gate_token.strip():
            try:
                ctx.store.append_audit("system", "write.rejected", thread_id,
                                       {"reason": "缺少闸门令牌", "path_action": "write"})
            except Exception:
                pass
            raise HTTPException(
                status_code=403,
                detail={"code": "gate_token_required",
                        "message": "写操作必须携带闸门令牌（请求头 X-Gate-Token）"},
            )
        return x_gate_token.strip()

    return require_gate_token