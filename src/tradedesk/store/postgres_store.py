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
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .base import ConcurrentUpdate, audit_hash, enum_val, summarize_llm_usage
from ..data.kb_seed import KB_VERSION
from ..domain.models import (
    KBChunk,
    AuditEntry,
    CustomerProfile,
    GateToken,
    LLMUsage,
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

CREATE TABLE IF NOT EXISTS llm_usage (
    id        TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL DEFAULT '',
    draft_id  TEXT NOT NULL DEFAULT '',
    lang      TEXT NOT NULL DEFAULT 'en',
    provider  TEXT NOT NULL DEFAULT '',
    model     TEXT NOT NULL DEFAULT '',
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens     INTEGER NOT NULL DEFAULT 0,
    total_tokens      INTEGER NOT NULL DEFAULT 0,
    http_calls        INTEGER NOT NULL DEFAULT 0,
    cost_cny   DOUBLE PRECISION,
    price_tier TEXT NOT NULL DEFAULT '',
    degraded   BOOLEAN NOT NULL DEFAULT FALSE,
    ts         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_lang ON llm_usage(lang);
CREATE INDEX IF NOT EXISTS idx_llm_usage_thread ON llm_usage(thread_id);
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

    def __init__(self, dsn: str = DEFAULT_DSN, create: bool = True,
                 vector_dim: int = 1536) -> None:
        import psycopg  # 延迟导入：没装驱动也能用 SQLite 后端跑

        self.dsn = dsn
        self.vector_dim = int(vector_dim or 0)
        # embedding 列的实际类型："vector"(有 pgvector) / "jsonb"(降级)。
        # 它由初始化时探测决定，后面的写读都按它来分支。
        self.embedding_column = "jsonb"
        self.pgvector_available = False
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
            self._init_embedding_table(cur)

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

    # ---------- LLM 用量与成本（S5-1b） ----------
    def record_llm_usage(self, usage: LLMUsage) -> LLMUsage:
        """记一条云端模型用量（口径与 SQLite 一致）。"""
        if not usage.id:
            usage.id = uuid.uuid4().hex
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO llm_usage(id, thread_id, draft_id, lang, provider, model, "
                    "prompt_tokens, completion_tokens, cached_tokens, total_tokens, http_calls, "
                    "cost_cny, price_tier, degraded, ts) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (usage.id, usage.thread_id, usage.draft_id, usage.lang, usage.provider,
                     usage.model, usage.prompt_tokens, usage.completion_tokens,
                     usage.cached_tokens, usage.total_tokens, usage.http_calls,
                     usage.cost_cny, usage.price_tier, usage.degraded, usage.ts),
                )
        return usage

    def llm_usage_summary(self, lang: Optional[str] = None,
                          thread_id: Optional[str] = None) -> Dict[str, Any]:
        """按语种 / 线程汇总成本（口径与 SQLite 一致）。"""
        # 必须 SELECT thread_id：单位成本的分母是**询盘条数**（distinct thread_id），
        # 不是调用次数（R21-05）。与 SQLite 侧口径完全一致。
        sql = ("SELECT lang, thread_id, prompt_tokens, completion_tokens, cached_tokens, "
               "total_tokens, http_calls, cost_cny, degraded, ts FROM llm_usage WHERE TRUE")
        args: List[Any] = []
        if lang:
            sql += " AND lang = %s"
            args.append(lang)
        if thread_id:
            sql += " AND thread_id = %s"
            args.append(thread_id)
        with self.conn.cursor() as cur:
            cur.execute(sql, args)
            rows = [{"lang": r[0], "thread_id": r[1],
                     "prompt_tokens": r[2], "completion_tokens": r[3],
                     "cached_tokens": r[4], "total_tokens": r[5], "http_calls": r[6],
                     "cost_cny": r[7], "degraded": r[8],
                     "ts": r[8].isoformat() if r[8] else ""} for r in cur.fetchall()]
        return summarize_llm_usage(rows)

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
        # 按 chunk_id 排序（说明见 sqlite_store）：更新行会被挤到堆尾，不排序就不能靠序号定位块。
        sql, params = "SELECT * FROM kb_chunks", []
        if kb_version:
            sql += " WHERE kb_version=%s"
            params.append(kb_version)
        sql += " ORDER BY chunk_id"
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
        # 重灌块不删向量（R15-02）：向量是派生数据，块没变就不该重嵌
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM kb_chunks")

    # ---------- 向量（S4） ----------
    def _init_embedding_table(self, cur) -> None:
        """创建向量表：有 pgvector 就用 vector 列，没有就降级为 JSONB。

        为什么不直接把 vector 写进 DDL：列的维度取决于使用的嵌入模型，
        而扩展可能没装（本机是本地构建的镜像）。采用“探测扩展 → 选列型 → 已建表则认现有列型”，
        因为列类型一旦建好就不再变（变了就要重建表，向量重算）。
        """
        try:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            self.pgvector_available = True
        except Exception:
            self.pgvector_available = False
        cur.execute("SELECT udt_name FROM information_schema.columns "
                    "WHERE table_name='kb_embeddings' AND column_name='embedding'")
        row = cur.fetchone()
        if row is not None:
            self.embedding_column = "vector" if str(row[0]).lower() == "vector" else "jsonb"
            if self.embedding_column == "vector" and self.vector_dim > 0:
                # 现有 vector(N) 的 N 与当前嵌入模型不一致 → 重建该列。
                # 向量是**派生数据**（可从知识块重新算出），不是业务数据，所以直接重建列。
                cur.execute("SELECT atttypmod FROM pg_attribute "
                            "WHERE attrelid='kb_embeddings'::regclass AND attname='embedding'")
                old_dim = cur.fetchone()
                if old_dim and int(old_dim[0]) != int(self.vector_dim):
                    print(f"[pgvector] 向量列维度从 {int(old_dim[0])} 变为 {self.vector_dim}，重建该列"
                          f"（向量需重新索引）")
                    cur.execute("ALTER TABLE kb_embeddings DROP COLUMN embedding")
                    cur.execute(f"ALTER TABLE kb_embeddings ADD COLUMN embedding vector({self.vector_dim})")
                    # 旧行的向量已经丢了 → 清掉行，不留“有行但向量为空”的残骸（R15-04）
                    cur.execute("DELETE FROM kb_embeddings")
            # 老库补列：content_fp 是失效键的一部分（R15-01）
            cur.execute("SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='kb_embeddings' AND column_name='content_fp'")
            if cur.fetchone() is None:
                cur.execute("ALTER TABLE kb_embeddings ADD COLUMN content_fp TEXT NOT NULL DEFAULT ''")
            self._ensure_hnsw_index(cur)
            return
        if self.pgvector_available and self.vector_dim > 0:
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS kb_embeddings ("
                f"  chunk_id   TEXT PRIMARY KEY,"
                f"  kb_version TEXT NOT NULL DEFAULT '',"
                f"  model      TEXT NOT NULL DEFAULT '',"
                f"  dim        INTEGER NOT NULL DEFAULT 0,"
                f"  embedding  vector({self.vector_dim}),"
                f"  content_fp TEXT NOT NULL DEFAULT '',"
                f"  updated_at TIMESTAMPTZ NOT NULL DEFAULT now())")
            self.embedding_column = "vector"
            self._ensure_hnsw_index(cur)
        else:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS kb_embeddings ("
                "  chunk_id   TEXT PRIMARY KEY,"
                "  kb_version TEXT NOT NULL DEFAULT '',"
                "  model      TEXT NOT NULL DEFAULT '',"
                "  dim        INTEGER NOT NULL DEFAULT 0,"
                "  embedding  JSONB NOT NULL DEFAULT '[]'::jsonb,"
                "  content_fp TEXT NOT NULL DEFAULT '',"
                "  updated_at TIMESTAMPTZ NOT NULL DEFAULT now())")
            self.embedding_column = "jsonb"

    def _ensure_hnsw_index(self, cur) -> None:
        """ANN 索引：防量化下推到 pgvector 时，线性扫描会成为 50–100 并发下的瓶颈（N7）。

        必须在**存量库**也执行（R17-02）：旧写法只在“表不存在”的分支里建索引，
        所以从 4a 升上来的部署永远只有主键索引 —— 而验收条款 4b-07 写的是「走 pgvector + HNSW」。
        失败要打日志，不能静默。
        """
        try:
            cur.execute("CREATE INDEX IF NOT EXISTS idx_kb_embeddings_hnsw "
                        "ON kb_embeddings USING hnsw (embedding vector_cosine_ops)")
        except Exception as exc:
            print(f"[pgvector] HNSW 索引创建失败（近邻检索将退化为全表扫描）：{exc}")

    def upsert_kb_embedding(self, chunk_id, kb_version, model, vector, content_fp="") -> None:
        vec = [float(x) for x in vector]
        with self.conn.cursor() as cur:
            if self.embedding_column == "vector":
                cur.execute(
                    "INSERT INTO kb_embeddings(chunk_id, kb_version, model, dim, embedding, content_fp, updated_at) "
                    "VALUES(%s,%s,%s,%s, CAST(%s AS vector), %s, now()) "
                    "ON CONFLICT(chunk_id) DO UPDATE SET kb_version=excluded.kb_version, "
                    "model=excluded.model, dim=excluded.dim, embedding=excluded.embedding, "
                    "content_fp=excluded.content_fp, updated_at=now()",
                    (chunk_id, kb_version, model, len(vec),
                     "[" + ",".join(f"{x:.6f}" for x in vec) + "]", content_fp or ""),
                )
            else:
                from psycopg.types.json import Json
                cur.execute(
                    "INSERT INTO kb_embeddings(chunk_id, kb_version, model, dim, embedding, content_fp, updated_at) "
                    "VALUES(%s,%s,%s,%s,%s,%s, now()) "
                    "ON CONFLICT(chunk_id) DO UPDATE SET kb_version=excluded.kb_version, "
                    "model=excluded.model, dim=excluded.dim, embedding=excluded.embedding, "
                    "content_fp=excluded.content_fp, updated_at=now()",
                    (chunk_id, kb_version, model, len(vec), Json(vec), content_fp or ""),
                )

    def list_kb_embeddings(self, kb_version=None):
        sql, params = "SELECT chunk_id, model, embedding FROM kb_embeddings", []
        if kb_version:
            sql += " WHERE kb_version=%s"
            params.append(kb_version)
        sql += " ORDER BY chunk_id"
        out = []
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            for chunk_id, model, raw in cur.fetchall():
                if isinstance(raw, str):
                    try:
                        vec = json.loads(raw)
                    except (TypeError, ValueError):
                        vec = []
                elif isinstance(raw, (list, tuple)):
                    vec = list(raw)
                else:
                    vec = []
                if not vec:
                    # 空向量（换维度后的残骸）不算一条有效索引（R15-04）
                    continue
                out.append((chunk_id, model, [float(x) for x in vec]))
        return out

    def search_kb_vectors(self, query_vec, top_k: int = 10, kb_version: str = ""):
        """pgvector 近邻检索（走 HNSW 索引）→ [(chunk_id, score)]。

        只在列类是 vector 时可用；JSONB 降级下返回空列表（调用方回退到内存索引）。
        """
        if self.embedding_column != "vector":
            return []
        vec = "[" + ",".join(f"{float(x):.6f}" for x in query_vec) + "]"
        version = kb_version or KB_VERSION
        with self.conn.cursor() as cur:
            # 必须按库版本过滤（R17-01）：内存口径是过滤的，PG 不过滤就会让旧版本向量
            # 占满 LIMIT 的名额 → 当前库的向量被挤出结果，稠密通道在 PG 上静默变空。
            cur.execute(
                "SELECT chunk_id, 1 - (embedding <=> CAST(%s AS vector)) AS score "
                "FROM kb_embeddings WHERE embedding IS NOT NULL AND kb_version = %s "
                "ORDER BY embedding <=> CAST(%s AS vector) LIMIT %s",
                (vec, version, vec, int(top_k)))
            return [(cid, float(score)) for cid, score in cur.fetchall()]

    def list_kb_embedding_meta(self, kb_version=None):
        sql, params = "SELECT chunk_id, model, content_fp FROM kb_embeddings", []
        if kb_version:
            sql += " WHERE kb_version=%s"
            params.append(kb_version)
        sql += " ORDER BY chunk_id"
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return [(cid, model, fp or "") for cid, model, fp in cur.fetchall()]

    def clear_kb_embeddings(self, kb_version=None) -> None:
        with self.conn.cursor() as cur:
            if kb_version:
                cur.execute("DELETE FROM kb_embeddings WHERE kb_version=%s", (kb_version,))
            else:
                cur.execute("DELETE FROM kb_embeddings")

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