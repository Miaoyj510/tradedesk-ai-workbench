# -*- coding: utf-8 -*-
"""全局配置：环境变量 > .env 文件 > 默认值。

配置项与 PRD 8.5 / 12.3 / 13.4 对应；敏感信息（模型 API Key）只从环境变量读取，
不写进代码库，也不落日志。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional


def _load_dotenv(path: Path) -> Dict[str, str]:
    """极简 .env 解析（避免为一个小功能引入依赖）。"""
    env: Dict[str, str] = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


@dataclass
class Config:
    # 存储
    db_path: str = "data/tradedesk.db"
    # 大模型（云 API Key 方式；S1 阶段不调用，仅登记配置）
    llm_provider: str = "minimax"
    llm_model: str = "MiniMax-Text-01"
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_timeout_s: int = 30
    # 检索
    embedding_model: str = "BGE-M3"
    reranker_model: str = "bge-reranker-v2-m3"
    kb_version: str = "kb-2026.11-b"
    list_version: str = "sanctions-2026.11-a"
    # 业务阈值
    low_confidence_threshold: float = 0.55
    amount_threshold_usd: float = 5000.0
    gate_token_ttl_s: int = 900
    # 并发/限流
    max_concurrency: int = 4
    # 服务
    host: str = "127.0.0.1"
    port: int = 8080
    env: str = "dev"

    @classmethod
    def load(cls, project_root: Optional[Path] = None, env_file: Optional[str] = None) -> "Config":
        root = project_root or Path(__file__).resolve().parents[2]
        dotenv = _load_dotenv(root / (env_file or ".env"))

        def get(key: str, default: str = "") -> str:
            return os.environ.get(key) or dotenv.get(key, default)

        cfg = cls(
            db_path=get("TRADEDESK_DB_PATH", "data/tradedesk.db"),
            llm_provider=get("TRADEDESK_LLM_PROVIDER", "minimax"),
            llm_model=get("TRADEDESK_LLM_MODEL", "MiniMax-Text-01"),
            llm_api_key=get("MINIMAX_API_KEY", ""),
            llm_base_url=get("MINIMAX_BASE_URL", ""),
            llm_timeout_s=int(get("TRADEDESK_LLM_TIMEOUT_S", "30")),
            embedding_model=get("TRADEDESK_EMBEDDING_MODEL", "BGE-M3"),
            reranker_model=get("TRADEDESK_RERANKER_MODEL", "bge-reranker-v2-m3"),
            kb_version=get("TRADEDESK_KB_VERSION", "kb-2026.11-b"),
            list_version=get("TRADEDESK_LIST_VERSION", "sanctions-2026.11-a"),
            low_confidence_threshold=float(get("TRADEDESK_LOW_CONFIDENCE_THRESHOLD", "0.55")),
            amount_threshold_usd=float(get("TRADEDESK_AMOUNT_THRESHOLD_USD", "5000")),
            gate_token_ttl_s=int(get("TRADEDESK_GATE_TOKEN_TTL_S", "900")),
            max_concurrency=int(get("TRADEDESK_MAX_CONCURRENCY", "4")),
            host=get("TRADEDESK_HOST", "127.0.0.1"),
            port=int(get("TRADEDESK_PORT", "8080")),
            env=get("TRADEDESK_ENV", "dev"),
        )
        return cfg


_settings: Optional[Config] = None


def get_settings() -> Config:
    global _settings
    if _settings is None:
        _settings = Config.load()
    return _settings


def reset_settings() -> None:
    """测试用：清空缓存的配置。"""
    global _settings
    _settings = None