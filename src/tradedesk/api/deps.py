# -*- coding: utf-8 -*-
"""FastAPI 依赖：存储、注册表、调度器，以及**闸门令牌校验**。

闸门令牌走请求头 `X-Gate-Token`；缺失或无效 → 403，且不产生任何对外动作。
"""
from __future__ import annotations

from typing import Optional

from fastapi import Header, HTTPException

from ..agents.inquiry import InquiryAgent
from ..agents.registry import MERCH_SPEC, GROWTH_SPEC, Registry
from ..agents.sentinel import SentinelAgent
from ..config import Config, get_settings
from ..runtime.gates import GateTokenService
from ..runtime.supervisor import Supervisor
from ..store.base import StateStore
from ..store.sqlite_store import SqliteStateStore


def build_registry() -> Registry:
    """注册本期两个实现型角色 + 两个仅声明型角色。"""
    reg = Registry()
    reg.register(InquiryAgent())
    reg.register(SentinelAgent())
    reg.register_spec(MERCH_SPEC)
    reg.register_spec(GROWTH_SPEC)
    return reg


class AppContext:
    """把依赖装到一个对象里，测试时方便替换。"""

    def __init__(self, store: StateStore, registry: Optional[Registry] = None,
                 config: Optional[Config] = None) -> None:
        self.store = store
        self.registry = registry or build_registry()
        self.config = config or get_settings()
        self.tokens = GateTokenService(store, ttl_seconds=self.config.gate_token_ttl_s)
        self.supervisor = Supervisor(
            store=store, registry=self.registry, token_service=self.tokens,
            low_confidence_threshold=self.config.low_confidence_threshold,
            amount_threshold=self.config.amount_threshold_usd,
        )


def get_context() -> AppContext:
    """默认上下文：SQLite（本地）。生产形态见 PRD 13.4。"""
    cfg = get_settings()
    store = SqliteStateStore(cfg.db_path)
    return AppContext(store=store, config=cfg)


def require_gate_token(x_gate_token: Optional[str] = Header(default=None)) -> str:
    """写操作的闸门令牌守卫：没有令牌直接 403（FR-ORC-09 / NFR-SEC-04）。"""
    if not x_gate_token or not x_gate_token.strip():
        raise HTTPException(
            status_code=403,
            detail={"code": "gate_token_required",
                    "message": "写操作必须携带闸门令牌（请求头 X-Gate-Token）"},
        )
    return x_gate_token.strip()