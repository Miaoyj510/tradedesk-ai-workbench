# -*- coding: utf-8 -*-
"""多语种分词与归一。

支持 zh / en / es / ar：
- 拉丁系（含西语）：按非字母数字切分
- 阿拉伯语：先去变音符号，再统一 alef / taa marbuta / yaa 变体，最后按非字母切分
- 中文：无词典分词，用**字符二元组**（bigram）近似，避免引入分词依赖
"""
from __future__ import annotations

import re
import unicodedata
from typing import List

_ARABIC_DIACRITICS = re.compile(r"[\u064B-\u0652\u0670]")
_ARABIC_NORMALIZE = {
    "\u0623": "\u0627", "\u0625": "\u0627", "\u0622": "\u0627",   # أ إ آ → ا
    "\u0629": "\u0647",                                              # ة → ه
    "\u0649": "\u064A",                                              # ى → ي
    "\u0624": "\u0648", "\u0626": "\u064A",                          # ؤ ئ → و ي
}
_LATIN_TOKEN = re.compile(r"[\w\u00C0-\u024F]+", re.UNICODE)
_ARABIC_TOKEN = re.compile(r"[\u0600-\u06FF]+", re.UNICODE)
_CJK = re.compile(r"[\u4E00-\u9FFF]+", re.UNICODE)


def normalize_text(text: str) -> str:
    """归一：NFKC + 小写 + 去阿拉伯变音/变体。"""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text).lower()
    text = _ARABIC_DIACRITICS.sub("", text)
    for src, dst in _ARABIC_NORMALIZE.items():
        text = text.replace(src, dst)
    return text


def tokenize(text: str) -> List[str]:
    """多语种切词（归一后的结果）。"""
    text = normalize_text(text)
    tokens: List[str] = []
    # 中文：字符二元组 + 单字
    for run in _CJK.findall(text):
        chars = list(run)
        tokens.extend(chars)
        tokens.extend(chars[i] + chars[i + 1] for i in range(len(chars) - 1))
    # 阿拉伯语
    for run in _ARABIC_TOKEN.findall(text):
        tokens.append(run)
        for i in range(len(run) - 1):
            tokens.append(run[i] + run[i + 1])
    # 拉丁系
    for run in _LATIN_TOKEN.findall(text):
        if run not in tokens:
            tokens.append(run)
    return [t for t in tokens if t]


def char_ngrams(text: str, n: int = 3) -> List[str]:
    """字符 n-gram：给型号/编号这类专名兜底（向量与词法都容易漏）。"""
    text = normalize_text(text).replace(" ", "")
    return [text[i:i + n] for i in range(max(0, len(text) - n + 1))]
