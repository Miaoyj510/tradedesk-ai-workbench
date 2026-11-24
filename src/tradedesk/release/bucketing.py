# -*- coding: utf-8 -*-
"""企业分桶 —— 灰度"按企业"分流（PRD 13.4.8）。

为什么必须**按企业**而不是按请求
------------------------------
同一个买家会在不同时间发多条询盘、还可能同时落在两个版本上：
按请求分流 → 同一个买家前半句看到 A 版价格、后半句看到 B 版价格，
这在报价场景里是事故。分桶的输入是**公司名**，所以同一企业的所有询盘
（哪怕来自不同业务员、不同线程）永远命中同一版本。

为什么用**哈希分桶**而不是随机
------------------------------
随机分桶每次重启都会重新洗牌：昨天在灰度的企业今天可能落到正式版，
出问题也定位不到"这批企业"。哈希分桶是**纯函数**——同样的公司名永远得到
同样的桶号，不需要存任何分流表（存表就意味着要维护、要在多实例间同步）。

三条规则
--------
1. 公司名先规范化（NFKC + 大小写折叠 + 空白折叠）：
   "ACME  Trading " / "Acme Trading" / "acme trading" 必须是同一家公司。
2. 语种灰度：发布若限定了语种，**只有该语种的询盘**进灰度；
   语种未知（空）按保守处理 —— 不进灰度。
3. 拿不到公司名（空）→ 不进灰度，走稳定版：宁可少灰度，不可错发版本。
"""
from __future__ import annotations

import hashlib
import unicodedata

from .manifest import STAGE_PERCENT, ReleaseManifest


def normalize_company(name: str) -> str:
    """公司名规范化：NFKC → casefold → 空白折叠。

    NFKC 用来把全角/半角、兼容字符统一（"ＡＣＭＥ" 与 "ACME" 是同一家）；
    casefold 比 lower 更彻底（对德语 ß 等更符合"同一公司"的直觉）；
    空白折叠让多余空格不影响分桶。
    """
    if not name:
        return ""
    text = unicodedata.normalize("NFKC", str(name))
    return " ".join(text.split()).casefold()


def enterprise_bucket(company: str) -> int:
    """企业桶号：0—99 的确定性整数（纯函数，无状态、无外部依赖）。

    取 sha256 前 8 个十六进制字符（32 bit）取模 100 —— 分布均匀，
    且对常见的企业名后缀变化（Co., Ltd、Limited、, Inc.）不敏感
    （规范化只做 NFKC/大小写/空白，不做后缀剥离：那属于业务口径，不该由分桶猜）。
    """
    norm = normalize_company(company)
    digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 100


def in_canary(manifest: ReleaseManifest, company: str, lang: str = "") -> bool:
    """这家企业、这个语种，现在该不该走灰度版本。"""
    if manifest is None or manifest.status != "canary":
        return False
    norm = normalize_company(company)
    if not norm:
        # 拿不到公司名 → 不进灰度（保守：宁可稳定版，也不给错版本）
        return False
    if manifest.langs and (not lang or lang not in manifest.langs):
        # 限定语种的发布：语种不符或语种未知 → 不进灰度
        return False
    if manifest.stage == "PILOT":
        whitelist = {normalize_company(c) for c in manifest.pilot_companies}
        return norm in whitelist
    percent = STAGE_PERCENT.get(manifest.stage, 0)
    if percent <= 0:
        return False
    return enterprise_bucket(company) < percent