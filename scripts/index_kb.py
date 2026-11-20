# -*- coding: utf-8 -*-
"""知识库向量索引命令行（S4）。

用法
    python scripts/index_kb.py                     # SQLite（本地默认）
    python scripts/index_kb.py --dsn <DSN>         # PostgreSQL
    python scripts/index_kb.py --force             # 强制重建（换了嵌入模型时用）
    python scripts/index_kb.py --dry-run           # 只看计划要嵌入多少条，不真调 API

为什么单独一个脚本，而不是启动时自动建
------------------------------------
每个进程启动都去调云端 API（计费），而且审核脚本每次都新建一个空库
→ 每次都要重新嵌入全部块。索引是**派生数据**，应该是发布流水线的一步（PRD 13.4.8）。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _setup_stdout() -> None:
    """自适应输出编码：tty 用控制台原生编码，非 tty 用 UTF-8，缺字用 replace 兜底。"""
    if os.environ.get("PYTHONIOENCODING"):
        return
    try:
        if sys.stdout.isatty():
            sys.stdout.reconfigure(errors="replace")
        else:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


_setup_stdout()

from tradedesk.config import get_settings              # noqa: E402
from tradedesk.data.kb_seed import KB_VERSION, load_kb  # noqa: E402
from tradedesk.retrieval.embedders import RemoteEmbedder  # noqa: E402
from tradedesk.retrieval.indexer import embedder_tag, index_kb  # noqa: E402
from tradedesk.store.postgres_store import PostgresStateStore  # noqa: E402
from tradedesk.store.sqlite_store import SqliteStateStore  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="建立知识库向量索引")
    ap.add_argument("--dsn", default="")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = get_settings()
    if not cfg.llm_api_key:
        print("没有读到 MINIMAX_API_KEY（见 .env），无法建索引。")
        return 1
    if args.dsn:
        store = PostgresStateStore(args.dsn, vector_dim=cfg.embedding_dim)
    else:
        store = SqliteStateStore(cfg.db_path)
    try:
        # 不碰库的全量清理（R15-03）：它会清 threads/customers/audit_log/gate_tokens，
        # 建向量索引与审计链无关，不能被索引脚本手法抹掉。
        # load_kb 是幂等重灌种子，且不删向量（R15-02）。
        load_kb(store, cfg.kb_version)
        emb = RemoteEmbedder(base_url=cfg.embedder_base_url, api_key=cfg.llm_api_key,
                              model=cfg.embedding_model, dim=cfg.embedding_dim,
                              timeout_s=cfg.llm_timeout_s, batch_size=cfg.embedder_batch_size,
                              provider=cfg.embedder_provider)
        blocks = store.list_kb_chunks(cfg.kb_version)
        tag = embedder_tag(emb)
        existing = {cid: model for cid, model, _ in store.list_kb_embeddings(cfg.kb_version)}
        todo = blocks if args.force else [b for b in blocks if existing.get(b.chunk_id) != tag]
        print(f"库版本：{cfg.kb_version}｜模型：{tag}｜块数：{len(blocks)}")
        print(f"待嵌入：{len(todo)} 条")
        if args.dry_run:
            print("（dry-run：未真正调用云端 API）")
            return 0
        stats = index_kb(store, emb, cfg.kb_version, chunks=blocks, force=args.force)
        print(f"完成：{stats}")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
