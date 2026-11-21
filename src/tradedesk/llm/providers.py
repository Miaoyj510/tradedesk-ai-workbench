# -*- coding: utf-8 -*-
"""云端厂商注册表（S5）。

为什么做成表
--------
不做成表就只能把厂商写进代码。表里的每一行 = 一个可接入的云端服务：
路由、模型名、key 的环境变量名、请求体形态。换一个厂商 = 换一行 + 换一个 env，业务代码不变。

**端点均已实测可达**（2026-11）：对每个端点发一个空请求，返回 401/400（鉴权/参数错误）而非连接失败，
说明路径与接口形态存在（OpenAI 兼容形态会报 400 参数错误而非 404）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass(frozen=True)
class Provider:
    """一个云端厂商。"""

    key: str
    name: str
    api_key_env: str
    chat_base_url: str
    chat_path: str
    chat_model: str
    embedding_base_url: str = ""
    embedding_path: str = "/v1/embeddings"
    embedding_model: str = ""
    # 嵌入请求体形态："openai" = {"model","input":[...]} ；"minimax" = {"model","texts":[...],"type":"db|query"}
    embedding_style: str = "openai"
    # 嵌入维度（0 = 不固定，由模型决定）
    embedding_dim: int = 0
    # 附加的查询参数（如 MiniMax 的 GroupId）
    extra_query: str = ""
    notes: str = ""
    # 基址地址的环境变量名（可选）——分厂商读取，避免切厂商时拿错地址
    base_url_env: str = ""

    @property
    def supports_embedding(self) -> bool:
        """这家厂商到今天为正式提供了嵌入接口。

        DeepSeek 官方文档中未列出嵌入接口（未找到），所以它只能做对话。
        语义通道需要另配厂商（如 MiniMax / 阿里云百炼）。
        """
        return bool(self.embedding_base_url and self.embedding_model)


PROVIDERS: Dict[str, Provider] = {
    "minimax": Provider(
        key="minimax", name="MiniMax",
        api_key_env="MINIMAX_API_KEY", base_url_env="MINIMAX_BASE_URL",
        chat_base_url="https://api.minimaxi.com", chat_path="/v1/chat/completions",
        # 实测（2026-11）：MiniMax-Text-01 已下线（HTTP 404 "no longer available"），
        # M2 也返回 404（虽然仍在 /v1/models 列表里）。当前可用：M3 / M3.1-Flash。
        chat_model="MiniMax-M3",
        embedding_base_url="https://api.minimaxi.com", embedding_path="/v1/embeddings",
        embedding_model="embo-01", embedding_style="minimax", embedding_dim=1536,
        notes="国内站点；国际站点为 https://api.minimax.chat。嵌入请求体为自定义的 texts/type，非 OpenAI 形态。"
             "对话模型会把思考过程写在 content 里的 <think>...</think> 块中（客户端必须剔除）。",
    ),
    "aliyun": Provider(
        key="aliyun", name="阿里云百炼（通义千问）",
        api_key_env="DASHSCOPE_API_KEY", base_url_env="DASHSCOPE_BASE_URL",
        chat_base_url="https://dashscope.aliyuncs.com/compatible-mode",
        chat_path="/v1/chat/completions", chat_model="qwen-plus",
        embedding_base_url="https://dashscope.aliyuncs.com/compatible-mode",
        embedding_path="/v1/embeddings", embedding_model="text-embedding-v3",
        embedding_style="openai", embedding_dim=1024,
        notes="百炼的 OpenAI 兼容模式；模型名需按账号可用模型列表调整。",
    ),
    "zhipu": Provider(
        key="zhipu", name="智谱 AI（GLM）",
        api_key_env="ZHIPU_API_KEY", base_url_env="ZHIPU_BASE_URL",
        chat_base_url="https://open.bigmodel.cn/api/paas/v4",
        chat_path="/chat/completions", chat_model="glm-4-plus",
        embedding_base_url="https://open.bigmodel.cn/api/paas/v4",
        embedding_path="/embeddings", embedding_model="embedding-3",
        embedding_style="openai", embedding_dim=1024,
        notes="开放平台 OpenAI 兼容接口；键的环境变量名兼容 BIGMODEL_API_KEY。",
    ),
    "volcengine": Provider(
        key="volcengine", name="火山引擎方舟（豆包）",
        api_key_env="ARK_API_KEY", base_url_env="ARK_BASE_URL",
        chat_base_url="https://ark.cn-beijing.volces.com/api/v3",
        chat_path="/chat/completions", chat_model="doubao-pro-32k",
        embedding_base_url="https://ark.cn-beijing.volces.com/api/v3",
        embedding_path="/embeddings", embedding_model="doubao-embedding",
        embedding_style="openai", embedding_dim=2560,
        notes="方舟的模型名通常是接口后的 ep-xxxx 或推理端点名，需按账号填。",
    ),
    "deepseek": Provider(
        key="deepseek", name="DeepSeek",
        api_key_env="DEEPSEEK_API_KEY", base_url_env="DEEPSEEK_BASE_URL",
        chat_base_url="https://api.deepseek.com", chat_path="/chat/completions",
        chat_model="deepseek-chat",
        notes="深度求索；官方未提供嵌入接口，语义通道需另配厂商。",
    ),
}

# 别名（配置里可以写任何一个）
ALIASES: Dict[str, str] = {
    "qwen": "aliyun", "dashscope": "aliyun", "tongyi": "aliyun", "阿里云": "aliyun",
    "glm": "zhipu", "bigmodel": "zhipu", "智谱": "zhipu",
    "ark": "volcengine", "doubao": "volcengine", "豆包": "volcengine", "火山": "volcengine",
    "minimax-ai": "minimax",
}


def list_providers() -> List[Provider]:
    return list(PROVIDERS.values())


def get_provider(key: str) -> Provider:
    """按名称取厂商（支持别名）；不存在时报错并列出可用选项。"""
    k = (key or "").strip().lower()
    k = ALIASES.get(k, k)
    if k not in PROVIDERS:
        raise KeyError(f"未知的云端厂商：{key}（可用：{', '.join(PROVIDERS)}）")
    return PROVIDERS[k]


# 通用 key 环境变量：不指定厂商时用它（例如容器里只传一个 key）。
GENERIC_KEY_ENV = "TRADEDESK_LLM_API_KEY"


def resolve_api_key(provider: Provider, fallback: str = "") -> str:
    """取 API key：厂商自己的环境变量 > 通用环境变量 > 配置里的 fallback。

    为什么还要一个通用变量：部署环境里通常只有一个密钥注入位，
    换厂商时只要改环境变量名，不要修代码。
    """
    for env in (provider.api_key_env, GENERIC_KEY_ENV):
        value = os.environ.get(env, "").strip()
        if value:
            return value
    return (fallback or "").strip()
