# -*- coding: utf-8 -*-
"""云端模型接入层（S5）。

为什么不把厂商写死
----------------
本项目目前用 MiniMax，但业务上单一厂商依赖不可接受（调用成本、数据出境、服务可用性、模型能力）。
所以接入层只认两件事：**请求/响应的形态**与**身份与路由**，具体厂商只是表里的一行。
换厂商 = 换一行表 + 换一个环境变量，不动业务代码。
"""
from .providers import (  # noqa: F401
    PROVIDERS, Provider, get_provider, list_providers, resolve_api_key,
)
from .chat import ChatClient, ChatResult, LLMError  # noqa: F401
