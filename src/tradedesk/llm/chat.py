# -*- coding: utf-8 -*-
"""对话完成客户端（OpenAI 兼容形态，S5）。

形态统一的原因：本项目接的厂商都提供 OpenAI 兼容的 `/chat/completions`，
所以客户端只有一套，厂商差异在路径、模型名、key 的环境变量上。

两个不同的错误形态（不能混为一谈）：
- **业务错误**：MiniMax 会在 HTTP 200 的 body 里返回 `base_resp.status_code != 0`（鉴权失败、模型名错等）—— 不重试。
- **网络错误**：超时、连接重置等 —— 可重试。
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

Transport = Callable[[str, dict, dict, int], dict]


class LLMError(RuntimeError):
    """云端模型调用失败（业务错误不重试）。"""


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def strip_reasoning(text: str) -> str:
    """剔除模型的思考过程（<think>...</think>）。

    为什么必须剔：MiniMax-M3 把思考内容直接写在 `message.content` 里。
    本项目的产出是**要发给买家的回复**，思考过程混进去就是对外发送内部推理，
    也可能泄露内部价格思路。所以剔除必须发生在**发出去之前**。
    """
    if not text:
        return ""
    out = _THINK_RE.sub("", text).strip()
    # 未闭合的思考块（模型被截断时会出现）也要剔掉
    idx = out.lower().find("<think>")
    if idx >= 0:
        out = out[:idx].strip()
    return out


@dataclass
class ChatResult:
    """一次完成的结果 + 用量（计费口径的基础）。"""

    text: str
    model: str = ""
    provider: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)

    def usage(self) -> Dict[str, int]:
        return {"prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.total_tokens}


def _http_transport(url: str, headers: dict, payload: dict, timeout: int) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_error(exc: "urllib.error.HTTPError") -> LLMError:
    """HTTP 4xx/5xx = **业务错误**（鉴权失败 / 参数错 / 模型名错），不重试。

    `urllib.error.HTTPError` 是 `URLError` 的子类。若不单独拦住，401 会被当成网络错误重试三次，
    每次都计费，而且报错会指向错误的方向。
    """
    try:
        body = exc.read().decode("utf-8", "replace")[:300]
    except Exception:                      # pragma: no cover
        body = ""
    return LLMError(f"模型 API 返回 HTTP {exc.code}：{body}")


class ChatClient:
    """对话完成客户端。"""

    def __init__(self, provider, api_key: str, base_url: str = "", model: str = "",
                 timeout_s: int = 30, retries: int = 2, temperature: float = 0.3,
                 max_tokens: int = 1024, transport: Optional[Transport] = None) -> None:
        self.provider = provider
        self.api_key = api_key
        self.base_url = (base_url or provider.chat_base_url).rstrip("/")
        self.model = model or provider.chat_model
        self.timeout_s = timeout_s
        self.retries = max(0, retries)
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._transport = transport or _http_transport
        self.calls = 0            # 实际 HTTP 调用次数（计费口径，重试也算）

    # ---- 请求组装 ----
    def _url(self) -> str:
        url = f"{self.base_url}{self.provider.chat_path}"
        if self.provider.extra_query:
            url = f"{url}?{self.provider.extra_query}"
        return url

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"}

    def _payload(self, messages: List[Dict[str, str]], **kw) -> dict:
        return {
            "model": kw.get("model", self.model),
            "messages": messages,
            "temperature": kw.get("temperature", self.temperature),
            "max_tokens": kw.get("max_tokens", self.max_tokens),
        }

    # ---- 响应解析 ----
    @staticmethod
    def _parse(resp: dict) -> ChatResult:
        if not isinstance(resp, dict):
            raise LLMError(f"模型返回不是 JSON 对象：{str(resp)[:120]}")
        base = resp.get("base_resp") or {}
        code = base.get("status_code")
        if code not in (None, 0):
            raise LLMError(f"模型报错：{code} {base.get('status_msg', '')}")
        # OpenAI 兼容风格：错误可能在 HTTP 200 的 body 里（{"error": {...}}）
        err = resp.get("error")
        if isinstance(err, dict) and err.get("message"):
            raise LLMError(f"模型报错：{err.get('code', '')} {err.get('message', '')}".strip())
        choices = resp.get("choices") or []
        if not choices:
            raise LLMError(f"模型没有返回 choices：{str(resp)[:160]}")
        msg = choices[0].get("message") or {}
        text = msg.get("content")
        if text is None:
            text = choices[0].get("text") or ""
        # 思考块剔除（见 strip_reasoning）——返回给上层的必须是给买家看的内容
        text = strip_reasoning(str(text))
        usage = resp.get("usage") or {}
        return ChatResult(
            text=str(text), model=str(resp.get("model", "")),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            total_tokens=int(usage.get("total_tokens") or 0),
            raw=resp,
        )

    # ---- 对外 ----
    def complete(self, messages: List[Dict[str, str]], **kw) -> ChatResult:
        """发一轮对话，返回文本 + usage。"""
        if not messages:
            raise LLMError("messages 不能为空")
        url, headers, payload = self._url(), self._headers(), self._payload(messages, **kw)
        last: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                self.calls += 1
                resp = self._transport(url, headers, payload, self.timeout_s)
                result = self._parse(resp)
                result.provider = self.provider.key
                return result
            except LLMError:
                raise                       # 业务错误（鉴权/模型名/参数）不重试
            except urllib.error.HTTPError as exc:
                raise _http_error(exc)       # HTTP 4xx/5xx = 业务错误，不重试
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = exc
                if attempt < self.retries:
                    time.sleep(0.4 * (attempt + 1))
        raise LLMError(f"模型调用失败（已重试 {self.retries} 次）：{last}")
