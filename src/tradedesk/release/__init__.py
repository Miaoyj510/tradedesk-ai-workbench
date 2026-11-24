# -*- coding: utf-8 -*-
"""发布与灰度子包（S6-3；PRD 13.4.8）。

四层能力里的"编排运行时"这一层的**发布侧**：
草稿要发出去，先得有一个"当前在发哪一版"的确定性答案 ——
本包负责把这个问题回答成一条可审计的链路：

    create（建发布，要审批与预案）
        → advance（按档位放量，每档观察 ≥ 1 个工作日）
            → resolve（一次询盘命中哪一版，按企业分桶）
                → rollback（出事了按类别切回上一版）
"""
from __future__ import annotations

from .bucketing import enterprise_bucket, in_canary, normalize_company
from .manifest import (CATEGORIES, COMPONENT_CATEGORY, COMPONENT_KEYS, LANGS,
                       OBSERVE_HOURS, STAGES, STAGE_PERCENT, EffectiveRelease,
                       ReleaseManifest, baseline_components, category_keys, utcnow)
from .service import ReleaseError, ReleaseService, http_status_for

__all__ = [
    "CATEGORIES", "COMPONENT_CATEGORY", "COMPONENT_KEYS", "LANGS", "OBSERVE_HOURS",
    "STAGES", "STAGE_PERCENT", "EffectiveRelease", "ReleaseManifest",
    "baseline_components", "category_keys", "utcnow",
    "enterprise_bucket", "in_canary", "normalize_company",
    "ReleaseError", "ReleaseService", "http_status_for",
]