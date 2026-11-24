# -*- coding: utf-8 -*-
"""发布清单（Release Manifest）—— S6-3 灰度与回滚（PRD 13.4.8）。

一个"发布"= 六个可独立回滚的组件版本号的集合：

    app_image          应用镜像
    agentspec_version  AgentSpec（角色契约）版本
    prompt_version     提示词资产版本
    glossary_version   多语术语表版本（随知识库快照一起版本化）
    kb_version         知识库版本
    list_version       制裁/管制名单库版本

它们归成**四个回滚类别**（PRD 13.4.8：发布单元四类独立回滚）：

    app       —— 应用镜像
    agent     —— AgentSpec 与提示词（一起回滚，不能只回滚一半）
    knowledge —— 知识库（含术语表快照）
    sanctions —— 名单库（变更必须重跑 L-6 风控回归）

三条硬规矩
----------
1. **审批人 / 变更说明 / 回滚预案是必填**：没有预案的发布不许创建 ——
   真出事时"回滚"必须是**事先写好的动作**，不是临场发挥。
2. **改动 Agent 或名单库必须附证据**：Agent 变更要附回归证据，
   名单变更要附 L-6 风控回归证据（PRD 13.4.8"名单库变更必须重跑 L-6"）。
3. **同时只允许一个灰度**：否则"哪家企业在哪个版本"就说不清了。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List

from pydantic import BaseModel, Field

# ---- 四个回滚类别（PRD 13.4.8） ----
CATEGORIES = ("app", "agent", "knowledge", "sanctions")

# ---- 六个组件 → 四个类别 ----
COMPONENT_CATEGORY: Dict[str, str] = {
    "app_image": "app",
    "agentspec_version": "agent",
    "prompt_version": "agent",
    "glossary_version": "knowledge",
    "kb_version": "knowledge",
    "list_version": "sanctions",
}
COMPONENT_KEYS = tuple(COMPONENT_CATEGORY)

# ---- 灰度档位（PRD 13.4.8：PILOT → 20% → 50% → 100%，每档观察 ≥ 1 个工作日） ----
STAGES = ("PILOT", "P20", "P50", "P100")
STAGE_PERCENT = {"P20": 20, "P50": 50, "P100": 100}
OBSERVE_HOURS = 24          # 观察窗（小时）；PRD 写的是"≥1 个工作日"，取 24 小时为下限
LANGS = ("en", "es", "ar", "zh")

STATUS_CANARY = "canary"            # 灰度中
STATUS_FULL = "full"                # 已全量
STATUS_ROLLED_BACK = "rolled_back"  # 全部改动类别都已回滚


def utcnow() -> datetime:
    """统一时间源（带时区）。灰度观察窗全靠它算，别处不要再单独 new datetime。"""
    return datetime.now(timezone.utc)


class ReleaseManifest(BaseModel):
    """一次发布的完整档案（落库后即可复原"当时到底发了什么"）。"""

    release_id: str
    components: Dict[str, str]
    # ---- 审计必填项（PRD 13.4.8：审计记版本号 / 变更 / 审批人 / 灰度范围 / 回滚预案） ----
    approver: str
    change_notes: str
    rollback_plan: str
    # ---- 证据 ----
    regression_evidence: str = ""   # Agent/提示词变更的回归证据
    risk_evidence: str = ""         # 名单库变更的 L-6 风控回归证据
    # ---- 灰度范围 ----
    pilot_companies: List[str] = Field(default_factory=list)  # PILOT 档的白名单企业
    langs: List[str] = Field(default_factory=list)             # 空 = 全语种
    # ---- 推进状态 ----
    stage: str = "PILOT"
    stage_entered_at: datetime = Field(default_factory=utcnow)
    status: str = STATUS_CANARY
    changed_categories: List[str] = Field(default_factory=list)
    rolled_back_categories: List[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class EffectiveRelease(BaseModel):
    """一次询盘实际命中了哪一版（灰度分流的输出）。"""

    components: Dict[str, str]
    source: str          # canary（灰度版本）/ stable（已发布别名）/ baseline（没有任何发布）
    release_id: str = "" # 对应的发布号（stable 指向多个发布时为空）


def baseline_components(kb_version: str, list_version: str) -> Dict[str, str]:
    """当前**基线**组件版本（没有任何发布时的默认口径）。

    基线 = 本机正在跑的东西：应用镜像是本地构建，AgentSpec 与提示词取代码里的版本号，
    知识库与术语表取当前 KB 版本，名单库取配置的名单版本。
    延迟导入 agents 包是为了避免循环依赖（store.base 会 import 本模块）。
    """
    from ..agents.inquiry import INQUIRY_SPEC
    from ..agents.prompts import PROMPT_VERSION

    return {
        "app_image": "tradedesk/app:local",
        "agentspec_version": INQUIRY_SPEC.version,
        "prompt_version": PROMPT_VERSION,
        "glossary_version": kb_version,
        "kb_version": kb_version,
        "list_version": list_version,
    }


def category_keys(category: str) -> List[str]:
    """某个回滚类别包含哪些组件键。"""
    return [k for k in COMPONENT_KEYS if COMPONENT_CATEGORY[k] == category]