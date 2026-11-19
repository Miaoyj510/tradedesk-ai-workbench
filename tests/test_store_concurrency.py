# -*- coding: utf-8 -*-
"""并发写入：审计哈希链在并发下不能断，闸门令牌只能被消费一次。

HTTP 层（S2）是多线程的 —— 同一时刻多个请求会同时写审计、追加事件、消费令牌。
这几条是"人在环"链路的地基：审计链一断，PRD 11.4 的"可验伪"就没了；
令牌能被消费两次，闸门就形同虚设。
"""
import os
import threading

import pytest

from tradedesk.domain.enums import Actor, EventType, GateId, Language
from tradedesk.domain.models import ThreadEvent
from tradedesk.domain.state_machine import build_thread
from tradedesk.runtime.gates import GateTokenService
from tradedesk.store.sqlite_store import SqliteStateStore

THREADS = 8
PER_THREAD = 20
TOTAL = THREADS * PER_THREAD


def _run_threads(fn):
    errors = []

    def worker(tid):
        try:
            for i in range(PER_THREAD):
                fn(tid, i)
        except Exception as e:  # 并发下的异常必须被看见，而不是悄悄少写几条
            errors.append(repr(e))

    ts = [threading.Thread(target=worker, args=(t,)) for t in range(THREADS)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return errors


# --------------------------------------------------------------------------
# SQLite 后端
# --------------------------------------------------------------------------
def test_sqlite_audit_chain_survives_concurrent_writes(tmpdir_local):
    store = SqliteStateStore(str(tmpdir_local / "c.db"))
    errors = _run_threads(
        lambda tid, i: store.append_audit("agent", "draft.generated", f"T-{tid}", {"i": i})
    )
    assert errors == []
    rows = store.list_audit()
    assert len(rows) == TOTAL
    assert [r.seq for r in rows] == list(range(1, TOTAL + 1))
    assert store.verify_audit_chain() == (True, None)


def test_sqlite_event_seq_unique_under_concurrency(tmpdir_local):
    store = SqliteStateStore(str(tmpdir_local / "e.db"))
    store.create_thread(build_thread("T-C", "BY-1", Language.EN))
    errors = _run_threads(
        lambda tid, i: store.append_event(
            ThreadEvent(thread_id="T-C", seq=0, event_type=EventType.MESSAGE_RECEIVED,
                       actor=Actor.SYSTEM, payload={"i": i})
        )
    )
    assert errors == []
    seqs = [e.seq for e in store.list_events("T-C")]
    assert sorted(seqs) == list(range(1, TOTAL + 1))


def test_sqlite_gate_token_consumed_only_once_under_concurrency(tmpdir_local):
    store = SqliteStateStore(str(tmpdir_local / "g.db"))
    svc = GateTokenService(store, ttl_seconds=600)
    tok = svc.issue("T-G", "D-1", [GateId.PRICE])

    results = []
    lock = threading.Lock()

    def consume(_):
        ok = svc.consume(tok.token, "T-G", "D-1")
        with lock:
            results.append(ok)

    ts = [threading.Thread(target=consume, args=(i,)) for i in range(THREADS)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert results.count(True) == 1, f"令牌被消费了 {results.count(True)} 次"


# --------------------------------------------------------------------------
# PostgreSQL 后端（容器未启动时跳过）
# --------------------------------------------------------------------------
def _pg_store():
    psycopg = pytest.importorskip("psycopg")
    from tradedesk.store.postgres_store import DEFAULT_DSN, PostgresStateStore

    dsn = os.environ.get("TRADEDESK_TEST_DSN") or DEFAULT_DSN
    try:
        s = PostgresStateStore(dsn)
    except Exception as e:
        pytest.skip(f"PostgreSQL 不可用：{e}")
    s.reset()
    return s


def test_postgres_audit_chain_survives_concurrent_writes():
    store = _pg_store()
    errors = _run_threads(
        lambda tid, i: store.append_audit("agent", "draft.generated", f"T-{tid}", {"i": i})
    )
    assert errors == []
    rows = store.list_audit()
    assert len(rows) == TOTAL
    assert [r.seq for r in rows] == list(range(1, TOTAL + 1))
    assert store.verify_audit_chain() == (True, None)
    store.close()


def test_postgres_gate_token_consumed_only_once_under_concurrency():
    store = _pg_store()
    svc = GateTokenService(store, ttl_seconds=600)
    tok = svc.issue("T-PG", "D-1", [GateId.PRICE])

    results = []
    lock = threading.Lock()

    def consume(_):
        ok = svc.consume(tok.token, "T-PG", "D-1")
        with lock:
            results.append(ok)

    ts = [threading.Thread(target=consume, args=(i,)) for i in range(THREADS)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert results.count(True) == 1, f"令牌被消费了 {results.count(True)} 次"
    store.close()
