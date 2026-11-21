# -*- coding: utf-8 -*-
"""从配置装配云端客户端（S5）。"""
from __future__ import annotations

from typing import Optional

from ..config import Config
from .chat import ChatClient
from .providers import Provider, get_provider, resolve_api_key


def build_chat_client(config: Config) -> Optional[ChatClient]:
    """按配置建对话客户端；没配厂商或没 key 返回 None。"""
    try:
        provider = get_provider(config.llm_provider)
    except KeyError:
        return None
    key = resolve_api_key(provider, config.llm_api_key)
    if not key:
        return None
    # 基址地址优先级：通用覆盖 > 厂商专用 > 厂商表默认
    base_url = (config.llm_base_url
                or getattr(config, "provider_base_urls", {}).get(provider.key, "")
                or provider.chat_base_url)
    return ChatClient(provider=provider, api_key=key,
                     base_url=base_url, model=config.llm_model,
                     timeout_s=config.llm_timeout_s)


def build_embedder(config: Config):
    """按配置建嵌入器；没配厂商、厂商没嵌入接口、或没 key 就返回 None。

    维度口径：`TRADEDESK_EMBEDDER_DIM` > 厂商表里的标称值 > 旧配置 `TRADEDESK_EMBEDDING_DIM`。
    """
    from ..retrieval.embedders import OpenAICompatibleEmbedder, RemoteEmbedder
    try:
        provider = get_provider(config.embedder_provider or config.llm_provider)
    except KeyError:
        return None
    if not provider.supports_embedding:
        return None            # 例：DeepSeek 官方没有嵌入接口，语义通道得另配厂商
    key = resolve_api_key(provider, config.llm_api_key)
    if not key:
        return None
    dim = config.embedder_dim or provider.embedding_dim
    if provider.embedding_style == "minimax":
        return RemoteEmbedder(base_url=config.embedder_base_url or provider.embedding_base_url,
                              api_key=key,
                              model=config.embedder_model or provider.embedding_model or config.embedding_model,
                              dim=dim or config.embedding_dim,
                              timeout_s=config.llm_timeout_s, batch_size=config.embedder_batch_size,
                              provider="minimax")
    return OpenAICompatibleEmbedder(provider=provider, api_key=key,
                                    base_url=config.embedder_base_url or provider.embedding_base_url,
                                    model=config.embedder_model or provider.embedding_model,
                                    dim=dim or 1024,
                                    timeout_s=config.llm_timeout_s,
                                    batch_size=config.embedder_batch_size)
