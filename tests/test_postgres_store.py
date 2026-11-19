# -*- coding: utf-8 -*-
"""PostgreSQL 后端测试：容器未启动时自动跳过，不影响主流程。

与 SQLite 后端跑的是同一套 StateStore 接口，验证两者行为一致。

⚠ 数据库隔离：这里的库由环境变量 `TRADEDESK_TEST_DSN` 指定（dev.ps1 会把
`-Dsn` 传进来）。两个 agent 各用各的库 —— 绝不能默认连共享的 `tradedesk` 库，
否则一个 agent 的 `reset()` 会把另一个 agent 的数据清掉。
"""
import os

import pytest

from tradedesk.domain.enums import Actor, EventType, Language, Stage
from tradedesk.domain.models import ThreadEvent
from tradedesk.domain.state_machine import build_thread

psycopg = pytest.importorskip("psycopg")
from tradedesk.store.postgres_store import DEFAULT_DSN, PostgresStateStore  # noqa: E402


def _dsn() -> str:
    """本 agent 自己的库；没配就退回默认库（单 agent 本地开发场景）。"""
    return os.environ.get("TRADEDESK_TEST_DSN") or DEFAULT_DSN


def _store():
    try:
        s = PostgresStateStore(_dsn())
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


def test_filter_by_stage_accepts_enum_or_string():
    """与 SQLite 后端行为一致：枚举和字符串都能筛。

    ⚠ str(Stage.QUOTING) 是 "Stage.QUOTING"，用它筛会静默返回空列表。
    两个后端必须一样 —— PRD 13.4.5 的选型轴②就是"业务代码零改动换后端"。
    """
    store = _store()
    quoting = build_thread("T-PG-F1", "BY-1", Language.EN)
    quoting.stage = Stage.QUOTING
    store.create_thread(quoting)
    store.create_thread(build_thread("T-PG-F2", "BY-2", Language.AR))

    assert len(store.list_threads(stage=Stage.QUOTING)) == 1
    assert len(store.list_threads(stage="QUOTING")) == 1
    assert len(store.list_threads(stage=Stage.INQUIRY)) == 1
    assert len(store.list_threads(language=Language.AR)) == 1
    store.close()