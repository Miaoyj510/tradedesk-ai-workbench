# -*- coding: utf-8 -*-
"""SQLite 实现：线程状态、事件日志、审计哈希链、闸门令牌。

为什么先用 SQLite（而不是直接上 PostgreSQL + pgvector）
- 本机没有 Docker，跑不了 pgvector 容器；SQLite 让"跨天长事务"这层今天就能被验证。
- 存储层是接口（store/base.py），换成 PostgreSQL 时业务代码零改动 —— 这正是 PRD 13.4.5 选型轴②的含义。
- SQLite 开启 WAL，读写并发足够 1—3 人测试阶段。

生产化差异见 PRD 13.4.11：数据库迁托管实例、审计独立存储、备份与容灾。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .base import audit_hash, canonical_ts, enum_val
from ..domain.models import (
    AuditEntry,
    CustomerProfile,
    GateToken,
    ThreadEvent,
    ThreadState,
)

SCHEMA_VERSION = 1

DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
    buyer_id   TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS threads (
    thread_id   TEXT PRIMARY KEY,
    buyer_id    TEXT NOT NULL,
    stage       TEXT NOT NULL,
    language    TEXT NOT NULL,
    version     INTEGER NOT NULL,
    payload     TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_threads_stage ON threads(stage);
CREATE INDEX IF NOT EXISTS idx_threads_buyer ON threads(buyer_id);

CREATE TABLE IF NOT EXISTS thread_events (
    thread_id  TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    actor      TEXT NOT NULL,
    payload    TEXT NOT NULL,
    ts         TEXT NOT NULL,
    PRIMARY KEY (thread_id, seq)
);

CREATE TABLE IF NOT EXISTS audit_log (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    actor      TEXT NOT NULL,
    action     TEXT NOT NULL,
    target     TEXT NOT NULL,
    payload    TEXT NOT NULL,
    ts         TEXT NOT NULL,
    prev_hash  TEXT NOT NULL,
    hash       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gate_tokens (
    token       TEXT PRIMARY KEY,
    thread_id   TEXT NOT NULL,
    draft_id    TEXT NOT NULL,
      action      TEXT NOT NULL DEFAULT '',
    gates       TEXT NOT NULL,
    issued_at   TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    consumed_at TEXT
);
"""


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)


def _audit_hash(entry: AuditEntry) -> str:
    return audit_hash(entry.seq, entry.actor, entry.action, entry.target,
                      entry.payload, entry.ts, entry.prev_hash)


class SqliteStateStore:
    """SQLite 状态存储。

    **每个线程一条连接**：Python 的 sqlite3 连接不能跨线程共用 ——
    `check_same_thread=False` 只是不报错，事务状态照样会互相踩。
    这里按线程隔离连接，靠 WAL 实现"多读一写"；再对"读一条 → 写一条"的
    取号操作加一把进程内写锁，保证 seq / prev_hash 不会算重。
    """

    def __init__(self, path: str = "data/tradedesk.db", create: bool = True) -> None:
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._conns: List[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()
        self._write_lock = threading.RLock()
        # :memory: 库不能按线程分连接（每条连接都是一个独立的空库），
        # 这种模式只用于单线程场景，靠写锁保证一致性。
        self._shared_conn: Optional[sqlite3.Connection] = None
        if create:
            self.init_schema()

    def _new_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # autocommit：默认隔离级别下，一次 SELECT 会开着一个读事务直到 commit；
        # 之后同一连接想写（"升级事务"）若此时别的连接正在写，SQLite 会**立刻**
        # 返回 SQLITE_BUSY（busy_timeout 对这种死锁升级无效）。
        # 改成 autocommit 后每条语句自成一个事务，跨线程并发写只靠 busy_timeout 排队。
        conn.isolation_level = None
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @property
    def _conn(self) -> sqlite3.Connection:
        """当前线程的连接（没有就建一条）。"""
        if self.path == ":memory:":
            if self._shared_conn is None:
                self._shared_conn = self._new_conn()
            return self._shared_conn
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_conn()
            self._local.conn = conn
            with self._conns_lock:
                self._conns.append(conn)
        return conn

    # ---------- 基础设施 ----------
    def init_schema(self) -> None:
        self._conn.executescript(DDL)
        # 轻量迁移：老库补列（CREATE TABLE IF NOT EXISTS 不会给已存在的表加列）
        _cols = {r[1] for r in self._conn.execute("PRAGMA table_info(gate_tokens)")}
        if "action" not in _cols:
            self._conn.execute("ALTER TABLE gate_tokens ADD COLUMN action TEXT NOT NULL DEFAULT ''")
        cur = self._conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'")
        row = cur.fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        self._conn.commit()

    def close(self) -> None:
        """关闭所有线程的连接（不只是当前线程那条）。"""
        with self._conns_lock:
            conns, self._conns = self._conns, []
        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass
        self._local = threading.local()
        if self._shared_conn is not None:
            try:
                self._shared_conn.close()
            except Exception:
                pass
            self._shared_conn = None

    # ---------- 演练与验证辅助（生产也可用：篡改演练、令牌过期演练） ----------
    def reset(self) -> None:
        """清空业务表，便于验证脚本重复运行。"""
        with self._write_lock:
            self._conn.executescript(
                "DELETE FROM threads; DELETE FROM customers; DELETE FROM thread_events; "
                "DELETE FROM audit_log; DELETE FROM gate_tokens;"
            )
            self._conn.commit()

    def tamper_audit_payload(self, seq: int, payload: Dict[str, Any]) -> None:
        """演练用：直接改写审计内容，用于验证哈希链能否发现篡改。"""
        self._conn.execute(
            "UPDATE audit_log SET payload=? WHERE seq=?",
            (_dumps(payload), seq),
        )
        self._conn.commit()

    def expire_gate_token(self, token_id: str, at: datetime) -> None:
        """演练用：把令牌改成已过期。"""
        self._conn.execute("UPDATE gate_tokens SET expires_at=? WHERE token=?", (at.isoformat(), token_id))
        self._conn.commit()

    # ---------- 客户档案 ----------
    def upsert_customer(self, profile: CustomerProfile) -> None:
        profile.updated_at = datetime.now(timezone.utc)
        self._conn.execute(
            "INSERT INTO customers(buyer_id, payload, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(buyer_id) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
            (profile.buyer_id, _dumps(profile.model_dump(mode="json")), profile.updated_at.isoformat()),
        )
        self._conn.commit()

    def get_customer(self, buyer_id: str) -> Optional[CustomerProfile]:
        row = self._conn.execute(
            "SELECT payload FROM customers WHERE buyer_id=?", (buyer_id,)
        ).fetchone()
        return CustomerProfile.model_validate_json(row["payload"]) if row else None

    # ---------- 线程 ----------
    def create_thread(self, thread: ThreadState) -> None:
        self._conn.execute(
            "INSERT INTO threads(thread_id, buyer_id, stage, language, version, payload, updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                thread.thread_id, thread.buyer_id, enum_val(thread.stage), enum_val(thread.language),
                thread.version, _dumps(thread.model_dump(mode="json")),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._conn.commit()

    def get_thread(self, thread_id: str) -> Optional[ThreadState]:
        row = self._conn.execute(
            "SELECT payload FROM threads WHERE thread_id=?", (thread_id,)
        ).fetchone()
        return ThreadState.model_validate_json(row["payload"]) if row else None

    def save_thread(self, thread: ThreadState) -> None:
        """乐观锁保存：版本号必须 +1，避免并发覆盖。"""
        with self._write_lock:
            cur = self._conn.execute(
                "UPDATE threads SET stage=?, language=?, version=?, payload=?, updated_at=? WHERE thread_id=? AND version=?",
                (
                    enum_val(thread.stage), enum_val(thread.language), thread.version,
                    _dumps(thread.model_dump(mode="json")),
                    datetime.now(timezone.utc).isoformat(), thread.thread_id, thread.version - 1,
                ),
            )
            if cur.rowcount == 0:
                existing = self.get_thread(thread.thread_id)
                raise RuntimeError(
                    f"并发冲突：thread {thread.thread_id} 当前版本 "
                    f"{existing.version if existing else '?'}，提交版本 {thread.version}"
                )
            self._conn.commit()

    def list_threads(self, **filters: Any) -> List[ThreadState]:
        sql = "SELECT payload FROM threads"
        clauses, params = [], []
        if "stage" in filters and filters["stage"]:
            # enum_val：不能写 str(filters["stage"]) —— str(Stage.QUOTING) 是
            # "Stage.QUOTING"，用它筛会一条都查不到（看板按阶段筛选会静默返回空）
            clauses.append("stage=?"); params.append(enum_val(filters["stage"]))
        if "language" in filters and filters["language"]:
            clauses.append("language=?"); params.append(enum_val(filters["language"]))
        if "buyer_id" in filters and filters["buyer_id"]:
            clauses.append("buyer_id=?"); params.append(filters["buyer_id"])
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC"
        return [ThreadState.model_validate_json(r["payload"]) for r in self._conn.execute(sql, params)]

    # ---------- 事件日志（append-only） ----------
    def append_event(self, event: ThreadEvent) -> ThreadEvent:
        # 取号 + 插入要原子：加写锁，否则并发写入会拿到同一个 seq。
        with self._write_lock:
            row = self._conn.execute(
                "SELECT MAX(seq) AS s FROM thread_events WHERE thread_id=?", (event.thread_id,)
            ).fetchone()
            event.seq = (row["s"] or 0) + 1
            self._conn.execute(
                "INSERT INTO thread_events(thread_id, seq, event_type, actor, payload, ts) VALUES(?,?,?,?,?,?)",
                (
                    event.thread_id, event.seq, event.event_type.value, event.actor.value,
                    _dumps(event.payload), event.ts.isoformat(),
                ),
            )
            self._conn.commit()
        return event

    def list_events(self, thread_id: str) -> List[ThreadEvent]:
        rows = self._conn.execute(
            "SELECT * FROM thread_events WHERE thread_id=? ORDER BY seq", (thread_id,)
        ).fetchall()
        out = []
        for r in rows:
            out.append(ThreadEvent(
                thread_id=r["thread_id"], seq=r["seq"], event_type=r["event_type"],  # type: ignore[arg-type]
                actor=r["actor"],  # type: ignore[arg-type]
                payload=json.loads(r["payload"]), ts=datetime.fromisoformat(r["ts"]),
            ))
        return out

    # ---------- 审计（append-only + 哈希链） ----------
    def append_audit(self, actor: str, action: str, target: str, payload: Dict[str, Any]) -> AuditEntry:
        # 哈希链要求"每条的 prev_hash 正好是上一条的 hash"。
        # 取 prev + 算 seq + 插入必须原子，加写锁。
        with self._write_lock:
            row = self._conn.execute("SELECT seq, hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
            prev_hash = row["hash"] if row else ""
            seq = (row["seq"] if row else 0) + 1
            entry = AuditEntry(
                seq=seq, actor=actor, action=action, target=target, payload=payload,
                ts=datetime.now(timezone.utc), prev_hash=prev_hash,
            )
            entry.hash = _audit_hash(entry)
            self._conn.execute(
                "INSERT INTO audit_log(seq, actor, action, target, payload, ts, prev_hash, hash) VALUES(?,?,?,?,?,?,?,?)",
                (
                    entry.seq, entry.actor, entry.action, entry.target, _dumps(entry.payload),
                    entry.ts.isoformat(), entry.prev_hash, entry.hash,
                ),
            )
            self._conn.commit()
        return entry

    def list_audit(self) -> List[AuditEntry]:
        rows = self._conn.execute("SELECT * FROM audit_log ORDER BY seq").fetchall()
        return [
            AuditEntry(
                seq=r["seq"], actor=r["actor"], action=r["action"], target=r["target"],
                payload=json.loads(r["payload"]), ts=datetime.fromisoformat(r["ts"]),
                prev_hash=r["prev_hash"], hash=r["hash"],
            )
            for r in rows
        ]

    def verify_audit_chain(self) -> Tuple[bool, Optional[int]]:
        """校验哈希链；返回 (是否完整, 首个被篡改的 seq)。"""
        prev = ""
        for e in self.list_audit():
            if e.prev_hash != prev or _audit_hash(e) != e.hash:
                return False, e.seq
            prev = e.hash
        return True, None

    # ---------- 闸门令牌 ----------
    def issue_gate_token(self, token: GateToken) -> None:
        with self._write_lock:
            self._conn.execute(
                "INSERT INTO gate_tokens(token, thread_id, draft_id, action, gates, issued_at, expires_at, consumed_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    token.token, token.thread_id, token.draft_id, token.action,
                    _dumps([g.value for g in token.gates]),
                    token.issued_at.isoformat(), token.expires_at.isoformat(),
                    token.consumed_at.isoformat() if token.consumed_at else None,
                ),
            )
            self._conn.commit()

    def get_gate_token(self, token_id: str) -> Optional[GateToken]:
        row = self._conn.execute("SELECT * FROM gate_tokens WHERE token=?", (token_id,)).fetchone()
        if not row:
            return None
        keys = row.keys()
        return GateToken(
            token=row["token"], thread_id=row["thread_id"], draft_id=row["draft_id"],
            action=row["action"] if "action" in keys else "",
            gates=json.loads(row["gates"]),
            issued_at=datetime.fromisoformat(row["issued_at"]),
            expires_at=datetime.fromisoformat(row["expires_at"]),
            consumed_at=datetime.fromisoformat(row["consumed_at"]) if row["consumed_at"] else None,
        )

    def consume_gate_token(self, token_id: str, thread_id: str, draft_id: str) -> bool:
        """一次性消费：线程与草稿必须匹配，且未消费、未过期。"""
        # 判重 + 标记消费要原子，否则两个并发请求可能同时消费同一个令牌。
        with self._write_lock:
            token = self.get_gate_token(token_id)
            if token is None or token.consumed_at is not None:
                return False
            if token.thread_id != thread_id or token.draft_id != draft_id:
                return False
            now = datetime.now(timezone.utc)
            if now > token.expires_at:
                return False
            cur = self._conn.execute(
                "UPDATE gate_tokens SET consumed_at=? WHERE token=? AND consumed_at IS NULL",
                (now.isoformat(), token_id),
            )
            self._conn.commit()
            return cur.rowcount == 1