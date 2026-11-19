# -*- coding: utf-8 -*-
"""PostgreSQL 实现（PRD 13.4 的目标形态：PostgreSQL 16 + pgvector）。

与 SqliteStateStore 实现**完全相同的 StateStore 接口**，因此业务代码零改动即可切换；
这正是 PRD 13.4.5「选型轴②：向量检索存储」的可替换性设计。

与 SQLite 实现的差异（生产化的真实成本所在）
- JSONB 取代 TEXT：可建 GIN 索引、可按字段过滤
- timestamptz 取代 ISO 字符串：时区正确、可比较
- bigserial 取代自增：并发下不冲突
- 乐观锁用 UPDATE ... WHERE version = old_version
- 连接池由 psycopg3 管理（生产环境换成连接池中间件即可）

pgvector 扩展在 S4 接入：镜像见 docker/pgvector/Dockerfile。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .base import ConcurrentUpdate, audit_hash, enum_val
from ..domain.models import (
    KBChunk,
    AuditEntry,
    CustomerProfile,
    GateToken,
    ThreadEvent,
    ThreadState,
)

DEFAULT_DSN = "postgresql://tradedesk:tradedesk_local_dev@127.0.0.1:5433/tradedesk"

# 事务级咨询锁：审计链与事件序号都是"读最后一条 → 再插一条"，
# 不加锁在并发下会算出同一个 seq / 同一个 prev_hash，链就断了。
# pg_advisory_xact_lock 在事务结束时自动释放，异常回滚也不会留死锁。
LOCK_AUDIT = 811_001
LOCK_EVENT = 811_002

DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS customers (
    buyer_id   TEXT PRIMARY KEY,
    payload    JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS threads (
    thread_id  TEXT PRIMARY KEY,
    buyer_id   TEXT NOT NULL,
    stage      TEXT NOT NULL,
    language   TEXT NOT NULL,
    message_hash TEXT NOT NULL DEFAULT '',
    version    INTEGER NOT NULL,
    payload    JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_threads_stage ON threads(stage);
CREATE INDEX IF NOT EXISTS idx_threads_buyer ON threads(buyer_id);
-- 幂等兜底：同一买家的同一条消息只能建出一条线程（并发重复投递）
CREATE UNIQUE INDEX IF NOT EXISTS uq_threads_msg ON threads(buyer_id, message_hash) WHERE message_hash <> '';
CREATE TABLE IF NOT EXISTS thread_events (
    thread_id  TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    actor      TEXT NOT NULL,
    payload    JSONB NOT NULL,
    ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (thread_id, seq)
);
CREATE TABLE IF NOT EXISTS audit_log (
    seq       BIGSERIAL PRIMARY KEY,
    actor     TEXT NOT NULL,
    action    TEXT NOT NULL,
    target    TEXT NOT NULL,
    payload   JSONB NOT NULL,
    ts        TIMESTAMPTZ NOT NULL DEFAULT now(),
    prev_hash TEXT NOT NULL,
    hash      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kb_chunks (
    chunk_id        TEXT PRIMARY KEY,
    doc_id         TEXT NOT NULL,
    doc_type       TEXT NOT NULL,
    title          TEXT NOT NULL DEFAULT '',
    text           TEXT NOT NULL,
    lang           TEXT NOT NULL DEFAULT 'zh',
    identifiers    JSONB NOT NULL DEFAULT '{}'::jsonb,
    countries      JSONB NOT NULL DEFAULT '[]'::jsonb,
    hs_codes       JSONB NOT NULL DEFAULT '[]'::jsonb,
    tags           JSONB NOT NULL DEFAULT '[]'::jsonb,
    effective_from TIMESTAMPTZ,
    effective_to   TIMESTAMPTZ,
    valid_until    TIMESTAMPTZ,
    kb_version     TEXT NOT NULL DEFAULT '',
    extra          JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_kb_version ON kb_chunks(kb_version);
CREATE INDEX IF NOT EXISTS idx_kb_doc_type ON kb_chunks(doc_type);

CREATE TABLE IF NOT EXISTS gate_tokens (
    token       TEXT PRIMARY KEY,
    thread_id   TEXT NOT NULL,
    draft_id    TEXT NOT NULL,
    action      TEXT NOT NULL DEFAULT '',
    gates       JSONB NOT NULL,
    issued_at   TIMESTAMPTZ NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ
);
"""


def _json_default(obj: Any) -> str:
    return str(obj)


class PostgresStateStore:
    """PostgreSQL 状态存储。接口与 SqliteStateStore 一致。

    **每个线程一条连接**：psycopg 的 Connection 不是线程安全的 ——
    同一个连接被多个线程同时用，事务状态会互相踩
    （实测报 "transaction commit at the wrong nesting level"），
    审计链在这种并发下会断。HTTP 层是多线程的，所以这里按线程隔离连接。
    """

    def __init__(self, dsn: str = DEFAULT_DSN, create: bool = True) -> None:
        import psycopg  # 延迟导入：没装驱动也能用 SQLite 后端跑

        self.dsn = dsn
        self._psycopg = psycopg
        self._local = threading.local()
        self._conns: List[Any] = []
        self._conns_lock = threading.Lock()
        if create:
            self.init_schema()

    @property
    def conn(self):
        """当前线程的连接（没有就建一条）。"""
        c = getattr(self._local, "conn", None)
        if c is None or c.closed:
            # connect_timeout：容器没起时立刻失败，不干等默认长超时
            c = self._psycopg.connect(self.dsn, connect_timeout=5)
            c.autocommit = True
            self._local.conn = c
            with self._conns_lock:
                self._conns.append(c)
        return c

    # ---------- 基础设施 ----------
    def init_schema(self) -> None:
        with self.conn.cursor() as cur:
            # 先给老库补列，再建索引（DDL 里有依赖新列的唯一索引）
            cur.execute("SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='threads' AND column_name='message_hash'")
            if cur.fetchone() is None:
                cur.execute("SELECT 1 FROM information_schema.tables "
                            "WHERE table_name='threads'")
                if cur.fetchone() is not None:
                    cur.execute("ALTER TABLE threads ADD COLUMN message_hash TEXT NOT NULL DEFAULT ''")
            cur.execute(DDL)
            # 轻量迁移：老库补列
            tcols = cur.execute("SELECT column_name FROM information_schema.columns "
                                "WHERE table_name='threads' AND column_name='message_hash'").fetchone()
            if tcols is None:
                cur.execute("ALTER TABLE threads ADD COLUMN message_hash TEXT NOT NULL DEFAULT ''")
            cur.execute("SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='gate_tokens' AND column_name='action'")
            if cur.fetchone() is None:
                cur.execute("ALTER TABLE gate_tokens ADD COLUMN action TEXT NOT NULL DEFAULT ''")
            cur.execute(
                "INSERT INTO schema_meta(key, value) VALUES('schema_version', '1') "
                "ON CONFLICT (key) DO NOTHING"
            )

    def close(self) -> None:
        """关闭所有线程的连接（不只是当前线程那条）。"""
        with self._conns_lock:
            conns, self._conns = self._conns, []
        for c in conns:
            try:
                c.close()
            except Exception:
                pass
        self._local = threading.local()

    def reset(self) -> None:
        """清空业务表（验证脚本可重复运行的前提）。"""
        with self.conn.cursor() as cur:
            cur.execute("TRUNCATE threads, customers, thread_events, audit_log, gate_tokens")

    # ---------- 演练与验证辅助（生产也可用：篡改演练、令牌过期演练） ----------
    def tamper_audit_payload(self, seq: int, payload: Dict[str, Any]) -> None:
        """演练用：直接改写审计内容，用于验证哈希链能否发现篡改。"""
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE audit_log SET payload=%s WHERE seq=%s",
                (json.dumps(payload, ensure_ascii=False, default=_json_default), seq),
            )

    def expire_gate_token(self, token_id: str, at: datetime) -> None:
        """演练用：把令牌改成已过期。"""
        with self.conn.cursor() as cur:
            cur.execute("UPDATE gate_tokens SET expires_at=%s WHERE token=%s", (at, token_id))
    # ---------- 客户档案 ----------
    def upsert_customer(self, profile: CustomerProfile) -> None:
        profile.updated_at = datetime.now(timezone.utc)
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO customers(buyer_id, payload, updated_at) VALUES(%s, %s, now()) "
                "ON CONFLICT (buyer_id) DO UPDATE SET payload=EXCLUDED.payload, updated_at=now()",
                (profile.buyer_id, json.dumps(profile.model_dump(mode="json"), ensure_ascii=False, default=_json_default)),
            )

    def get_customer(self, buyer_id: str) -> Optional[CustomerProfile]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT payload FROM customers WHERE buyer_id=%s", (buyer_id,))
            row = cur.fetchone()
        return CustomerProfile.model_validate(row[0]) if row else None

    # ---------- 线程 ----------
    def create_thread(self, thread: ThreadState) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO threads(thread_id, buyer_id, stage, language, message_hash, version, payload, updated_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s, now())",
                (
                    thread.thread_id, thread.buyer_id,
                    enum_val(thread.stage),
                    enum_val(thread.language),
                    thread.last_message_hash,
                    thread.version,
                    json.dumps(thread.model_dump(mode="json"), ensure_ascii=False, default=_json_default),
                ),
            )

    def create_thread_if_absent(self, thread: ThreadState) -> bool:
        """原子建线程：已存在同 id 或同 (买家, 消息哈希) 时返回 False（= 重复消息）。"""
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO threads(thread_id, buyer_id, stage, language, message_hash, version, payload, updated_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s, now()) ON CONFLICT DO NOTHING",
                (
                    thread.thread_id, thread.buyer_id,
                    enum_val(thread.stage),
                    enum_val(thread.language),
                    thread.last_message_hash,
                    thread.version,
                    json.dumps(thread.model_dump(mode="json"), ensure_ascii=False, default=_json_default),
                ),
            )
            return cur.rowcount > 0

    def allocate_thread_id(self, prefix: str = "T-", start: int = 2000) -> str:
        """持久取号：原子自增，进程重启/多实例都不撞号。"""
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO schema_meta(key, value) VALUES('thread_seq', 1) "
                "ON CONFLICT (key) DO UPDATE SET value = (schema_meta.value::bigint + 1) "
                "RETURNING value")
            nxt = int(cur.fetchone()[0])
            return f"{prefix}{start + nxt}"

    def get_thread(self, thread_id: str) -> Optional[ThreadState]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT payload FROM threads WHERE thread_id=%s", (thread_id,))
            row = cur.fetchone()
        return ThreadState.model_validate(row[0]) if row else None

    def save_thread(self, thread: ThreadState) -> None:
        """乐观锁：UPDATE 的 WHERE 里带旧版本号，匹配 0 行即并发冲突。"""
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE threads SET stage=%s, language=%s, version=%s, payload=%s, updated_at=now() "
                "WHERE thread_id=%s AND version=%s",
                (
                    enum_val(thread.stage),
                    enum_val(thread.language),
                    thread.version,
                    json.dumps(thread.model_dump(mode="json"), ensure_ascii=False, default=_json_default),
                    thread.thread_id, thread.version - 1,
                ),
            )
            if cur.rowcount == 0:
                cur.execute("SELECT version FROM threads WHERE thread_id=%s", (thread.thread_id,))
                row = cur.fetchone()
                raise ConcurrentUpdate(thread.thread_id, row[0] if row else "?", thread.version)

    def list_threads(self, **filters: Any) -> List[ThreadState]:
        sql, params = "SELECT payload FROM threads", []
        clauses = []
        if filters.get("stage"):
            # enum_val：不能写 str(filters["stage"]) —— str(Stage.QUOTING) 是
            # "Stage.QUOTING"，用它筛会一条都查不到（看板按阶段筛选会静默返回空）
            clauses.append("stage=%s"); params.append(enum_val(filters["stage"]))
        if filters.get("language"):
            clauses.append("language=%s"); params.append(enum_val(filters["language"]))
        if filters.get("buyer_id"):
            clauses.append("buyer_id=%s"); params.append(filters["buyer_id"])
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC"
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [ThreadState.model_validate(r[0]) for r in rows]

    # ---------- 事件日志 ----------
    def append_event(self, event: ThreadEvent) -> ThreadEvent:
        # 取号 + 插入必须在同一事务里并加锁，否则并发写入会拿到同一个 seq。
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_EVENT,))
                cur.execute(
                    "SELECT COALESCE(MAX(seq),0) FROM thread_events WHERE thread_id=%s", (event.thread_id,)
                )
                event.seq = cur.fetchone()[0] + 1
                cur.execute(
                    "INSERT INTO thread_events(thread_id, seq, event_type, actor, payload, ts) "
                    "VALUES(%s,%s,%s,%s,%s,%s)",
                    (
                        event.thread_id, event.seq, event.event_type.value, event.actor.value,
                        json.dumps(event.payload, ensure_ascii=False, default=_json_default),
                        event.ts,
                    ),
                )
        return event

    def list_events(self, thread_id: str) -> List[ThreadEvent]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT thread_id, seq, event_type, actor, payload, ts FROM thread_events "
                "WHERE thread_id=%s ORDER BY seq", (thread_id,)
            )
            rows = cur.fetchall()
        return [
            ThreadEvent(thread_id=r[0], seq=r[1], event_type=r[2], actor=r[3],
                        payload=r[4], ts=r[5])
            for r in rows
        ]

    # ---------- 审计（哈希链） ----------
    def append_audit(self, actor: str, action: str, target: str, payload: Dict[str, Any]) -> AuditEntry:
        # 哈希链要求"每条的 prev_hash 正好是上一条的 hash"。
        # 取 prev + 算 seq + 插入必须在同一事务内加锁完成，
        # 否则两个并发写入会算出同一个 prev_hash，链从那一条起就断了。
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_AUDIT,))
                cur.execute("SELECT seq, hash FROM audit_log ORDER BY seq DESC LIMIT 1")
                row = cur.fetchone()
                prev_hash = row[1] if row else ""
                seq = (row[0] if row else 0) + 1
                entry = AuditEntry(seq=seq, actor=actor, action=action, target=target, payload=payload,
                                   ts=datetime.now(timezone.utc), prev_hash=prev_hash)
                entry.hash = audit_hash(entry.seq, entry.actor, entry.action, entry.target,
                                        entry.payload, entry.ts, entry.prev_hash)
                cur.execute(
                    "INSERT INTO audit_log(seq, actor, action, target, payload, ts, prev_hash, hash) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        entry.seq, entry.actor, entry.action, entry.target,
                        json.dumps(entry.payload, ensure_ascii=False, default=_json_default),
                        entry.ts, entry.prev_hash, entry.hash,
                    ),
                )
        return entry

    def list_audit(self) -> List[AuditEntry]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT seq, actor, action, target, payload, ts, prev_hash, hash FROM audit_log ORDER BY seq")
            rows = cur.fetchall()
        return [
            AuditEntry(seq=r[0], actor=r[1], action=r[2], target=r[3], payload=r[4],
                       ts=r[5], prev_hash=r[6], hash=r[7])
            for r in rows
        ]

    def verify_audit_chain(self) -> Tuple[bool, Optional[int]]:
        prev = ""
        for e in self.list_audit():
            expect = audit_hash(e.seq, e.actor, e.action, e.target, e.payload, e.ts, e.prev_hash)
            if e.prev_hash != prev or expect != e.hash:
                return False, e.seq
            prev = e.hash
        return True, None

    # ---------- 闸门令牌 ----------
    # ---------- 知识库 ----------
    def upsert_kb_chunk(self, chunk) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO kb_chunks(chunk_id, doc_id, doc_type, title, text, lang, identifiers, "
                "countries, hs_codes, tags, effective_from, effective_to, valid_until, kb_version, extra) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (chunk_id) DO UPDATE SET doc_id=EXCLUDED.doc_id, "
                "doc_type=EXCLUDED.doc_type, title=EXCLUDED.title, text=EXCLUDED.text, "
                "lang=EXCLUDED.lang, identifiers=EXCLUDED.identifiers, countries=EXCLUDED.countries, "
                "hs_codes=EXCLUDED.hs_codes, tags=EXCLUDED.tags, "
                "effective_from=EXCLUDED.effective_from, effective_to=EXCLUDED.effective_to, "
                "valid_until=EXCLUDED.valid_until, kb_version=EXCLUDED.kb_version, extra=EXCLUDED.extra",
                (
                    chunk.chunk_id, chunk.doc_id, chunk.doc_type, chunk.title, chunk.text, chunk.lang,
                    json.dumps(chunk.identifiers, ensure_ascii=False),
                    json.dumps(chunk.countries, ensure_ascii=False),
                    json.dumps(chunk.hs_codes, ensure_ascii=False),
                    json.dumps(chunk.tags, ensure_ascii=False),
                    chunk.effective_from, chunk.effective_to, chunk.valid_until,
                    chunk.kb_version, json.dumps(chunk.extra, ensure_ascii=False),
                ),
            )

    def list_kb_chunks(self, kb_version=None):
        sql, params = "SELECT * FROM kb_chunks", []
        if kb_version:
            sql += " WHERE kb_version=%s"
            params.append(kb_version)
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [
            KBChunk(
                chunk_id=r[0], doc_id=r[1], doc_type=r[2], title=r[3], text=r[4], lang=r[5],
                identifiers=r[6], countries=r[7], hs_codes=r[8], tags=r[9],
                effective_from=r[10], effective_to=r[11], valid_until=r[12],
                kb_version=r[13], extra=r[14],
            )
            for r in rows
        ]

    def get_kb_chunk(self, chunk_id):
        for c in self.list_kb_chunks():
            if c.chunk_id == chunk_id:
                return c
        return None

    def clear_kb(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM kb_chunks")

    def issue_gate_token(self, token: GateToken) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO gate_tokens(token, thread_id, draft_id, action, gates, issued_at, expires_at, consumed_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    token.token, token.thread_id, token.draft_id, token.action,
                    json.dumps([g.value for g in token.gates]), token.issued_at, token.expires_at,
                    token.consumed_at,
                ),
            )

    def get_gate_token(self, token_id: str) -> Optional[GateToken]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT token, thread_id, draft_id, action, gates, issued_at, expires_at, consumed_at "
                "FROM gate_tokens WHERE token=%s", (token_id,)
            )
            row = cur.fetchone()
        if not row:
            return None
        return GateToken(token=row[0], thread_id=row[1], draft_id=row[2], action=row[3], gates=row[4],
                         issued_at=row[5], expires_at=row[6], consumed_at=row[7])

    def consume_gate_token(self, token_id: str, thread_id: str, draft_id: str) -> bool:
        """一次性消费：条件更新（consumed_at IS NULL）保证并发下也只成功一次。"""
        token = self.get_gate_token(token_id)
        if token is None or token.consumed_at is not None:
            return False
        if token.thread_id != thread_id or token.draft_id != draft_id:
            return False
        now = datetime.now(timezone.utc)
        if now > token.expires_at:
            return False
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE gate_tokens SET consumed_at=%s WHERE token=%s AND consumed_at IS NULL",
                (now, token_id),
            )
            return cur.rowcount == 1