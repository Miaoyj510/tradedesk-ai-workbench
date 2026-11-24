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
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..domain.enums import Intent, Language
from ..llm.chat import LLMError
from ..runtime.gates import INJECTION_PATTERNS
from .prompts import (
    ALL_PLACEHOLDERS, OPTIONAL_PLACEHOLDERS, PLACEHOLDER_ASK, PLACEHOLDER_CERT,
    PLACEHOLDER_LEAD, PLACEHOLDER_MOQ, PLACEHOLDER_PORT, PLACEHOLDER_PRICE,
    PLACEHOLDER_SKU, PLACEHOLDER_TERMS, REQUIRED_PLACEHOLDERS, build_messages,
)

PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
# ---- 词形数字（R19-03）——“拿不到真值”不等于“编不出价格” ----
# 只扫"阿拉伯数字"的判定器，模型把价格写成英文单词（"four dollars fifty"）就能绕过。
WORD_NUMBER_EN = (
    r"(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|"
    r"thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|"
    r"million|billion|dozen)")
WORD_NUMBER_CN = r"(?:\u4e00|\u4e8c|\u4e24|\u4e09|\u56db|\u4e94|\u516d|\u4e03|\u516b|\u4e5d|\u5341|\u767e|\u5343|\u4e07)"
WORD_NUMBER_AR = (
    r"(?:\u0648\u0627\u062d\u062f|\u0627\u062b\u0646\u0627\u0646|\u062b\u0644\u0627\u062b\u0629|\u0623\u0631\u0628\u0639\u0629|\u062e\u0645\u0633\u0629|"
    r"\u0633\u062a\u0629|\u0633\u0628\u0639\u0629|\u062b\u0645\u0627\u0646\u064a\u0629|\u0639\u0634\u0631\u0629|\u0639\u0634\u0631\u064a\u0646|\u0645\u0627\u0626\u0629|\u0623\u0644\u0641)")
# 货币词与数量单位（“数字 + 它”才算数值表达）
MONEY_UNIT = (
    r"(?:dollars?|euros?|usd|eur|rmb|cny|yuan|rupees?|rials?|"
    r"\u5143|\u7f8e\u5143|\u6b27\u5143|\u0641\u064a\u0627\u0644|\u062f\u0648\u0644\u0627\u0631|"
    r"pieces?|units?|pcs?|per\s+(?:piece|unit|pc|pcs)|\u53f0|\u4e2a|\u5757)")
CURRENCY_DIGIT_RE = re.compile(
    r"(?:usd|eur|rmb|cny|\$|\u20ac|\uffe5|\u5143|\u0641\u064a\u0627\u0644|\u062f\u0648\u0644\u0627\u0631)\s*[\d.,]+", re.I)
CURRENCY_DIGIT_SUFFIX_RE = re.compile(
    r"[\d.,]+\s*(?:\u5143|\u7f8e\u5143|\u6b27\u5143|\u0641\u064a\u0627\u0644|\u062f\u0648\u0644\u0627\u0631|dollars?|euros?)", re.I)
WORD_MONEY_RE = re.compile(
    r"(?:" + WORD_NUMBER_EN + r"|" + WORD_NUMBER_CN + r"|" + WORD_NUMBER_AR + r")"
    r"(?:\s+(?:" + WORD_NUMBER_EN + r"))?\s*(?:" + MONEY_UNIT + r")", re.I)


def numeric_fabrics(body: str) -> List[str]:
    """发现“隐形数值”：模型自己写的价格/数量表达。

    这里只拥有两种形态：
      ① **币种 + 数字**（USD 12.50 / 12.50 元）；
      ② **词形数字 + 货币/单位**（four dollars / twenty five per piece）。
    普通的“数字 + 单位”（如 5000 units）不在这里拒：那类数字由「数字白名单」那一层去处理（让它们可以是买家原话里的）。
    """
    out: List[str] = []
    m = CURRENCY_DIGIT_RE.search(body or "")
    if m:
        out.append("模型写出了币种数字（%s）" % m.group(0).strip())
    m = CURRENCY_DIGIT_SUFFIX_RE.search(body or "")
    if m:
        out.append("模型写出了币种数字（%s）" % m.group(0).strip())
    m = WORD_MONEY_RE.search(body or "")
    if m:
        out.append("模型写出了词形数字（%s）" % m.group(0).strip())
    return out
# 币种前缀：这些位置的数字是**报价**，必须来自知识库真值
CURRENCY_NUM_RE = re.compile(r"(?:USD|EUR|\$|\u20ac|\uffe5)\s*(\d+(?:\.\d+)?)", re.I)


def numbers_of(text: str) -> set:
    """文本里的数字集合（浮点）。"""
    out = set()
    for m in NUMBER_RE.findall(text or ""):
        try:
            out.add(float(m))
        except ValueError:
            pass
    return out

# 模型正文的长度上下限（超出说明它在胡说或在贴提示词）
MIN_BODY_CHARS = 40
MAX_BODY_CHARS = 4000
# 必填事实槽位（不包含证书句与追问句）：它们为空就不能出稿（R19-06）
REQUIRED_FACT_KEYS = ("sku", "moq", "price_range", "terms", "port", "lead_time")


class DraftRejected(RuntimeError):
    """模型输出不满足受控条件 —— 丢弃并退回模板。

    带 `calls`：被拒绝前发过的 HTTP 调用可能已产生费用。
    带 `usage`：**越权被拒时响应里是有 usage 的**（模型已经返回了正文，否则没有正文可判），
    所以那一次调用**是花了钱的**，token 必须照记（R19-05）。
    """

    def __init__(self, message: str, calls: int = 0,
                 usage: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.calls = calls
        self.usage = usage or {}


@dataclass
class ModelDraft:
    """一次成功的模型出稿。"""

    body: str
    provider: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0
    # 成本（元）：单价未核实时为 None（不猜、不估算）
    cost_cny: Optional[float] = None
    price_tier: str = ""

    def usage(self) -> Dict[str, Any]:
        return {"provider": self.provider, "model": self.model,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "cached_tokens": self.cached_tokens,
                "total_tokens": self.total_tokens,
                "calls": self.calls,
                "cost_cny": self.cost_cny,
                "price_tier": self.price_tier,
                "degraded": False}


def _names(placeholders: Iterable[str]) -> set:
    return {p.strip("{}").strip() for p in placeholders}


def check_model_body(text: str,
                     allowed: Iterable[str] = ALL_PLACEHOLDERS,
                     required: Iterable[str] = REQUIRED_PLACEHOLDERS,
                     buyer_numbers: Optional[Iterable[float]] = None,
                     truth_numbers: Optional[Iterable[float]] = None) -> List[str]:
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
    truth = {float(x) for x in (truth_numbers or set())}
    buyer = {float(x) for x in (buyer_numbers or set())}
    # 数字白名单**分两层**（S5 修订，真实评测驱动）：
    #   ① 币种位置（USD 12.50）的数字 → 必须在**知识库真值**里（否则拿买家的数量冒充价格）。
    #   ② 其他位置的数字 → 可以是**买家原话里的数字**（模型复述买家数量不算编造）。
    # 旧逻辑将任何数字都当越权，结果是模型只要复述买家数量（5000 件）就被丢弃整段
    # —— 真实测试里 ar/es 的拒绝率被这一条抬高到 70%/20%（事实上是误判）。
    for m in CURRENCY_NUM_RE.finditer(stripped):
        try:
            v = float(m.group(1))
        except ValueError:
            continue
        if v not in truth:
            problems.append("模型写出了价格数字 %s（不在知识库真值里）" % m.group(1))
            break
    else:
        for m in NUMBER_RE.finditer(stripped):
            try:
                v = float(m.group(0))
            except ValueError:
                continue
            if v in truth or v in buyer:
                continue
            problems.append("模型自行写出了数字（越权：数值只能来自结构化字段）")
            break
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
    # 隐形数值（R19-03）：写成英文单词的价格也要拒
    problems.extend(numeric_fabrics(body))
    return problems


def render_facts(body: str, facts: Dict[str, str], lang: Optional[Language] = None) -> str:
    """把占位符换成真实值，并在**可选事实缺失时由代码补齐**。

    真实值**全部来自结构化字段**（价格/交期/MOQ 来自规格记录，证书句来自证书块），
    模型没有参与这一步 —— 这是“模型编不出价格”能成立的原因。

    **为什么要补齐**（S5 修订）：可选占位符（MOQ / 条款 / 港 / 证书句 / 追问句）不应该由
    **模型**决定进不进去对外内容 —— 它漏写了，代码补上；它写了，代码就不重复。
    真实测试里 ar/es 语种的模型常常漏写这些占位符，旧逻辑把它们当必填 → 整段丢弃、走模板，
    ar 出稿成功率因此只有 30%。
    """
    # 必填事实空值 → 不出稿（R19-06）：价格为空时写出 "USD None–None"，
    # 而 GATE-01 的价格模式**要有数字**，所以这样的草稿会**绕过价格签核闸门**——缺事实就不发。
    missing = [k for k in REQUIRED_FACT_KEYS
               if k in facts and not str(facts.get(k) or "").strip()]
    if missing:
        raise DraftRejected("必填事实槽位为空：" + ", ".join(missing))
    out = body or ""
    used = set()
    for key, value in facts.items():
        token = "{{" + key + "}}"
        if token in out:
            used.add(key)
        out = out.replace(token, "" if value is None else str(value))
    if "{{" in out:
        raise DraftRejected("占位符替换后仍有残留")
    out = out.strip()
    if lang is not None:
        from .prompts import FACT_SENTENCES
        sentences = FACT_SENTENCES.get(lang, {})
        # used 里存的是**槽位名**（如 "moq"），而 OPTIONAL_PLACEHOLDERS 里是**占位符**（如 "{{moq}}"）
        # 两者形式不一致就会让补齐静默失效—— facts 的键也是槽位名。
        missing = [k for k in OPTIONAL_PLACEHOLDERS
                   if k.strip("{}") not in used
                   and facts.get(k.strip("{}"))
                   and k.strip("{}") in sentences]
        if missing:
            extra = " ".join(sentences[k.strip("{}")].format(**facts).strip() for k in missing)
            out = (out + "\n\n" + extra).strip()
    return out


class ModelDraftWriter:
    """把"证据 + 买家消息"变成一段受控的回复草稿。"""

    # max_tokens 默认 2048：MiniMax-M3 是思考模型，**思考 token 与正文共用预算**。
    # 900 时 ar 语种的推理模型因此返回空正文（实测：ar 平均只有 356 tokens）。
    def __init__(self, chat, temperature: float = 0.3, max_tokens: int = 2048,
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
        # 必填只有三个核心事实；证书句等可选项漏了由代码补齐（见 render_facts）。
        required = [p for p in REQUIRED_PLACEHOLDERS if p in allowed]
        return tuple(allowed), tuple(required)

    def write(self, language: Language, buyer_message: str, company: str = "",
              country: str = "", intents: Optional[Iterable[Intent]] = None,
              with_cert: bool = False, truth_numbers: Optional[Iterable[float]] = None) -> ModelDraft:
        """出一稿。模型输出不合规 → 抛 DraftRejected（调用方退回模板）。"""
        allowed, required = self.allowed_placeholders(with_cert)
        messages = build_messages(language, buyer_message, company=company,
                                  country=country, intents=intents, placeholders=allowed)
        # 本次调用数（R19-04）：ChatClient.calls 是**进程累计值**，直接拿它会让落库的调用次数按 N(N+1)/2 膨胀。
        before = int(getattr(self.chat, "calls", 0) or 0)
        try:
            result = self.chat.complete(messages, temperature=self.temperature,
                                        max_tokens=self.max_tokens)
        except LLMError as exc:
            after = int(getattr(self.chat, "calls", 0) or 0)
            raise DraftRejected("模型调用失败：%s" % exc,
                                calls=max(0, after - before)) from exc
        after = int(getattr(self.chat, "calls", 0) or 0)
        calls = max(0, after - before)
        usage: Dict[str, Any] = {
            "provider": result.provider or "",
            "model": result.model or getattr(self.chat, "model", ""),
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "cached_tokens": int((result.raw.get("usage") or {}).get("prompt_tokens_details", {})
                                 .get("cached_tokens", 0) or 0),
            "total_tokens": result.total_tokens,
            "calls": calls,
        }
        problems = check_model_body(result.text, allowed, required,
                                     buyer_numbers=numbers_of(buyer_message),
                                     truth_numbers=truth_numbers)
        if problems:
            # 越权被拒时**响应里有 usage**（R19-05）——这一次已经花了钱，token 必须照记。
            raise DraftRejected("；".join(problems), calls=calls, usage=usage)
        provider = result.provider or (getattr(getattr(self.chat, "provider", None), "key", "") or "")
        model = result.model or getattr(self.chat, "model", "")
        # 计价在这里做（而不是写入点）：成本是“这一次出稿的事”，与草稿一起归属。
        from ..llm.pricing import estimate_cost
        est = estimate_cost(model, result.prompt_tokens, result.completion_tokens)
        cost_cny, tier = est if est is not None else (None, "")
        return ModelDraft(
            body=result.text.strip(),
            provider=provider,
            model=model,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            cached_tokens=int((result.raw.get("usage") or {}).get("prompt_tokens_details", {})
                              .get("cached_tokens", 0) or 0),
            total_tokens=result.total_tokens,
            calls=calls,          # 本次的调用数，不是进程累计值（R19-04）
            cost_cny=cost_cny,
            price_tier=tier,
        )
