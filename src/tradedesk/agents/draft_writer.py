# -*- coding: utf-8 -*-
"""模型出稿执行器（S5-1a）。

职责边界
--------
只做三件事：拼提示词 → 调云端模型 → **校验模型没越权**。
不碰价格决策、不碰合同、不碰发送（红线：报价人工签核、合同必须人工确认）。

越权判定（模型输出不合规 → 整段丢弃，退回模板草稿）
--------------------------------------------------
1. **写了数字**（去掉占位符后仍有数字）→ 越权：数值只能来自结构化字段。
2. **编造未知占位符** → 说明模型在自造事实槽位。
3. **漏掉必需占位符** → 回复会缺价格 / 缺交期，对外失真。
4. **命中注入特征** → 模型把买家的注入内容写进了回复。
5. **空输出 / 替换后仍有残留占位符** → 直接判废。

为什么"退回模板"而不是"让模型重试"
--------------------------------
重试会把成本和时延翻倍，而失败模式（编数字、漏槽位）不是重试能修的。
模板路径一直在（本来就是已过审的基线），退回它是最稳的降级。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from ..domain.enums import Intent, Language
from ..llm.chat import LLMError
from ..runtime.gates import INJECTION_PATTERNS
from .prompts import (
    ALL_PLACEHOLDERS, PLACEHOLDER_ASK, PLACEHOLDER_CERT, PLACEHOLDER_LEAD,
    PLACEHOLDER_MOQ, PLACEHOLDER_PORT, PLACEHOLDER_PRICE, PLACEHOLDER_SKU,
    PLACEHOLDER_TERMS, REQUIRED_PLACEHOLDERS, build_messages,
)

PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
DIGIT_RE = re.compile(r"\d")

# 模型正文的长度上下限（超出说明它在胡说或在贴提示词）
MIN_BODY_CHARS = 40
MAX_BODY_CHARS = 4000


class DraftRejected(RuntimeError):
    """模型输出不满足受控条件 —— 丢弃并退回模板。"""


@dataclass
class ModelDraft:
    """一次成功的模型出稿。"""

    body: str
    provider: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0

    def usage(self) -> Dict[str, int]:
        return {"prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.total_tokens,
                "calls": self.calls}


def _names(placeholders: Iterable[str]) -> set:
    return {p.strip("{}").strip() for p in placeholders}


def check_model_body(text: str,
                     allowed: Iterable[str] = ALL_PLACEHOLDERS,
                     required: Iterable[str] = REQUIRED_PLACEHOLDERS) -> List[str]:
    """校验模型输出；返回问题列表，空列表 = 通过。

    注意顺序：先把占位符**摘掉**再查数字 —— 否则占位符里没有数字但要保留判断。
    """
    problems: List[str] = []
    body = (text or "").strip()
    if not body:
        return ["模型返回为空"]
    if len(body) < MIN_BODY_CHARS:
        problems.append("模型输出过短（%d 字符），可能没有真正作答" % len(body))
    if len(body) > MAX_BODY_CHARS:
        problems.append("模型输出过长（%d 字符），可能夹带了提示词或推理" % len(body))
    found = set(PLACEHOLDER_RE.findall(body))
    stripped = PLACEHOLDER_RE.sub("", body)
    if DIGIT_RE.search(stripped):
        problems.append("模型自行写出了数字（越权：数值只能来自结构化字段）")
    allowed_names = _names(allowed)
    unknown = found - allowed_names
    if unknown:
        problems.append("模型编造了未知占位符：" + ", ".join(sorted(unknown)))
    missing = _names(required) - found
    if missing:
        problems.append("模型漏掉了必需占位符：" + ", ".join(sorted(missing)))
    for pat in INJECTION_PATTERNS:
        if pat.search(body):
            problems.append("模型输出命中注入特征")
            break
    return problems


def render_facts(body: str, facts: Dict[str, str]) -> str:
    """把占位符换成真实值。

    真实值**全部来自结构化字段**（价格/交期/MOQ 来自规格记录，证书句来自证书块），
    模型没有参与这一步 —— 这是"模型编不出价格"能成立的原因。
    """
    out = body or ""
    for key, value in facts.items():
        out = out.replace("{{" + key + "}}", "" if value is None else str(value))
    if "{{" in out:
        raise DraftRejected("占位符替换后仍有残留")
    return out.strip()


class ModelDraftWriter:
    """把"证据 + 买家消息"变成一段受控的回复草稿。"""

    def __init__(self, chat, temperature: float = 0.3, max_tokens: int = 900,
                 placeholders: Optional[Iterable[str]] = None) -> None:
        self.chat = chat
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.placeholders = tuple(placeholders) if placeholders is not None else ALL_PLACEHOLDERS

    def allowed_placeholders(self, with_cert: bool) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
        """决定这次给模型哪些槽位。

        有有效证书 → 证书槽位也**必须**出现（否则回复漏掉认证声明，
        而 PRD 8.5 的 GATE-05 依赖"草稿里有认证声明 + 引用"这条链路）；
        没有有效证书 → 干脆不给这个槽位，模型也就无从漏写。
        """
        allowed = list(self.placeholders)
        if with_cert and PLACEHOLDER_CERT not in allowed:
            allowed.append(PLACEHOLDER_CERT)
        if not with_cert and PLACEHOLDER_CERT in allowed:
            allowed.remove(PLACEHOLDER_CERT)
        required = [p for p in REQUIRED_PLACEHOLDERS if p in allowed]
        if with_cert and PLACEHOLDER_CERT in allowed:
            required.append(PLACEHOLDER_CERT)
        return tuple(allowed), tuple(required)

    def write(self, language: Language, buyer_message: str, company: str = "",
              country: str = "", intents: Optional[Iterable[Intent]] = None,
              with_cert: bool = False) -> ModelDraft:
        """出一稿。模型输出不合规 → 抛 DraftRejected（调用方退回模板）。"""
        allowed, required = self.allowed_placeholders(with_cert)
        messages = build_messages(language, buyer_message, company=company,
                                  country=country, intents=intents, placeholders=allowed)
        try:
            result = self.chat.complete(messages, temperature=self.temperature,
                                        max_tokens=self.max_tokens)
        except LLMError as exc:
            raise DraftRejected("模型调用失败：%s" % exc) from exc
        problems = check_model_body(result.text, allowed, required)
        if problems:
            raise DraftRejected("；".join(problems))
        return ModelDraft(
            body=result.text.strip(),
            provider=result.provider or getattr(self.chat, "provider", None) and
            getattr(getattr(self.chat, "provider", None), "key", "") or "",
            model=result.model or getattr(self.chat, "model", ""),
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            total_tokens=result.total_tokens,
            calls=getattr(self.chat, "calls", 0),
        )
