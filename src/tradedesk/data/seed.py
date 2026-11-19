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

# ---------------------------------------------------------------------------
# S2：Inquiry / Sentinel 用的演示知识快照
# ⚠ 名单为**结构化演示数据**，不是真实名单（PRD 13.4.12）。
#    真实名单源需要商用授权，本期不接入。
# ---------------------------------------------------------------------------

DEMO_PRODUCTS = [
    {
        "sku": "X-200",
        "name": {"zh": "手动液压搬运车", "en": "Manual Hydraulic Hand Truck",
                 "es": "Carretilla hidráulica manual", "ar": "عربة يدوية هيدروليكية"},
        "material": "铸铁泵体 + 尼龙轮",
        "capacity": "额定载荷 2 吨",
        "moq": 1000,
        "lead_time_days": "25-30",
        "price_low": 4.85, "price_high": 5.20, "currency": "USD",
        "terms": "FOB",
        "port": "Shenzhen",
        "hs_code": "8479.90",
        "controlled": False,
        "certificates": ["CERT-CE-2041"],
    },
    {
        "sku": "M-77",
        "name": {"zh": "手动吊装夹具", "en": "Manual Lifting Clamp",
                 "es": "Eslinga de elevación manual", "ar": "مشبك رفع يدوي"},
        "material": "铸钢 + 尼龙滚轮钳口",
        "capacity": "安全载荷 500 kg",
        "moq": 100,
        "lead_time_days": "20-25",
        "price_low": 6.40, "price_high": 6.90, "currency": "USD",
        "terms": "FOB",
        "port": "Ningbo",
        "hs_code": "8479.89",
        "controlled": False,
        "certificates": ["TC-M77-118", "CERT-ISO-9001"],
    },
]

DEMO_CERTIFICATES = [
    {"cert_no": "CERT-CE-2041", "type": "CE", "covers": ["X-200"], "valid_until": "2027-03-31",
     "issuer": "TÜV SÜD (0086)"},
    {"cert_no": "TC-M77-118", "type": "Test Certificate", "covers": ["M-77"], "valid_until": "2026-08-12",
     "issuer": "SGS"},
    {"cert_no": "CERT-ISO-9001", "type": "ISO 9001", "covers": [], "valid_until": "2027-01-15",
     "issuer": "工厂资质"},
]

# 演示名单：结构化样例，非真实名单
DEMO_SANCTIONS = [
    {"entry_id": "CN-2211", "name": "Gulf Trading Partners LLC", "country": "AE",
     "type": "出口管制·受控最终用户", "list_version": "sanctions-2026.11-a"},
    {"entry_id": "CN-2307", "name": "Demo Metal Trading SARL", "country": "SA",
     "type": "出口管制·受控最终用户", "list_version": "sanctions-2026.11-a"},
]

# 受控/高风险目的国（演示口径）
DEMO_RESTRICTED_COUNTRIES = ["AE", "SA", "IR", "KP", "SY"]


def demo_products() -> list:
    return [dict(p) for p in DEMO_PRODUCTS]


def demo_certificates() -> list:
    return [dict(c) for c in DEMO_CERTIFICATES]


def demo_sanctions() -> list:
    return [dict(s) for s in DEMO_SANCTIONS]