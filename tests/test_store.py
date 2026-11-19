# -*- coding: utf-8 -*-
"""存储：线程往返、事件追加、审计哈希链、乐观锁、跨重启续传。"""
from datetime import datetime, timedelta, timezone

import pytest

from tradedesk.domain.enums import Actor, EventType, Language, Stage
from tradedesk.domain.models import ThreadEvent
from tradedesk.domain.state_machine import build_thread
from tradedesk.store.sqlite_store import SqliteStateStore


def test_thread_roundtrip(tmpdir_local):
    store = SqliteStateStore(str(tmpdir_local / "s.db"))
    th = build_thread("T-1", "BY-1", Language.AR, message_hash="h")
    store.create_thread(th)
    got = store.get_thread("T-1")
    assert got is not None and got.language is Language.AR


def test_events_are_sequential(tmpdir_local):
    store = SqliteStateStore(str(tmpdir_local / "s.db"))
    store.create_thread(build_thread("T-2", "BY-1", Language.EN))
    for i in range(3):
        store.append_event(ThreadEvent(thread_id="T-2", seq=0, event_type=EventType.MESSAGE_RECEIVED,
                                       actor=Actor.SYSTEM, payload={"i": i}))
    events = store.list_events("T-2")
    assert [e.seq for e in events] == [1, 2, 3]


def test_audit_chain_detects_tampering(tmpdir_local):
    store = SqliteStateStore(str(tmpdir_local / "s.db"))
    store.append_audit("agent", "draft.generated", "T-3", {"x": 1})
    store.append_audit("human", "signed_off", "T-3", {"x": 2})
    assert store.verify_audit_chain() == (True, None)
    rows = store.list_audit()
    store._conn.execute("UPDATE audit_log SET payload='{\"x\":999}' WHERE seq=?", (rows[-1].seq,))
    store._conn.commit()
    ok, bad = store.verify_audit_chain()
    assert ok is False and bad == rows[-1].seq


def test_optimistic_lock(tmpdir_local):
    store = SqliteStateStore(str(tmpdir_local / "s.db"))
    th = build_thread("T-4", "BY-1", Language.EN)
    store.create_thread(th)
    stale = th.model_copy(deep=True)
    fresh = th.model_copy(deep=True); fresh.version += 1
    store.save_thread(fresh)
    with pytest.raises(RuntimeError):
        store.save_thread(stale)


def test_resume_after_restart(tmpdir_local):
    path = str(tmpdir_local / "s.db")
    store = SqliteStateStore(path)
    th = build_thread("T-5", "BY-1", Language.ES, message_hash="h")
    th.last_activity_at = datetime.now(timezone.utc) - timedelta(days=5)
    store.create_thread(th)
    store.close()
    again = SqliteStateStore(path)
    got = again.get_thread("T-5")
    assert got is not None
    assert (datetime.now(timezone.utc) - got.last_activity_at).days >= 5


def test_filter_by_stage_accepts_enum_or_string(tmpdir_local):
    """看板按阶段筛选（FR-ORC-10）必须能直接传枚举。

    ⚠ str(Stage.QUOTING) 是 "Stage.QUOTING" 而不是 "QUOTING"（Python 3.11 起），
    拿它去拼 WHERE 会一条都查不出来 —— 而且不报错，是静默返回空列表。
    """
    store = SqliteStateStore(str(tmpdir_local / "f.db"))
    quoting = build_thread("T-F1", "BY-1", Language.EN)
    quoting.stage = Stage.QUOTING
    store.create_thread(quoting)
    inquiry = build_thread("T-F2", "BY-2", Language.AR)
    store.create_thread(inquiry)

    assert len(store.list_threads(stage=Stage.QUOTING)) == 1
    assert len(store.list_threads(stage="QUOTING")) == 1
    assert len(store.list_threads(stage=Stage.INQUIRY)) == 1
    assert len(store.list_threads(stage=Stage.CLOSED)) == 0


def test_filter_by_language_and_buyer(tmpdir_local):
    store = SqliteStateStore(str(tmpdir_local / "g.db"))
    store.create_thread(build_thread("T-G1", "BY-1", Language.ES))
    store.create_thread(build_thread("T-G2", "BY-1", Language.EN))
    store.create_thread(build_thread("T-G3", "BY-2", Language.ES))

    assert len(store.list_threads(language=Language.ES)) == 2
    assert len(store.list_threads(language=Language.EN)) == 1
    assert len(store.list_threads(buyer_id="BY-1")) == 2
    assert len(store.list_threads(buyer_id="BY-1", language=Language.ES)) == 1