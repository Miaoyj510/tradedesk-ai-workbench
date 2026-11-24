# -*- coding: utf-8 -*-
"""发布 / 灰度 / 回滚（S6-3；PRD 13.4.8）。

覆盖：审批必填、证据要求、单灰度约束、观察窗、档位推进、分类别回滚、
别名持久化、审计链完整。
"""
import pytest

from tradedesk.release.bucketing import enterprise_bucket, in_canary, normalize_company
from tradedesk.release.manifest import baseline_components
from tradedesk.release.service import ReleaseError, ReleaseService
from tradedesk.store.sqlite_store import SqliteStateStore

KB = "kb-2026.11-c"
LIST = "sanctions-2026.11-a"


def _svc(store=None):
    store = store or SqliteStateStore(":memory:")
    svc = ReleaseService(store, baseline_components(KB, LIST), observe_hours=24)
    return store, svc


def _create(svc, **kw):
    args = dict(components={"kb_version": "kb-2026.11-d"},
                approver="张三", change_notes="知识库换版本",
                rollback_plan="回滚 knowledge 类别",
                pilot_companies=["ACME Trading"])
    args.update(kw)
    return svc.create(**args)


# ------------------------------------------------------------------ 分桶
def test_normalize_company_folds_width_case_and_space():
    assert normalize_company("  ACME   Trading  ") == "acme trading"
    assert normalize_company("ＡＣＭＥ Trading") == "acme trading"
    assert normalize_company("ACME Trading") == normalize_company("acme  trading")


def test_enterprise_bucket_is_deterministic_and_in_range():
    b1 = enterprise_bucket("ACME Trading")
    assert b1 == enterprise_bucket("ACME Trading")
    assert b1 == enterprise_bucket("acme  trading")
    assert 0 <= b1 <= 99


def test_pilot_whitelist_is_normalized():
    store, svc = _svc()
    m = _create(svc, pilot_companies=["ACME Trading"])
    assert in_canary(m, "acme trading", "en") is True
    assert in_canary(m, "Other Ltd", "en") is False


def test_empty_company_never_enters_canary():
    store, svc = _svc()
    m = _create(svc)
    assert in_canary(m, "", "en") is False


def test_lang_gate_excludes_other_and_unknown_langs():
    store, svc = _svc()
    m = _create(svc, langs=["es"])
    assert in_canary(m, "ACME Trading", "es") is True
    assert in_canary(m, "ACME Trading", "en") is False
    # 语种未知按保守处理：不进灰度
    assert in_canary(m, "ACME Trading", "") is False


def test_index_version_is_a_component_of_knowledge_category():
    from tradedesk.release.manifest import COMPONENT_CATEGORY, COMPONENT_KEYS
    assert "index_version" in COMPONENT_KEYS
    assert COMPONENT_CATEGORY["index_version"] == "knowledge"
    assert COMPONENT_CATEGORY["kb_version"] == "knowledge"


def test_pilot_whitelist_survives_stage_advance():
    """放量档不能把试点企业悄悄踢出灰度（留痕说的 = 做的）。"""
    from datetime import datetime, timedelta, timezone
    store, svc = _svc()
    t0 = datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc)
    m = _create(svc, pilot_companies=["Pilot Corp"], now=t0)
    assert in_canary(m, "Pilot Corp", "en") is True
    m = svc.advance(m.release_id, now=t0 + timedelta(hours=25))
    assert m.stage == "P20"
    assert in_canary(m, "Pilot Corp", "en") is True
    assert svc.resolve("Pilot Corp", "en").source == "canary"


def test_p20_selects_exactly_bucket_below_20():
    store, svc = _svc()
    names = ["Demo Import GmbH %03d" % i for i in range(200)]
    m = _create(svc, components={"prompt_version": "1.1.0"},
                regression_evidence="R-2026-11-24")
    m.stage = "P20"
    selected = [n for n in names if in_canary(m, n, "en")]
    expected = [n for n in names if enterprise_bucket(n) < 20]
    assert selected == expected
    # 抽样 200 家的命中比例应当落在 20% 附近（不要求精确 20%，但不能离谱）
    assert 10 <= len(selected) <= 50


# ------------------------------------------------------------------ 创建
def test_create_requires_approver_change_notes_and_plan():
    store, svc = _svc()
    for field in ("approver", "change_notes", "rollback_plan"):
        kwargs = {"approver": "张三", "change_notes": "变更", "rollback_plan": "预案"}
        kwargs[field] = ""
        with pytest.raises(ReleaseError) as ei:
            _create(svc, **kwargs)
        assert ei.value.code == "validation"


def test_create_requires_pilot_whitelist():
    store, svc = _svc()
    with pytest.raises(ReleaseError) as ei:
        _create(svc, pilot_companies=[])
    assert ei.value.code == "validation"


def test_create_rejects_unknown_component():
    store, svc = _svc()
    with pytest.raises(ReleaseError) as ei:
        _create(svc, components={"mystery_knob": "1"})
    assert ei.value.code == "validation"


def test_create_without_change_is_rejected():
    store, svc = _svc()
    with pytest.raises(ReleaseError) as ei:
        _create(svc, components={"kb_version": KB})   # 与稳定版一致
    assert ei.value.code == "no_change"


def test_agent_change_requires_regression_evidence():
    store, svc = _svc()
    with pytest.raises(ReleaseError) as ei:
        _create(svc, components={"prompt_version": "1.1.0"})
    assert ei.value.code == "regression_required"


def test_sanctions_change_requires_risk_evidence():
    store, svc = _svc()
    with pytest.raises(ReleaseError) as ei:
        _create(svc, components={"list_version": "sanctions-2026.12-a"})
    assert ei.value.code == "risk_evidence_required"


def test_only_one_canary_at_a_time():
    store, svc = _svc()
    _create(svc)
    with pytest.raises(ReleaseError) as ei:
        _create(svc, components={"prompt_version": "1.1.0"}, regression_evidence="R-001")
    assert ei.value.code == "canary_in_flight"


def test_duplicate_release_id_rejected():
    store, svc = _svc()
    _create(svc, release_id="REL-X")
    with pytest.raises(ReleaseError) as ei:
        _create(svc, components={"prompt_version": "1.1.0"}, regression_evidence="R-001",
                release_id="REL-X")
    assert ei.value.code == "duplicate_release"


# ------------------------------------------------------------------ 推进
def test_advance_respects_observe_window():
    from datetime import datetime, timedelta, timezone
    store, svc = _svc()
    t0 = datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc)
    m = _create(svc, now=t0)
    with pytest.raises(ReleaseError) as ei:
        svc.advance(m.release_id, now=t0 + timedelta(hours=23))
    assert ei.value.code == "observe_window"


def test_advance_cannot_skip_stage():
    from datetime import datetime, timedelta, timezone
    store, svc = _svc()
    t0 = datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc)
    m = _create(svc, now=t0)
    m2 = svc.advance(m.release_id, now=t0 + timedelta(hours=25))
    assert m2.stage == "P20"          # 一次只进一档
    m3 = svc.advance(m.release_id, now=t0 + timedelta(hours=50))
    assert m3.stage == "P50"


def test_advance_to_p100_switches_four_aliases():
    from datetime import datetime, timedelta, timezone
    store, svc = _svc()
    t0 = datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc)
    m = _create(svc, now=t0)
    now = t0
    for _ in range(3):
        now = now + timedelta(hours=25)
        m = svc.advance(m.release_id, now=now)
    assert m.stage == "P100"
    assert m.status == "full"
    aliases = store.list_release_aliases()
    assert sorted(aliases) == ["agent", "app", "knowledge", "sanctions"]
    assert svc.resolve("ACME Trading", "en").source == "stable"


# ------------------------------------------------------------------ 回滚
def test_rollback_one_category_keeps_others():
    store, svc = _svc()
    m = _create(svc)
    m2 = svc.rollback(m.release_id, "knowledge")
    assert "knowledge" in m2.rolled_back_categories
    assert m2.status == "rolled_back"     # 本次只改了 knowledge，改动类别已全部回滚
    alias = store.get_release_alias("knowledge")
    assert alias["components"]["kb_version"] == KB     # 回到基线
    assert store.get_release_alias("app") is None      # 其他类别不受影响


def test_rollback_to_previous_release_version():
    from datetime import datetime, timedelta, timezone
    store, svc = _svc()
    t0 = datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc)
    first = _create(svc, now=t0)
    svc.rollback(first.release_id, "knowledge", now=t0)          # 先回到基线
    second = _create(svc, components={"kb_version": "kb-2026.11-e"},
                     now=t0 + timedelta(hours=1))
    svc.rollback(second.release_id, "knowledge", now=t0 + timedelta(hours=2))
    alias = store.get_release_alias("knowledge")
    assert alias["release_id"] == first.release_id
    assert alias["components"]["kb_version"] == "kb-2026.11-d"


def test_rollback_rejects_bad_category_and_missing_release():
    store, svc = _svc()
    m = _create(svc)
    with pytest.raises(ReleaseError) as ei:
        svc.rollback(m.release_id, "knowledge_v2")
    assert ei.value.code == "bad_category"
    with pytest.raises(ReleaseError) as ei:
        svc.rollback("REL-NOPE", "knowledge")
    assert ei.value.code == "not_found"


def test_advance_on_rolled_back_release_rejected():
    from datetime import datetime, timedelta, timezone
    store, svc = _svc()
    t0 = datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc)
    m = _create(svc, now=t0)
    svc.rollback(m.release_id, "knowledge", now=t0)
    with pytest.raises(ReleaseError) as ei:
        svc.advance(m.release_id, now=t0 + timedelta(hours=48))
    assert ei.value.code == "not_canary"


# -------------------------------------------------------------- 持久化/审计
def test_alias_and_release_persist_across_restart(tmpdir_local):
    db = str(tmpdir_local / "release.db")
    store = SqliteStateStore(db)
    svc = ReleaseService(store, baseline_components(KB, LIST), observe_hours=24)
    m = _create(svc)
    svc.rollback(m.release_id, "knowledge")
    store.close()

    store2 = SqliteStateStore(db)
    svc2 = ReleaseService(store2, baseline_components(KB, LIST), observe_hours=24)
    assert len(svc2.list()) == 1
    alias = store2.get_release_alias("knowledge")
    assert alias is not None and alias["components"]["kb_version"] == KB
    store2.close()


def test_evidence_must_be_verifiable():
    """证据不能是"随便写点什么"——否则"不允许跳过回归直接改 Prompt"就是空话。"""
    store, svc = _svc()
    with pytest.raises(ReleaseError) as ei:
        _create(svc, components={"prompt_version": "1.2.0"},
                regression_evidence="随便写点什么")
    assert ei.value.code == "regression_evidence_unverifiable"
    with pytest.raises(ReleaseError) as ei:
        _create(svc, components={"list_version": "sanctions-2026.12-b"},
                risk_evidence="随便写点什么")
    assert ei.value.code == "risk_evidence_unverifiable"


def test_same_chunk_id_can_live_in_two_kb_versions():
    """复合主键：同名块在两个库版本下并存（双索引的前提）。"""
    from tradedesk.data.kb_seed import build_demo_chunks
    store = SqliteStateStore(":memory:")
    v1 = build_demo_chunks("kb-v1")
    v2 = build_demo_chunks("kb-v2")
    for c in v1 + v2:
        store.upsert_kb_chunk(c)
    a = store.get_kb_chunk("KB-A-SPEC-X-200", "kb-v1")
    b = store.get_kb_chunk("KB-A-SPEC-X-200", "kb-v2")
    assert a is not None and b is not None
    assert a.kb_version == "kb-v1" and b.kb_version == "kb-v2"
    assert len(store.list_kb_chunks("kb-v1")) == len(v1)
    assert len(store.list_kb_chunks("kb-v2")) == len(v2)


def test_release_fills_in_an_unready_kb_version():
    """灰度到一个"库里没块"的版本 → 建发布时必须把该版本补齐。"""
    from tradedesk.data.kb_seed import build_demo_chunks
    store = SqliteStateStore(":memory:")
    svc = ReleaseService(store, baseline_components("kb-v1", LIST), observe_hours=24)
    m = svc.create(components={"kb_version": "kb-brand-new"}, approver="张三",
                   change_notes="切到新库版本", rollback_plan="回滚 knowledge 类别",
                   pilot_companies=["New Co"])
    assert m.components["kb_version"] == "kb-brand-new"
    assert len(store.list_kb_chunks("kb-brand-new")) > 0


def test_audit_chain_stays_valid_after_release_operations():
    store, svc = _svc()
    m = _create(svc)
    svc.rollback(m.release_id, "knowledge")
    ok, broken = store.verify_audit_chain()
    assert ok is True and broken is None
    actions = [a.action for a in store.list_audit()]
    assert "release.create" in actions
    assert "release.rollback" in actions