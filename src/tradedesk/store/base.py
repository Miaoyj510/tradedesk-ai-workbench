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
    LLMUsage,
    ThreadEvent,
    ThreadState,
)


def summarize_llm_usage(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把用量行汇总成 PRD 12.4 的成本口径。

    **两个后端共用这一个函数**（SQLite / PostgreSQL 都只取行，汇总在这里做）——
    R17 的根因里有一条就是“同一个指标在两个后端算的不一样”，汇总逻辑不能各写一遍。

    口径：
    - `cost_cny` 是**已知单价的行**的合计；未知单价的行不计入，而是用 `priced_count` 显示覆盖率。
    - 所以 `avg_cost_cny` 是**已计价行**的均值，不是全部行的均值。
    """
    total_cost = 0.0
    priced = 0
    degraded = 0
    tokens = {"prompt_tokens": 0, "completion_tokens": 0,
              "cached_tokens": 0, "total_tokens": 0}
    http_calls = 0
    max_cost: Optional[float] = None
    by_lang: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        for k in tokens:
            tokens[k] += int(r.get(k) or 0)
        http_calls += int(r.get("http_calls") or 0)
        if r.get("degraded"):
            degraded += 1
        cost = r.get("cost_cny")
        lang = str(r.get("lang") or "")
        bucket = by_lang.setdefault(lang, {"count": 0, "total_tokens": 0,
                                           "cost_cny": 0.0, "priced_count": 0})
        bucket["count"] += 1
        bucket["total_tokens"] += int(r.get("total_tokens") or 0)
        if cost is not None:
            c = float(cost)
            total_cost += c
            priced += 1
            bucket["cost_cny"] += c
            bucket["priced_count"] += 1
            max_cost = c if max_cost is None else max(max_cost, c)
    out: Dict[str, Any] = {
        "count": len(rows),
        "prompt_tokens": tokens["prompt_tokens"],
        "completion_tokens": tokens["completion_tokens"],
        "cached_tokens": tokens["cached_tokens"],
        "total_tokens": tokens["total_tokens"],
        "http_calls": http_calls,
        "degraded_count": degraded,
        "priced_count": priced,
        "cost_cny": total_cost if priced else None,
        "avg_cost_cny": (total_cost / priced) if priced else None,
        "max_cost_cny": max_cost,
        "by_lang": by_lang,
    }
    return out


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
    # ---- 向量（S4）：向量独立存表，不与块内容混在一起 ----
    # 块的文本改了就要重新嵌入，向量表单独记录才能“只重算变了的那些块”。
    def upsert_kb_embedding(self, chunk_id: str, kb_version: str, model: str,
                            vector: List[float], content_fp: str = "") -> None: ...
    def list_kb_embedding_meta(self, kb_version: Optional[str] = None) -> List[Tuple[str, str, str]]: ...
    def list_kb_embeddings(self, kb_version: Optional[str] = None) -> List[Tuple[str, str, List[float]]]: ...
    def clear_kb_embeddings(self, kb_version: Optional[str] = None) -> None: ...
    def list_kb_chunks(self, kb_version: Optional[str] = None) -> List[KBChunk]: ...
    def get_kb_chunk(self, chunk_id: str) -> Optional[KBChunk]: ...
    def clear_kb(self) -> None: ...

    # 闸门令牌
    def issue_gate_token(self, token: GateToken) -> None: ...
    def consume_gate_token(self, token_id: str, thread_id: str, draft_id: str) -> bool: ...
    def get_gate_token(self, token_id: str) -> Optional[GateToken]: ...
    def record_llm_usage(self, usage: LLMUsage) -> LLMUsage: ...
    def llm_usage_summary(self, lang: Optional[str] = None,
                          thread_id: Optional[str] = None) -> Dict[str, Any]: ...