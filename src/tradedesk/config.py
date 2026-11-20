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


# 知识库版本号的**唯一真源**在 data/kb_seed.py —— 以前这里另写了一份字面量，
# 结果知识库内容一改，配置还指着旧版本：检索层读的是旧块，审核断言读的是新块，
# 同一份"引用回指"检查就变成"引用全都对不上"。版本号只能有一处。
from .data.kb_seed import KB_VERSION


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
    # PostgreSQL DSN；为空 = 用 SQLite（本地零依赖）。生产形态填这个。
    database_url: str = ""
    # 队列 / 缓存（S2 起使用）
    redis_url: str = ""
    # 大模型（云 API Key 方式；S1 阶段不调用，仅登记配置）
    llm_provider: str = "minimax"
    llm_model: str = "MiniMax-Text-01"
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_timeout_s: int = 30
    # 检索
    # 体前实际使用的嵌入模型。PRD 原定 BGE-M3（本地部署），但本机无可用 GPU
    # （Iris Xe 共享显存 128MB）、16GB 内存，且 Docker Hub 拉不到模型镜像 —— 因此 S4 走**云端嵌入 API**
    # （PRD 13.4.5 选型轴③允许替换嵌入实现，接口不变）。评测报告里必须写清模型口径。
    embedding_model: str = "embo-01"
    # embo-01 的真实输出维度是 **1536**（实测云端接口得到，不是文档里的 1024）。
    # 维度错会在嵌入客户端直接报错，也会让 pgvector 列拒绝写入 —— 两道关都能拦下来。
    embedding_dim: int = 1536
    reranker_model: str = "bge-reranker-v2-m3"
    embedder_provider: str = "minimax"
    embedder_base_url: str = "https://api.minimaxi.com"
    embedder_batch_size: int = 16
    # 默认关闭：启动时嵌入会调云端 API（计费），测试与本地运行必须开启才会发起请求。
    embedder_enabled: bool = False
    kb_version: str = KB_VERSION
    list_version: str = "sanctions-2026.11-a"
    # 业务阈值
    # SLA 按业务类型可配置（FR-ORC-08：≥3 类）
    sla_first_response_s: int = 300      # 首响
    sla_quote_s: int = 3600             # 报价
    sla_followup_s: int = 86400          # 跟进
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
            database_url=get("TRADEDESK_DATABASE_URL", ""),
            redis_url=get("TRADEDESK_REDIS_URL", ""),
            llm_provider=get("TRADEDESK_LLM_PROVIDER", "minimax"),
            llm_model=get("TRADEDESK_LLM_MODEL", "MiniMax-Text-01"),
            llm_api_key=get("MINIMAX_API_KEY", ""),
            llm_base_url=get("MINIMAX_BASE_URL", ""),
            llm_timeout_s=int(get("TRADEDESK_LLM_TIMEOUT_S", "30")),
            embedding_model=get("TRADEDESK_EMBEDDING_MODEL", "embo-01"),
            embedding_dim=int(get("TRADEDESK_EMBEDDING_DIM", "1536")),
            reranker_model=get("TRADEDESK_RERANKER_MODEL", "bge-reranker-v2-m3"),
            embedder_provider=get("TRADEDESK_EMBEDDER_PROVIDER", "minimax"),
            embedder_base_url=get("MINIMAX_BASE_URL", "") or "https://api.minimaxi.com",
            embedder_batch_size=int(get("TRADEDESK_EMBEDDER_BATCH_SIZE", "16")),
            embedder_enabled=get("TRADEDESK_EMBEDDER_ENABLED", "0") in ("1", "true", "True", "yes"),
            kb_version=get("TRADEDESK_KB_VERSION", KB_VERSION),
            list_version=get("TRADEDESK_LIST_VERSION", "sanctions-2026.11-a"),
            sla_first_response_s=int(get("TRADEDESK_SLA_FIRST_RESPONSE_S", "300")),
            sla_quote_s=int(get("TRADEDESK_SLA_QUOTE_S", "3600")),
            sla_followup_s=int(get("TRADEDESK_SLA_FOLLOWUP_S", "86400")),
            low_confidence_threshold=float(get("TRADEDESK_LOW_CONFIDENCE_THRESHOLD", "0.55")),
            amount_threshold_usd=float(get("TRADEDESK_AMOUNT_THRESHOLD_USD", "5000")),
            gate_token_ttl_s=int(get("TRADEDESK_GATE_TOKEN_TTL_S", "900")),
            max_concurrency=int(get("TRADEDESK_MAX_CONCURRENCY", "4")),
            host=get("TRADEDESK_HOST", "127.0.0.1"),
            port=int(get("TRADEDESK_PORT", "8080")),
            env=get("TRADEDESK_ENV", "dev"),
        )
        # SQLite 路径按**项目根**解析成绝对路径，而不是相对当前工作目录。
        # 两个 agent 各有各的 worktree，相对路径会让两个进程共用同一个库文件
        # —— 跟"两库隔离"直接冲突（WORKFLOW.md 第 4 节）。
        if cfg.db_path and not os.path.isabs(cfg.db_path):
            cfg.db_path = str((root / cfg.db_path).resolve())
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