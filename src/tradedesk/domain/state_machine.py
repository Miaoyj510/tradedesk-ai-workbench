# -*- coding: utf-8 -*-
"""询盘线程状态机（PRD 4.3 / FR-ORC-03）：合法迁移、前置条件、幂等、纯函数推进。

设计要点
1. 状态迁移是**纯函数**：`apply_transition` 不碰 IO，便于穷举测试与回放。
2. 前置条件（guard）写在迁移表里，而不是散落在业务代码：进入 ORDERED 必须有合同版本；
   外发价格必须 price_signed_off。
3. 非法迁移不抛异常给上层业务，而是返回 `(allowed, reason)`，由调用方决定"拒绝并留痕"。
"""
from __future__ import annotations

from typing import Dict, FrozenSet, List, Tuple

from .enums import Stage
from .models import ThreadState

# ---- 合法迁移表（PRD 4.3） ----
TRANSITIONS: Dict[Stage, FrozenSet[Stage]] = {
    Stage.INQUIRY: frozenset({Stage.SAMPLING, Stage.QUOTING, Stage.CLOSED, Stage.LOST}),
    Stage.SAMPLING: frozenset({Stage.QUOTING, Stage.INQUIRY, Stage.CLOSED, Stage.LOST}),
    Stage.QUOTING: frozenset({Stage.SAMPLING, Stage.ORDERED, Stage.CLOSED, Stage.LOST}),
    Stage.ORDERED: frozenset({Stage.DOCS, Stage.PAYMENT, Stage.CLOSED, Stage.LOST}),
    Stage.DOCS: frozenset({Stage.PAYMENT, Stage.ORDERED, Stage.CLOSED, Stage.LOST}),
    Stage.PAYMENT: frozenset({Stage.CLOSED, Stage.LOST}),
    Stage.CLOSED: frozenset(),
    Stage.LOST: frozenset(),
}

TERMINAL_STAGES: FrozenSet[Stage] = frozenset({Stage.CLOSED, Stage.LOST})

# ---- 迁移前置条件 ----
# key: (from, to) -> (说明, 判定函数)
Guard = Tuple[str, str]  # (说明, 判定字段名)


def _has_contract(thread: ThreadState) -> bool:
    return bool(thread.contract_version)


def _price_signed_off(thread: ThreadState) -> bool:
    return thread.price_signed_off


GUARDS: Dict[Tuple[Stage, Stage], Tuple[str, bool]] = {
    (Stage.QUOTING, Stage.ORDERED): ("进入订单前必须确认合同条款（contract_version 非空）", False),
}


class TransitionRejected(Exception):
    """非法迁移。携带原因，供调用方写入事件日志。"""

    def __init__(self, message: str, code: str = "invalid_transition"):
        super().__init__(message)
        self.message = message
        self.code = code


def is_terminal(stage: Stage) -> bool:
    return stage in TERMINAL_STAGES


def allowed_targets(stage: Stage) -> List[Stage]:
    return sorted(TRANSITIONS.get(stage, frozenset()), key=lambda s: s.value)


def check_transition(thread: ThreadState, to_stage: Stage) -> Tuple[bool, str]:
    """判断能否迁移，返回 (是否允许, 原因)。不修改 thread。"""
    if thread.stage == to_stage:
        return False, f"已处于 {to_stage.value}，无需迁移"
    if is_terminal(thread.stage):
        return False, f"{thread.stage.value} 是终态，不可再迁移"
    if to_stage not in TRANSITIONS.get(thread.stage, frozenset()):
        return False, (
            f"非法迁移 {thread.stage.value} → {to_stage.value}；"
            f"合法目标：{[s.value for s in allowed_targets(thread.stage)]}"
        )
    guard = GUARDS.get((thread.stage, to_stage))
    if guard:
        desc, checker = guard
        ok = _has_contract(thread) if checker is False else checker(thread)
        if not ok:
            return False, desc
    return True, "ok"


def apply_transition(thread: ThreadState, to_stage: Stage) -> ThreadState:
    """纯函数推进：返回新的 ThreadState；非法时抛 TransitionRejected。"""
    to_stage = Stage(to_stage)
    ok, reason = check_transition(thread, to_stage)
    if not ok:
        raise TransitionRejected(reason)
    new = thread.model_copy(deep=True)
    new.stage = to_stage
    new.stage_entered_at = thread.last_activity_at
    new.version = thread.version + 1
    return new


def is_duplicate_message(thread: ThreadState, message_hash: str) -> bool:
    """幂等判定：同一 message_hash 重复投递不产生新事件、不重复推进（FR-ORC-05）。"""
    return bool(message_hash) and thread.last_message_hash == message_hash


def build_thread(
    thread_id: str,
    buyer_id: str,
    language,
    message_hash: str = "",
    intent_labels=None,
) -> ThreadState:
    """新建线程：初始状态 INQUIRY，记录首条消息哈希用于后续幂等判定。"""
    return ThreadState(
        thread_id=thread_id,
        buyer_id=buyer_id,
        language=language,
        intent_labels=list(intent_labels or []),
        last_message_hash=message_hash,
    )