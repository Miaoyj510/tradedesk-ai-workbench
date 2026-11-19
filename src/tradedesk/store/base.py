# -*- coding: utf-8 -*-
"""存储接口（PRD 13.4.5 选型轴②/⑤ 的抽象边界）。

当前实现：SQLite（零依赖、单机可跑、可验证）。
后续实现：PostgreSQL + pgvector —— 只要实现同一接口即可替换，业务代码零改动。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol, Tuple, runtime_checkable

from ..domain.models import (
    AuditEntry,
    CustomerProfile,
    GateToken,
    KBChunk,
    ThreadEvent,
    ThreadState,
)


def canonical_ts(dt: datetime) -> str:
    """时间戳的规范表示：两种后端算出的哈希必须一致，因此统一在这里格式化。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def enum_val(x: Any) -> str:
    """枚举 → 它在库里存的那个字符串。

    ⚠ 不要用 `str(枚举)`：Python 3.11 起 `str(Stage.QUOTING)` 是 "Stage.QUOTING"，
    不是 "QUOTING"。拿它去查库会一条都查不到（FR-ORC-10 看板按阶段筛选会静默返回空）。
    """
    return x.value if hasattr(x, "value") else str(x)


def audit_hash(seq: int, actor: str, action: str, target: str, payload: Dict[str, Any],
               ts: datetime, prev_hash: str) -> str:
    """审计条目的哈希：内容 + 前一条哈希 → 链式防篡改。"""
    material = "|".join([
        str(seq), actor, action, target,
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
        canonical_ts(ts), prev_hash,
    ])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class ConcurrentUpdate(RuntimeError):
    """乐观锁冲突：另一个请求刚改过这条数据。

    这是**正常业务状态**（对方先提交了），应该由上层映射成 409 并让调用方重读，
    而不是变成 500。
    """

    def __init__(self, thread_id: str, current, submitted) -> None:
        super().__init__(
            f"并发冲突：thread {thread_id} 当前版本 {current}，提交版本 {submitted}")
        self.thread_id = thread_id


@runtime_checkable
class StateStore(Protocol):
    """线程状态、客户档案、事件日志、审计、闸门令牌的持久化接口。"""

    def init_schema(self) -> None: ...

    # 客户档案
    def upsert_customer(self, profile: CustomerProfile) -> None: ...
    def get_customer(self, buyer_id: str) -> Optional[CustomerProfile]: ...

    # 线程
    def create_thread(self, thread: ThreadState) -> None: ...
    def get_thread(self, thread_id: str) -> Optional[ThreadState]: ...
    def save_thread(self, thread: ThreadState) -> None: ...
    def list_threads(self, **filters: Any) -> List[ThreadState]: ...

    # 事件（append-only）
    def append_event(self, event: ThreadEvent) -> ThreadEvent: ...
    def list_events(self, thread_id: str) -> List[ThreadEvent]: ...

    # 审计（append-only + 哈希链）
    def append_audit(self, actor: str, action: str, target: str, payload: Dict[str, Any]) -> AuditEntry: ...
    def list_audit(self) -> List[AuditEntry]: ...
    def verify_audit_chain(self) -> Tuple[bool, Optional[int]]: ...

    # 知识库
    def upsert_kb_chunk(self, chunk: KBChunk) -> None: ...
    def list_kb_chunks(self, kb_version: Optional[str] = None) -> List[KBChunk]: ...
    def get_kb_chunk(self, chunk_id: str) -> Optional[KBChunk]: ...
    def clear_kb(self) -> None: ...

    # 闸门令牌
    def issue_gate_token(self, token: GateToken) -> None: ...
    def consume_gate_token(self, token_id: str, thread_id: str, draft_id: str) -> bool: ...
    def get_gate_token(self, token_id: str) -> Optional[GateToken]: ...