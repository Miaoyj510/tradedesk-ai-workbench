# -*- coding: utf-8 -*-
"""演示数据（可复现的固定场景）。

注意：名单条目为**结构化演示数据**，不是真实名单，界面与文档中必须显式标注（PRD 13.4.12）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..domain.enums import Intent, Language
from ..domain.models import (
    CommittedItem,
    CustomerProfile,
    Deadline,
    OpenQuestion,
    ThreadState,
)
from ..store.base import StateStore


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


DEMO_CUSTOMERS = [
    CustomerProfile(
        buyer_id="BY-BR-01", company="Brasil Distribuidora Ltda", country="巴西", contact="Núria Almeida",
        risk_flags=[], known_requirements={"品类": "手动搬运车", "关注": "CE 认证"},
    ),
    CustomerProfile(
        buyer_id="BY-AE-01", company="Gulf Trading Partners LLC", country="阿联酋", contact="خالد الرشيد",
        risk_flags=["sanction_hit"], known_requirements={"品类": "吊装夹具"},
    ),
    CustomerProfile(
        buyer_id="BY-UK-01", company="Meridian Home Goods Ltd", country="英国", contact="Priya Nair",
        risk_flags=[], known_requirements={"品类": "仓储搬运设备"},
    ),
]


def demo_threads() -> list[ThreadState]:
    now = utcnow()
    return [
        ThreadState(
            thread_id="T-1042", buyer_id="BY-BR-01", stage="QUOTING", language=Language.ES,
            intent_labels=[Intent.PRICE, Intent.SPEC],
            committed_items=[
                CommittedItem(kind="sample", value="样品已寄出（SF 7712345665）", committed_at=now - timedelta(days=2)),
                CommittedItem(kind="delivery", value="交期参考 25–30 天（未签核）", source="agent_draft"),
            ],
            open_questions=[
                OpenQuestion(text="买家未确认目的港", raised_by="agent", raised_at=now - timedelta(hours=3)),
                OpenQuestion(text="需确认是否需要第三方检验（SGS）", raised_by="agent", raised_at=now - timedelta(hours=3)),
            ],
            deadlines=[
                Deadline(label="报价 SLA", due_at=now + timedelta(minutes=42), level="warn"),
                Deadline(label="客户跟进", due_at=now + timedelta(hours=8), level="ok"),
            ],
            last_message_hash="sha-demo-1042",
            status_flags=[],
            last_activity_at=now - timedelta(minutes=3),
            stage_entered_at=now - timedelta(days=2),
        ),
        ThreadState(
            thread_id="T-1043", buyer_id="BY-AE-01", stage="INQUIRY", language=Language.AR,
            intent_labels=[Intent.PRICE, Intent.SAMPLE],
            committed_items=[], open_questions=[OpenQuestion(text="等待合规人工判定")],
            deadlines=[Deadline(label="合规复核", due_at=now - timedelta(minutes=3), level="over")],
            last_message_hash="sha-demo-1043",
            status_flags=["sanction_hit"],            # 命中受控条目 → Inquiry 输出被抑制
            last_activity_at=now - timedelta(hours=1),
            stage_entered_at=now - timedelta(hours=1),
        ),
        ThreadState(
            thread_id="T-1046", buyer_id="BY-UK-01", stage="INQUIRY", language=Language.EN,
            intent_labels=[Intent.PRICE],
            committed_items=[], open_questions=[OpenQuestion(text="型号"), OpenQuestion(text="数量")],
            deadlines=[Deadline(label="首次响应 SLA", due_at=now + timedelta(minutes=95), level="warn")],
            last_message_hash="sha-demo-1046",
            status_flags=[],
            last_activity_at=now - timedelta(minutes=14),
            stage_entered_at=now - timedelta(minutes=14),
        ),
    ]


def seed_demo(store: StateStore) -> None:
    """写入演示数据（幂等：同 id 覆盖）。"""
    for c in DEMO_CUSTOMERS:
        store.upsert_customer(c)
    for t in demo_threads():
        if store.get_thread(t.thread_id) is None:
            store.create_thread(t)