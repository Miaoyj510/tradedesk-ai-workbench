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
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .base import (ConcurrentUpdate, audit_hash, canonical_ts, enum_val,
                   summarize_llm_usage)
from ..domain.models import (
    KBChunk,
    AuditEntry,
    CustomerProfile,
    GateToken,
    LLMUsage,
    ThreadEvent,
    ThreadState,
)
from ..release.manifest import ReleaseManifest

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
    message_hash TEXT NOT NULL DEFAULT '',
    version     INTEGER NOT NULL,
    payload     TEXT NOT NULL,
    updated_at  TEXT NOT NULL
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

CREATE TABLE IF NOT EXISTS kb_embeddings (
    chunk_id   TEXT NOT NULL,
    kb_version TEXT NOT NULL DEFAULT '',
    model      TEXT NOT NULL DEFAULT '',
    dim        INTEGER NOT NULL DEFAULT 0,
    vector     TEXT NOT NULL DEFAULT '[]',
    content_fp TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (chunk_id, kb_version)
);;
CREATE TABLE IF NOT EXISTS kb_chunks (
    chunk_id        TEXT NOT NULL,
    doc_id         TEXT NOT NULL,
    doc_type       TEXT NOT NULL,
    title          TEXT NOT NULL DEFAULT '',
    text           TEXT NOT NULL,
    lang           TEXT NOT NULL DEFAULT 'zh',
    identifiers    TEXT NOT NULL DEFAULT '{}',
    countries      TEXT NOT NULL DEFAULT '[]',
    hs_codes       TEXT NOT NULL DEFAULT '[]',
    tags           TEXT NOT NULL DEFAULT '[]',
    effective_from TEXT,
    effective_to   TEXT,
    valid_until    TEXT,
    kb_version     TEXT NOT NULL DEFAULT '',
    extra          TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (chunk_id, kb_version)
);
CREATE INDEX IF NOT EXISTS idx_kb_version ON kb_chunks(kb_version);
CREATE INDEX IF NOT EXISTS idx_kb_doc_type ON kb_chunks(doc_type);

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

CREATE TABLE IF NOT EXISTS llm_usage (
    id        TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL DEFAULT '',
    draft_id  TEXT NOT NULL DEFAULT '',
    lang      TEXT NOT NULL DEFAULT 'en',
    provider  TEXT NOT NULL DEFAULT '',
    model     TEXT NOT NULL DEFAULT '',
    prompt_tokens    INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens    INTEGER NOT NULL DEFAULT 0,
    total_tokens     INTEGER NOT NULL DEFAULT 0,
    http_calls       INTEGER NOT NULL DEFAULT 0,
    cost_cny  REAL,
    price_tier TEXT NOT NULL DEFAULT '',
    degraded  INTEGER NOT NULL DEFAULT 0,
    ts        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_lang ON llm_usage(lang);
CREATE INDEX IF NOT EXISTS idx_llm_usage_thread ON llm_usage(thread_id);

-- 发布与灰度（S6-3）：发布清单 + 版本别名（按类别回滚靠别名切换）
CREATE TABLE IF NOT EXISTS releases (
    release_id  TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS release_alias (
    category    TEXT PRIMARY KEY,
    release_id  TEXT NOT NULL,
    components  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""


# 复合主键建表片段（迁移时重建表用；DDL 里保持同一份定义）
SQLITE_KB_CHUNKS_DDL = """
CREATE TABLE IF NOT EXISTS kb_chunks (
    chunk_id        TEXT NOT NULL,
    doc_id         TEXT NOT NULL,
    doc_type       TEXT NOT NULL,
    title          TEXT NOT NULL DEFAULT '',
    text           TEXT NOT NULL,
    lang           TEXT NOT NULL DEFAULT 'zh',
    identifiers    TEXT NOT NULL DEFAULT '{}',
    countries      TEXT NOT NULL DEFAULT '[]',
    hs_codes       TEXT NOT NULL DEFAULT '[]',
    tags           TEXT NOT NULL DEFAULT '[]',
    effective_from TEXT,
    effective_to   TEXT,
    valid_until    TEXT,
    kb_version     TEXT NOT NULL DEFAULT '',
    extra          TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (chunk_id, kb_version)
);
"""

SQLITE_KB_CHUNKS_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_kb_version ON kb_chunks(kb_version);
CREATE INDEX IF NOT EXISTS idx_kb_doc_type ON kb_chunks(doc_type);
"""

SQLITE_KB_EMBEDDINGS_DDL = """
CREATE TABLE IF NOT EXISTS kb_embeddings (
    chunk_id   TEXT NOT NULL,
    kb_version TEXT NOT NULL DEFAULT '',
    model      TEXT NOT NULL DEFAULT '',
    dim        INTEGER NOT NULL DEFAULT 0,
    vector     TEXT NOT NULL DEFAULT '[]',
    content_fp TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (chunk_id, kb_version)
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
        # 先给老库补列，再建索引 —— DDL 里有依赖新列的唯一索引，顺序反了会直接炸
        try:
            tcols = {r[1] for r in self._conn.execute("PRAGMA table_info(threads)")}
            if tcols and "message_hash" not in tcols:
                self._conn.execute(
                    "ALTER TABLE threads ADD COLUMN message_hash TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        # 复合主键迁移必须**在** executescript 之前：
        # 老库的两张表是单列 chunk_id 主键，同名块在两个库版本下会互相顶掉 ——
        # 双索引（PRD 13.4.8）就立不住。新库没有这两张表，迁移直接跳过。
        self._migrate_kb_composite_pk()
        self._conn.executescript(DDL)
        # 轻量迁移：老库补列（CREATE TABLE IF NOT EXISTS 不会给已存在的表加列）
        _cols = {r[1] for r in self._conn.execute("PRAGMA table_info(gate_tokens)")}
        if "action" not in _cols:
            self._conn.execute("ALTER TABLE gate_tokens ADD COLUMN action TEXT NOT NULL DEFAULT ''")
        # 向量表：补 content_fp 列（失效键要含块内容指纹，R15-01）
        _ecols = {r[1] for r in self._conn.execute("PRAGMA table_info(kb_embeddings)")}
        if _ecols and "content_fp" not in _ecols:
            self._conn.execute("ALTER TABLE kb_embeddings ADD COLUMN content_fp TEXT NOT NULL DEFAULT ''")
        cur = self._conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'")
        row = cur.fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        self._conn.commit()

    def _migrate_kb_composite_pk(self) -> None:
        """把 kb_chunks / kb_embeddings 的主键从单列 chunk_id 改成 (chunk_id, kb_version)。

        为什么必须改：主键是单列时，两个库版本里的**同名块**会互相顶掉 ——
        "双索引 + 别名切换"（PRD 13.4.8）根本立不住：建新版本等于毁掉旧版本，
        回滚只能重建（重建窗口 = 服务不可用）。

        改成复合主键后：同名块可以在两个版本下并存，回滚只是切别名，**不重建**。
        迁移是重建表（数据全量保留）：块与向量都是可再生的派生数据，迁移不丢业务数据。
        """
        for table, ddl, indexes in (
            ("kb_chunks", SQLITE_KB_CHUNKS_DDL, SQLITE_KB_CHUNKS_INDEXES),
            ("kb_embeddings", SQLITE_KB_EMBEDDINGS_DDL, ""),
        ):
            exists = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if not exists:
                continue
            info = list(self._conn.execute("PRAGMA table_info(%s)" % table))
            pk_cols = [r[1] for r in sorted(info, key=lambda r: r[5]) if r[5] > 0]
            if "kb_version" in pk_cols:
                continue          # 已是复合主键
            # 旧索引跟着旧表走：先删掉（否则同名索引会让新表建不出索引）
            for idx in ("idx_kb_version", "idx_kb_doc_type"):
                try:
                    self._conn.execute("DROP INDEX IF EXISTS %s" % idx)
                except sqlite3.OperationalError:
                    pass
            self._conn.execute("ALTER TABLE %s RENAME TO %s_old" % (table, table))
            self._conn.execute(ddl)
            self._conn.execute("INSERT INTO %s SELECT * FROM %s_old" % (table, table))
            self._conn.execute("DROP TABLE %s_old" % table)
            if indexes:
                self._conn.executescript(indexes)
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

    # ---------- 知识库 ----------
    def upsert_kb_chunk(self, chunk) -> None:
        with self._write_lock:
            self._conn.execute(
                "INSERT INTO kb_chunks(chunk_id, doc_id, doc_type, title, text, lang, identifiers, "
                "countries, hs_codes, tags, effective_from, effective_to, valid_until, kb_version, extra) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(chunk_id, kb_version) DO UPDATE SET "
                "doc_id=excluded.doc_id, doc_type=excluded.doc_type, title=excluded.title, "
                "text=excluded.text, lang=excluded.lang, identifiers=excluded.identifiers, "
                "countries=excluded.countries, hs_codes=excluded.hs_codes, tags=excluded.tags, "
                "effective_from=excluded.effective_from, effective_to=excluded.effective_to, "
                "valid_until=excluded.valid_until, kb_version=excluded.kb_version, extra=excluded.extra",
                (
                    chunk.chunk_id, chunk.doc_id, chunk.doc_type, chunk.title, chunk.text, chunk.lang,
                    _dumps(chunk.identifiers), _dumps(chunk.countries), _dumps(chunk.hs_codes),
                    _dumps(chunk.tags),
                    chunk.effective_from.isoformat() if chunk.effective_from else None,
                    chunk.effective_to.isoformat() if chunk.effective_to else None,
                    chunk.valid_until.isoformat() if chunk.valid_until else None,
                    chunk.kb_version, _dumps(chunk.extra),
                ),
            )
            self._conn.commit()

    def list_kb_chunks(self, kb_version=None):
        # 按 chunk_id 排序：不排序时行序取决于写入顺序，而一条记录被更新后会被挤到末尾 ——
        # “哪个块是这个型号的规格记录”不能靠序号猜（PostgreSQL 更新行会移到堆尾）。
        sql, params = "SELECT * FROM kb_chunks", []
        if kb_version:
            sql += " WHERE kb_version=?"
            params.append(kb_version)
        sql += " ORDER BY chunk_id"
        rows = self._conn.execute(sql, params).fetchall()
        return [
            KBChunk(
                chunk_id=r["chunk_id"], doc_id=r["doc_id"], doc_type=r["doc_type"], title=r["title"],
                text=r["text"], lang=r["lang"], identifiers=json.loads(r["identifiers"]),
                countries=json.loads(r["countries"]), hs_codes=json.loads(r["hs_codes"]),
                tags=json.loads(r["tags"]),
                effective_from=datetime.fromisoformat(r["effective_from"]) if r["effective_from"] else None,
                effective_to=datetime.fromisoformat(r["effective_to"]) if r["effective_to"] else None,
                valid_until=datetime.fromisoformat(r["valid_until"]) if r["valid_until"] else None,
                kb_version=r["kb_version"], extra=json.loads(r["extra"]),
            )
            for r in rows
        ]

    def get_kb_chunk(self, chunk_id, kb_version=None):
        """按块 ID 取块；给了 kb_version 就精确到那一个版本。

        复合主键之后同一 chunk_id 会在多个库版本下并存，
        所以**必须能按版本取**（不指定版本时取版本号最小的那一个）。
        """
        sql = "SELECT * FROM kb_chunks WHERE chunk_id=?"
        params = [chunk_id]
        if kb_version:
            sql += " AND kb_version=?"
            params.append(kb_version)
        sql += " ORDER BY kb_version"
        rows = self._conn.execute(sql, params).fetchall()
        if not rows:
            return None
        r = rows[0]
        return KBChunk(
            chunk_id=r["chunk_id"], doc_id=r["doc_id"], doc_type=r["doc_type"], title=r["title"],
            text=r["text"], lang=r["lang"], identifiers=json.loads(r["identifiers"]),
            countries=json.loads(r["countries"]), hs_codes=json.loads(r["hs_codes"]),
            tags=json.loads(r["tags"]),
            effective_from=datetime.fromisoformat(r["effective_from"]) if r["effective_from"] else None,
            effective_to=datetime.fromisoformat(r["effective_to"]) if r["effective_to"] else None,
            valid_until=datetime.fromisoformat(r["valid_until"]) if r["valid_until"] else None,
            kb_version=r["kb_version"], extra=json.loads(r["extra"]),
        )

    def clear_kb(self, kb_version=None) -> None:
        # 重灌知识块不得连带删向量（R15-02）：向量与块一一对应，
        # 块没变就不该删向量 —— 否则每次重灌都要全量重嵌（白花钱）。
        # 给 kb_version 时**只清该版本**（双索引的前提：灌基线不能抹掉灰度版）。
        with self._write_lock:
            if kb_version:
                self._conn.execute("DELETE FROM kb_chunks WHERE kb_version=?", (kb_version,))
            else:
                self._conn.execute("DELETE FROM kb_chunks")
            self._conn.commit()

    # ---------- 向量（S4） ----------
    def upsert_kb_embedding(self, chunk_id, kb_version, model, vector, content_fp="") -> None:
        with self._write_lock:
            self._conn.execute(
                "INSERT INTO kb_embeddings(chunk_id, kb_version, model, dim, vector, content_fp, updated_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(chunk_id, kb_version) DO UPDATE SET "
                "model=excluded.model, dim=excluded.dim, "
                "vector=excluded.vector, content_fp=excluded.content_fp, updated_at=excluded.updated_at",
                (chunk_id, kb_version, model, len(vector), _dumps([float(x) for x in vector]),
                 content_fp or "", datetime.now(timezone.utc).isoformat()),
            )
            self._conn.commit()

    def list_kb_embedding_meta(self, kb_version=None):
        sql, params = "SELECT chunk_id, model, content_fp FROM kb_embeddings", []
        if kb_version:
            sql += " WHERE kb_version=?"
            params.append(kb_version)
        sql += " ORDER BY chunk_id"
        return [(r["chunk_id"], r["model"], r["content_fp"])
                for r in self._conn.execute(sql, params).fetchall()]

    def list_kb_embeddings(self, kb_version=None):
        sql, params = "SELECT chunk_id, model, vector FROM kb_embeddings", []
        if kb_version:
            sql += " WHERE kb_version=?"
            params.append(kb_version)
        sql += " ORDER BY chunk_id"
        out = []
        for row in self._conn.execute(sql, params).fetchall():
            try:
                vec = json.loads(row["vector"])
            except (TypeError, ValueError):
                vec = []
            out.append((row["chunk_id"], row["model"], [float(x) for x in vec]))
        return out

    def clear_kb_embeddings(self, kb_version=None) -> None:
        with self._write_lock:
            if kb_version:
                self._conn.execute("DELETE FROM kb_embeddings WHERE kb_version=?", (kb_version,))
            else:
                self._conn.execute("DELETE FROM kb_embeddings")
            self._conn.commit()

    # ---------- 演练与验证辅助（生产也可用：篡改演练、令牌过期演练） ----------
    def reset(self) -> None:
        """清空业务表，便于验证脚本重复运行。"""
        with self._write_lock:
            self._conn.executescript(
                "DELETE FROM threads; DELETE FROM customers; DELETE FROM thread_events; "
                "DELETE FROM audit_log; DELETE FROM gate_tokens; "
                "DELETE FROM releases; DELETE FROM release_alias;"
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
            "INSERT INTO threads(thread_id, buyer_id, stage, language, message_hash, version, payload, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                thread.thread_id, thread.buyer_id, enum_val(thread.stage), enum_val(thread.language),
                thread.last_message_hash,
                thread.version, _dumps(thread.model_dump(mode="json")),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._conn.commit()

    def create_thread_if_absent(self, thread: ThreadState) -> bool:
        """原子建线程：已存在同 id 或同 (买家, 消息哈希) 时返回 False（= 重复消息）。"""
        with self._write_lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO threads(thread_id, buyer_id, stage, language, message_hash, version, payload, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    thread.thread_id, thread.buyer_id, enum_val(thread.stage), enum_val(thread.language),
                    thread.last_message_hash,
                    thread.version, _dumps(thread.model_dump(mode="json")),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def allocate_thread_id(self, prefix: str = "T-", start: int = 2000) -> str:
        """持久取号：计数器存在 schema_meta —— 进程重启、多实例都不撞号。"""
        with self._write_lock:
            row = self._conn.execute(
                "SELECT value FROM schema_meta WHERE key='thread_seq'").fetchone()
            nxt = (int(row["value"]) if row else 0) + 1
            self._conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES('thread_seq', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(nxt),))
            self._conn.commit()
            return f"{prefix}{start + nxt}"

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
                raise ConcurrentUpdate(
                    thread.thread_id,
                    existing.version if existing else "?", thread.version)
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

    # ---------- LLM 用量与成本（S5-1b） ----------
    def record_llm_usage(self, usage: LLMUsage) -> LLMUsage:
        """记一条云端模型用量。

        成本不在这里算：算完的用量已经带着价格与档位（见 llm/pricing.py）。
        单价未核实时 cost_cny 为 None（不猜、不估算）。
        """
        if not usage.id:
            usage.id = uuid.uuid4().hex
        with self._write_lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO llm_usage(id, thread_id, draft_id, lang, provider, model, "
                "prompt_tokens, completion_tokens, cached_tokens, total_tokens, http_calls, "
                "cost_cny, price_tier, degraded, ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (usage.id, usage.thread_id, usage.draft_id, usage.lang, usage.provider,
                 usage.model, usage.prompt_tokens, usage.completion_tokens,
                 usage.cached_tokens, usage.total_tokens, usage.http_calls,
                 usage.cost_cny, usage.price_tier, 1 if usage.degraded else 0,
                 usage.ts.isoformat()),
            )
            self._conn.commit()
        return usage

    def llm_usage_summary(self, lang: Optional[str] = None,
                          thread_id: Optional[str] = None) -> Dict[str, Any]:
        """按语种 / 线程汇总成本（口径见 store/base.py 的 summarize_llm_usage）。"""
        # 必须 SELECT thread_id：单位成本的分母是**询盘条数**（distinct thread_id），
        # 不是调用次数（R21-05）。少了这一列，thread_count 恒为 0。
        sql = ("SELECT lang, thread_id, prompt_tokens, completion_tokens, cached_tokens, "
               "total_tokens, http_calls, cost_cny, degraded, ts FROM llm_usage WHERE 1=1")
        args: List[Any] = []
        if lang:
            sql += " AND lang = ?"
            args.append(lang)
        if thread_id:
            sql += " AND thread_id = ?"
            args.append(thread_id)
        rows = [dict(r) for r in self._conn.execute(sql, args).fetchall()]
        return summarize_llm_usage(rows)

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

    # ---------- 发布与灰度（S6-3） ----------
    def save_release(self, manifest) -> None:
        """发布清单落库：整份档案存 payload，字段演进不需要改表。"""
        with self._write_lock:
            self._conn.execute(
                "INSERT INTO releases(release_id, payload, created_at, updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(release_id) DO UPDATE SET payload=excluded.payload, "
                "updated_at=excluded.updated_at",
                (manifest.release_id, _dumps(manifest.model_dump(mode="json")),
                 manifest.created_at.isoformat(), manifest.updated_at.isoformat()),
            )
            self._conn.commit()

    def get_release(self, release_id: str):
        row = self._conn.execute(
            "SELECT payload FROM releases WHERE release_id=?", (release_id,)).fetchone()
        return ReleaseManifest.model_validate_json(row["payload"]) if row else None

    def list_releases(self):
        rows = self._conn.execute(
            "SELECT payload FROM releases ORDER BY created_at, release_id").fetchall()
        return [ReleaseManifest.model_validate_json(r["payload"]) for r in rows]

    def set_release_alias(self, category: str, release_id: str, components) -> None:
        """版本别名：某类别当前指向哪套组件（回滚就是改这一行）。"""
        with self._write_lock:
            self._conn.execute(
                "INSERT INTO release_alias(category, release_id, components, updated_at) "
                "VALUES(?,?,?,?) ON CONFLICT(category) DO UPDATE SET "
                "release_id=excluded.release_id, components=excluded.components, "
                "updated_at=excluded.updated_at",
                (category, release_id, _dumps(dict(components or {})),
                 datetime.now(timezone.utc).isoformat()),
            )
            self._conn.commit()

    def get_release_alias(self, category: str):
        row = self._conn.execute(
            "SELECT category, release_id, components, updated_at FROM release_alias "
            "WHERE category=?", (category,)).fetchone()
        if not row:
            return None
        return {"category": row["category"], "release_id": row["release_id"],
                "components": json.loads(row["components"]),
                "updated_at": row["updated_at"]}

    def list_release_aliases(self):
        rows = self._conn.execute(
            "SELECT category, release_id, components, updated_at FROM release_alias "
            "ORDER BY category").fetchall()
        return {r["category"]: {"category": r["category"], "release_id": r["release_id"],
                                "components": json.loads(r["components"]),
                                "updated_at": r["updated_at"]} for r in rows}

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