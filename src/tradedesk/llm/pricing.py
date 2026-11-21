# -*- coding: utf-8 -*-
"""云端模型计费口径（S5-1a）。

数据来源
--------
MiniMax 官方定价页 `https://platform.minimaxi.com/document/price`（**官方口径，不是第三方转述**），
取数时间 **2026-11-21**，单位 **元 / 百万 tokens**。

为什么单独放一个文件
--------------------
价格会变，折扣档位因账号而异。所以：
- 价格**只在这里**维护，且每条都必须带来源与取数日期；
- **拿不到价格就返回 None** —— 不猜、不估算（PRD 12.4 的"≤0.2 元/条"要有依据才判）；
- 档位不明时给"标准档"与"折扣档"两个数，让读的人知道区间而不是被一个精确假数字误导。

已核实的档位（MiniMax 官方页原文）
----------------------------------
- MiniMax-M3，输入 ≤512k tokens：**4.20 / 16.80 / 0.84**（输入 / 输出 / 缓存读取）；
  同一条目另标 **"永久五折"**：2.10 / 8.40 / 0.42。
  （>512k 输入 tokens 的档位是 8.40 / 33.60 / 1.68，五折 4.20 / 16.80 / 0.84 —— 本项目提示词远小于 512k。）
  `service_tier=priority` 是标准档的 1.5 倍。
- MiniMax-M3.1-Flash：1.00 / 4.00 / 0.10。
- **嵌入模型 embo-01 的单价未在官方定价页找到** → 不估算（待验证项 S5-V4）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

SOURCE_URL = "https://platform.minimaxi.com/document/price"
AS_OF = "2026-11-21"


@dataclass(frozen=True)
class ModelPrice:
    """一个模型档位的单价（元 / 百万 tokens）。"""

    model: str
    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float = 0.0
    tier: str = ""
    note: str = ""

    def cost(self, prompt_tokens: int = 0, completion_tokens: int = 0,
             cached_tokens: int = 0) -> float:
        """按 token 数算钱（元）。"""
        billable_input = max(0, prompt_tokens - cached_tokens)
        return (billable_input / 1_000_000.0 * self.input_per_mtok
                + completion_tokens / 1_000_000.0 * self.output_per_mtok
                + cached_tokens / 1_000_000.0 * self.cache_read_per_mtok)


# 已核实的档位（MiniMax 官方页，2026-11-21 取数）
PRICES: Tuple[ModelPrice, ...] = (
    ModelPrice("MiniMax-M3", 4.20, 16.80, 0.84, tier="标准档",
               note="输入 ≤ 512k tokens"),
    ModelPrice("MiniMax-M3", 2.10, 8.40, 0.42, tier="永久五折档",
               note="输入 ≤ 512k tokens；是否适用于本账号未确认"),
    ModelPrice("MiniMax-M3", 8.40, 33.60, 1.68, tier="标准档",
               note="输入 > 512k tokens"),
    ModelPrice("MiniMax-M3.1-Flash", 1.00, 4.00, 0.10, tier="标准档",
               note="官方页未标注分档"),
)


def find_prices(model: str) -> List[ModelPrice]:
    """取某个模型的全部档位（可能有多档：标准 / 五折 / 长上下文）。"""
    name = (model or "").strip()
    return [p for p in PRICES if p.model == name]


def estimate_cost(model: str, prompt_tokens: int = 0, completion_tokens: int = 0,
                  cached_tokens: int = 0) -> Optional[Tuple[float, str]]:
    """估算一次调用的成本。

    返回 (金额元, 档位说明)；**模型不在表里就返回 None**（不猜、不估算）。
    多档位时取**标准档**（最保守的一档，避免低估成本）。
    """
    tiers = [p for p in find_prices(model) if "永久五折" not in p.tier]
    if not tiers:
        return None
    p = min(tiers, key=lambda x: x.input_per_mtok * x.output_per_mtok)
    return (p.cost(prompt_tokens, completion_tokens, cached_tokens),
            "%s（%s）" % (p.tier, p.note))


def estimate_cost_range(model: str, prompt_tokens: int = 0, completion_tokens: int = 0,
                         cached_tokens: int = 0) -> Optional[Tuple[float, float]]:
    """返回 (最低, 最高) 区间：有五折档时上下界不同，不给单一精确数。"""
    tiers = find_prices(model)
    if not tiers:
        return None
    costs = [p.cost(prompt_tokens, completion_tokens, cached_tokens) for p in tiers]
    return (min(costs), max(costs))
