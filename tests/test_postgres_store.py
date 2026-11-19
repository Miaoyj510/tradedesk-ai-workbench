# -*- coding: utf-8 -*-
"""PostgreSQL 后端测试：容器未启动时自动跳过，不影响主流程。

与 SQLite 后端跑的是同一套 StateStore 接口，验证两者行为一致。
"""
import pytest

from tradedesk.domain.enums import Actor, EventType, Language
from tradedesk.domain.models import ThreadEvent
from tradedesk.domain.state_machine import build_thread

psycopg = pytest.importorskip("psycopg")
from tradedesk.store.postgres_store import DEFAULT_DSN, PostgresStateStore  # noqa: E402


def _store():
    try:
        s = PostgresStateStore(DEFAULT_DSN)
    except Exception as e:  # 容器没起就跳过
        pytest.skip(f"PostgreSQL 不可用：{e}")
    s.reset()
    return s


def test_thread_roundtrip():
    store = _store()
    th = build_thread("T-PG-1", "BY-1", Language.AR, message_hash="h")
    store.create_thread(th)
    got = store.get_thread("T-PG-1")
    assert got is not None and got.language is Language.AR
    store.close()


def test_events_sequential():
    store = _store()
    store.create_thread(build_thread("T-PG-2", "BY-1", Language.EN))
    for i in range(3):
        store.append_event(ThreadEvent(thread_id="T-PG-2", seq=0, event_type=EventType.MESSAGE_RECEIVED,
                                       actor=Actor.SYSTEM, payload={"i": i}))
    assert [e.seq for e in store.list_events("T-PG-2")] == [1, 2, 3]
    store.close()


def test_audit_chain_and_tamper():
    store = _store()
    store.append_audit("agent", "draft.generated", "T-PG-3", {"x": 1})
    store.append_audit("human", "signed_off", "T-PG-3", {"x": 2})
    assert store.verify_audit_chain() == (True, None)
    rows = store.list_audit()
    store.tamper_audit_payload(rows[-1].seq, {"x": 999})
    ok, bad = store.verify_audit_chain()
    assert ok is False and bad == rows[-1].seq
    store.close()


def test_optimistic_lock():
    import pytest as _pytest
    store = _store()
    th = build_thread("T-PG-4", "BY-1", Language.EN)
    store.create_thread(th)
    stale = th.model_copy(deep=True)
    fresh = th.model_copy(deep=True); fresh.version += 1
    store.save_thread(fresh)
    with _pytest.raises(RuntimeError):
        store.save_thread(stale)
    store.close()