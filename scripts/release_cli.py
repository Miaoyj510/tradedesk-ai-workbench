# -*- coding: utf-8 -*-
"""发布与灰度命令行（S6-3；PRD 13.4.8）。

用法
    python scripts/release_cli.py status
    python scripts/release_cli.py create --kb-version kb-2026.11-d \
        --approver "张三" --change-notes "知识库换版本" \
        --rollback-plan "回滚 knowledge 类别到 kb-2026.11-c" \
        --pilot "ACME Trading" --lang en --lang es
    python scripts/release_cli.py advance REL-20261124-090000-13ad
    python scripts/release_cli.py rollback REL-20261124-090000-13ad --category knowledge
    python scripts/release_cli.py resolve --company "ACME Trading" --lang en

加 --dsn <DSN> 走 PostgreSQL，否则走 SQLite（默认 data/tradedesk.db）。

红线
----
* 推进档位**不提供跳过观察窗的开关** —— 观察窗是 PRD 的硬要求。
* 名单库（--list-version）变更必须带 --risk-evidence（PRD 13.4.8 要求重跑 L-6）。
* 提示词（--prompt-version）变更必须带 --regression-evidence。
"""
from __future__ import annotations

import argparse
import json
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

from tradedesk.config import get_settings                      # noqa: E402
from tradedesk.release.manifest import baseline_components     # noqa: E402
from tradedesk.release.service import ReleaseError, ReleaseService  # noqa: E402
from tradedesk.store.postgres_store import PostgresStateStore  # noqa: E402
from tradedesk.store.sqlite_store import SqliteStateStore      # noqa: E402

COMPONENT_FLAGS = (
    ("app_image", "--app-image"),
    ("agentspec_version", "--agentspec-version"),
    ("prompt_version", "--prompt-version"),
    ("glossary_version", "--glossary-version"),
    ("kb_version", "--kb-version"),
    ("list_version", "--list-version"),
)


def _build_store(dsn: str, sqlite_path: str):
    if dsn:
        return PostgresStateStore(dsn)
    return SqliteStateStore(sqlite_path)


def _build_service(store, cfg) -> ReleaseService:
    defaults = baseline_components(cfg.kb_version, cfg.list_version)
    return ReleaseService(store, defaults)


def _emit(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def main() -> int:
    ap = argparse.ArgumentParser(description="发布 / 灰度 / 回滚")
    ap.add_argument("--dsn", default="", help="PostgreSQL DSN（不给则用 SQLite）")
    ap.add_argument("--sqlite", default="", help="SQLite 文件路径（默认取配置）")

    # 子命令也认 --dsn/--sqlite（放在子命令前后都能写）。
    # 用 SUPPRESS 作默认值：不给的时候**不覆盖**主解析器已经读到的值
    # （argparse 的子解析器默认会盖掉同名属性，这里必须显式躲开）。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dsn", default=argparse.SUPPRESS,
                        help="PostgreSQL DSN（不给则用 SQLite）")
    common.add_argument("--sqlite", default=argparse.SUPPRESS,
                        help="SQLite 文件路径（默认取配置）")

    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="查看发布台账、当前稳定版与版本别名", parents=[common])

    p_create = sub.add_parser("create", help="建一次灰度发布", parents=[common])
    for key, flag in COMPONENT_FLAGS:
        p_create.add_argument(flag, dest=key, default="", help="要变更的组件版本号")
    p_create.add_argument("--approver", default="", help="审批人（必填）")
    p_create.add_argument("--change-notes", default="", help="变更说明（必填）")
    p_create.add_argument("--rollback-plan", default="", help="回滚预案（必填）")
    p_create.add_argument("--regression-evidence", default="", help="回归证据（Agent/提示词变更必填）")
    p_create.add_argument("--risk-evidence", default="", help="L-6 风控回归证据（名单变更必填）")
    p_create.add_argument("--pilot", action="append", default=[], help="灰度白名单企业（可多次）")
    p_create.add_argument("--lang", action="append", default=[], help="限定语种（可多次；不给=全语种）")
    p_create.add_argument("--release-id", default="", help="指定发布号（默认自动生成）")

    p_advance = sub.add_parser("advance", help="推进一档（PILOT → P20 → P50 → P100）", parents=[common])
    p_advance.add_argument("release_id")

    p_rb = sub.add_parser("rollback", help="按类别回滚", parents=[common])
    p_rb.add_argument("release_id")
    p_rb.add_argument("--category", required=True,
                      help="app / agent / knowledge / sanctions")

    p_resolve = sub.add_parser("resolve", help="查一次询盘实际命中哪一版", parents=[common])
    p_resolve.add_argument("--company", default="")
    p_resolve.add_argument("--lang", default="")

    args = ap.parse_args()
    cfg = get_settings()
    store = _build_store(args.dsn, args.sqlite or cfg.db_path)
    svc = _build_service(store, cfg)

    try:
        if args.cmd == "status":
            _emit(svc.status())
            return 0
        if args.cmd == "create":
            components = {k: getattr(args, k) for k, _f in COMPONENT_FLAGS
                          if getattr(args, k)}
            manifest = svc.create(
                components=components,
                approver=args.approver,
                change_notes=args.change_notes,
                rollback_plan=args.rollback_plan,
                regression_evidence=args.regression_evidence,
                risk_evidence=args.risk_evidence,
                pilot_companies=args.pilot,
                langs=args.lang,
                release_id=args.release_id or None,
                actor=args.approver or "cli")
            _emit(manifest.model_dump(mode="json"))
            return 0
        if args.cmd == "advance":
            manifest = svc.advance(args.release_id, actor="cli")
            _emit(manifest.model_dump(mode="json"))
            return 0
        if args.cmd == "rollback":
            manifest = svc.rollback(args.release_id, args.category, actor="cli")
            _emit(manifest.model_dump(mode="json"))
            return 0
        if args.cmd == "resolve":
            eff = svc.resolve(args.company, args.lang)
            _emit({"company": args.company, "lang": args.lang, "source": eff.source,
                   "release_id": eff.release_id, "components": eff.components})
            return 0
    except ReleaseError as exc:
        print(json.dumps({"error": exc.code, "message": exc.message},
                         ensure_ascii=False, indent=2))
        return 2
    finally:
        try:
            store.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())