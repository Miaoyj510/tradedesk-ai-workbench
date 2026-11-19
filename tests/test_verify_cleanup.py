# -*- coding: utf-8 -*-
"""验证脚本的清理动作必须只清"本 agent 自己的库"。

两个 agent 各用一个 PostgreSQL 库（tradedesk_a / tradedesk_b）。
清理时如果连的是默认共享库 `tradedesk`，就会把对方的库清空 —— 这条测试守住这条边界。
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


def _load_verify_script():
    """按文件路径加载 scripts/verify_s1.py（scripts 不是包，import 不到）。"""
    path = ROOT / "scripts" / "verify_s1.py"
    spec = importlib.util.spec_from_file_location("verify_s1_under_test", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cleanup_clears_only_the_given_dsn(monkeypatch, tmpdir_local):
    import tradedesk.store.postgres_store as pg

    captured = {}

    class _FakeStore:
        def __init__(self, dsn):
            captured["dsn"] = dsn

        def reset(self):
            captured["reset"] = True

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(pg, "PostgresStateStore", _FakeStore)

    mod = _load_verify_script()
    dsn = "postgresql://tradedesk:tradedesk_local_dev@127.0.0.1:5433/tradedesk_b"
    mod.purge_artifacts(tmpdir_local / "x.db", postgres=True, dsn=dsn)

    # 清的是传进来的那个库，不是默认共享库
    assert captured.get("dsn") == dsn
    assert captured.get("reset") is True
    assert captured.get("closed") is True
    assert pg.DEFAULT_DSN != dsn


def test_cleanup_of_shared_default_dsn_is_still_possible():
    """没有指定独立库时（单 agent 本地开发），默认库仍然能被清 —— 但要显式传。"""
    import tradedesk.store.postgres_store as pg

    mod = _load_verify_script()
    assert mod.purge_artifacts.__defaults__ is not None
    # 默认参数就是共享库；两个 agent 场景下必须显式传自己的库
    assert pg.DEFAULT_DSN.endswith("/tradedesk")
