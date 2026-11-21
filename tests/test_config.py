# -*- coding: utf-8 -*-
"""配置读取：环境变量 > .env 文件 > 默认值。

盯的是两件容易出事的事：
1. `.env.example` 里写了什么，配置就真得读什么 —— 否则照着模板切 PostgreSQL
   会静默退回 SQLite，没人发现。
2. SQLite 路径必须按项目根解析成绝对路径 —— 两个 agent 各有 worktree，
   相对路径会让两个进程共用同一个库文件。
"""
import os
import shutil
from pathlib import Path

import pytest

from tradedesk.config import Config, reset_settings

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """把可能影响配置的变量清干净，避免被本机环境干扰。"""
    for key in (
        "TRADEDESK_DB_PATH", "TRADEDESK_DATABASE_URL", "TRADEDESK_REDIS_URL",
        "TRADEDESK_LLM_PROVIDER", "TRADEDESK_LLM_MODEL", "MINIMAX_API_KEY",
        "MINIMAX_BASE_URL", "TRADEDESK_LLM_TIMEOUT_S", "TRADEDESK_EMBEDDING_MODEL",
        "TRADEDESK_RERANKER_MODEL", "TRADEDESK_KB_VERSION", "TRADEDESK_LIST_VERSION",
        "TRADEDESK_LOW_CONFIDENCE_THRESHOLD", "TRADEDESK_AMOUNT_THRESHOLD_USD",
        "TRADEDESK_GATE_TOKEN_TTL_S", "TRADEDESK_MAX_CONCURRENCY",
        "TRADEDESK_HOST", "TRADEDESK_PORT", "TRADEDESK_ENV",
    ):
        monkeypatch.delenv(key, raising=False)
    reset_settings()
    yield
    reset_settings()


def test_db_path_is_absolute_under_project_root():
    cfg = Config.load(project_root=ROOT)
    assert os.path.isabs(cfg.db_path)
    assert cfg.db_path.startswith(str(ROOT))
    assert cfg.db_path.endswith("tradedesk.db")


def test_each_worktree_gets_its_own_database_file():
    """两个 worktree 的库文件必须不是同一个。

    ⚠ 不能用 `ROOT.parent / "agent-a"` 这种写法：它只在"从主仓库跑"时成立；
    在 worktree 里跑时 ROOT.parent/"agent-a" 恰好等于 ROOT 自己，测试会自相矛盾地失败。
    这里改成与运行位置无关的写法：从 git common-dir 反推主仓库根，再与当前 worktree 比较。
    """
    import subprocess

    git = shutil.which("git") or r"C:\Users\DELL\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\git\cmd\git.exe"
    common = subprocess.run(
        [git, "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=str(ROOT), capture_output=True, text=True)
    repo_root = Path(common.stdout.strip()).parent if common.returncode == 0 else ROOT.parent
    a = Config.load(project_root=repo_root)
    b = Config.load(project_root=ROOT)
    assert a.db_path != b.db_path, f"主仓库与 worktree 共用同一个库文件：{a.db_path}"
    assert str(ROOT) in b.db_path, "库路径必须按项目根解析，而不是当前工作目录"


def test_absolute_db_path_is_left_alone():
    os.environ["TRADEDESK_DB_PATH"] = str(Path("C:/tmp") / "x.db")
    cfg = Config.load(project_root=ROOT)
    assert cfg.db_path == str(Path("C:/tmp") / "x.db")


def test_env_var_overrides_dotenv(tmpdir_local, monkeypatch):
    (tmpdir_local / ".env").write_text(
        "TRADEDESK_LLM_MODEL=from-dotenv\nTRADEDESK_KB_VERSION=kb-dotenv\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TRADEDESK_LLM_MODEL", "from-env")
    cfg = Config.load(project_root=tmpdir_local)
    assert cfg.llm_model == "from-env"          # 环境变量优先
    assert cfg.kb_version == "kb-dotenv"        # .env 兜底


def test_dotenv_only_applies_when_env_var_absent(tmpdir_local):
    (tmpdir_local / ".env").write_text("TRADEDESK_LLM_MODEL=from-dotenv\n", encoding="utf-8")
    cfg = Config.load(project_root=tmpdir_local)
    assert cfg.llm_model == "from-dotenv"


def test_example_template_vars_are_all_readable():
    """`.env.example` 里出现的每个 TRADEDESK_* 变量，配置都得真的读。"""
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    declared = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        declared.add(line.split("=", 1)[0].strip())

    import tradedesk.config as cfgmod
    source = Path(cfgmod.__file__).read_text(encoding="utf-8")
    # 厂商专用的 key / base_url 变量名由厂商表统一管理（见 llm/providers.py），
    # 不在 config.py 里写死；新增厂商时这里自动跳过，不用再手动补免费列表。
    from tradedesk.llm.providers import PROVIDERS
    vendor_envs = {pv.api_key_env for pv in PROVIDERS.values()}
    vendor_envs |= {pv.base_url_env for pv in PROVIDERS.values() if pv.base_url_env}
    for name in declared:
        if name in vendor_envs:
            continue
        assert name in source, f"{name} 写在 .env.example 里，但配置里没有读"


def test_database_url_and_redis_url_are_read(monkeypatch):
    monkeypatch.setenv("TRADEDESK_DATABASE_URL", "postgresql://u:p@127.0.0.1:5433/tradedesk")
    monkeypatch.setenv("TRADEDESK_REDIS_URL", "redis://127.0.0.1:6380/0")
    cfg = Config.load(project_root=ROOT)
    assert cfg.database_url.endswith("/tradedesk")
    assert cfg.redis_url.endswith("/0")


def test_defaults_when_nothing_configured():
    cfg = Config.load(project_root=ROOT)
    assert cfg.database_url == ""       # 空 = 用 SQLite
    assert cfg.low_confidence_threshold == 0.55
    assert cfg.amount_threshold_usd == 5000.0
    assert cfg.gate_token_ttl_s == 900
    assert cfg.max_concurrency == 4
