# -*- coding: utf-8 -*-
"""询盘线程状态机（PRD 4.3 / FR-ORC-03）：合法迁移、前置条件、幂等、纯函数推进。

设计要点
1. 状态迁移是**纯函数**：`apply_transition` 不碰 IO，便于穷举测试与回放。
2. 前置条件（guard）写在迁移表里，而不是散落在业务代码：进入 ORDERED 必须有合同版本。
   价格 / 交期的签核不在这里 —— 那是"外发草稿"路径上的人审闸门（GATE-01 / GATE-02）。
3. 非法迁移不抛异常给上层业务，而是返回 `(allowed, reason)`，由调用方决定"拒绝并留痕"。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Dict, FrozenSet, List, Optional, Tuple

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
Guard = Tuple[str, Callable[[ThreadState], bool]]


def _has_contract(thread: ThreadState) -> bool:
    return bool(thread.contract_version)


# PRD 4.3：ORDERED 的进入条件是"双方确认条款（合同人工确认）"，所以这里的 guard
# 只有合同一条。价格 / 交期的签核**不在阶段迁移上** —— 那是"外发草稿"路径上的
# 人审闸门（GATE-01 / GATE-02，见 runtime/gates.py）。
GUARDS: Dict[Tuple[Stage, Stage], Guard] = {
    (Stage.QUOTING, Stage.ORDERED): (
        "进入订单前必须确认合同条款（contract_version 非空）",
        _has_contract,
    ),
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
        if not checker(thread):
            return False, desc
    return True, "ok"


def apply_transition(thread: ThreadState, to_stage: Stage,
                     at: Optional[datetime] = None) -> ThreadState:
    """纯函数推进：返回新的 ThreadState；非法时抛 TransitionRejected。

    `at` 是本次迁移发生的时间戳，不传就用当前时间。做确定性回放时传历史时间戳，
    结果就可复现 —— 这是它"不读时钟、也不碰 IO"的关键。

    两个时间戳的分工（PRD 8.3）：
    - `stage_entered_at` = 进入当前阶段的时间 → 用来算"阶段停留时长"
    - `last_activity_at` = 最近一次活动时间 → 用来算"跨天断点续传"
    迁移本身就是一次活动，两个一起推进。
    """
    to_stage = Stage(to_stage)
    ok, reason = check_transition(thread, to_stage)
    if not ok:
        raise TransitionRejected(reason)
    moment = at or datetime.now(timezone.utc)
    new = thread.model_copy(deep=True)
    new.stage = to_stage
    new.stage_entered_at = moment
    new.last_activity_at = moment
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