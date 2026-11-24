# -*- coding: utf-8 -*-
"""角色、可见域与本地演示认证（S6-4）。

**这里的认证是什么、不是什么**
------------------------------------
是：**本地演示账号目录**（账号 + 加密密码 + 会话令牌），用于让演示有真实的登录流程。
不是：**生产鉴权**。PRD 13.4.11 把"企业账号 + SSO（OIDC）+ 四类角色" 列在**生产环境**（D-11，IT 负责，**不在开发范围**）；
本地 Demo 的形态是“**本地演示账号 + 密码登录**”（会话令牌存内存，默认**不强制**）。

为什么还要做登录
--------------
PRD 11.2 的**四类可见域**（内部成本价 / 参考价区间 / 历史成交价 / 成本结构与毛利）
是**产品机制**（规则是"分级注入"），而不是界面美观。没有身份入口，这套规则在演示里根本看不到。

**边界（很重要）**
- 本地认证默认**不开启**（`TRADEDESK_AUTH_REQUIRED=0`）：否则审核与验证脚本会被拒。
- 开启后（`=1`）：业务端点必须带有效会话令牌（`X-Session-Token`）。
- 密码用 PBKDF2-HMAC-SHA256 加盐存储（`hashlib`，不引入新依赖）。
- **演示凭据写在前端登录页上**（本地 Demo 形态），不得被误认为生产凭据。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from typing import Any, Dict, List, Optional

# PRD 11.2 的四类可见域（可见范围）
PRICE_FIELDS = {
    "internal_cost": "内部成本价 / 底价",
    "reference_price": "参考价区间",
    "deal_history": "历史成交价",
    "cost_structure": "成本结构与毛利",
}

ROLES: List[Dict[str, Any]] = [
    {
        "id": "sales",
        "name": "外贸业务员",
        "user": "李静",
        "desc": "处理询盘、签核报价、发回函；看得到参考价区间与全部引用。",
        "views": ["threads", "conversation", "draft", "citations", "evidence", "gates", "signoff"],
        "price_fields": ["reference_price"],
        "can": ["sign_off", "send", "advance_stage"],
    },
    {
        "id": "manager",
        "name": "授权经理",
        "user": "王强",
        "desc": "在业务员之上：看得到内部成本区间，并可对低风险草稿批量签核（PRD FR-HUM-10）。",
        "views": ["threads", "conversation", "draft", "citations", "evidence", "gates", "signoff", "batch"],
        "price_fields": ["reference_price", "internal_cost"],
        # "release"：发布管理权（建发布 / 推进灰度档位 / 回滚）——
        # 只有承担发布责任的岗位才有，业务员不代劳（S6-3 越权闸门）。
        "can": ["sign_off", "send", "advance_stage", "batch_signoff", "release"],
    },
    {
        "id": "compliance",
        "name": "风控合规岗",
        "user": "陈敏",
        "desc": "只看名单命中证据链、风险事件与闸门；**不接触报价正文，也不能签核**。",
        "views": ["threads", "conversation_raw", "evidence", "gates", "risk"],
        "price_fields": [],
        "can": ["screen", "escalate"],
    },
    {
        "id": "exec",
        "name": "管理层",
        "user": "周砚",
        "desc": "只看聚合指标与漏斗，**不看单条询盘正文**。",
        "views": ["metrics"],
        "price_fields": [],
        "can": ["view_metrics"],
    },
]

ROLE_BY_ID: Dict[str, Dict[str, Any]] = {r["id"]: r for r in ROLES}

# 演示账号目录（本地 Demo；生产账号由 IT 通过 OIDC 提供，PRD D-11）
DEMO_PASSWORD = "demo1234"
DEMO_USERS: List[Dict[str, str]] = [
    {"username": "sales", "password": DEMO_PASSWORD, "role": "sales", "name": "李静"},
    {"username": "manager", "password": DEMO_PASSWORD, "role": "manager", "name": "王强"},
    {"username": "compliance", "password": DEMO_PASSWORD, "role": "compliance", "name": "陈敏"},
    {"username": "exec", "password": DEMO_PASSWORD, "role": "exec", "name": "周砚"},
]

_PBKDF2_ROUNDS = 120_000
_HASH_CACHE: Dict[str, str] = {}


def _hash_password(password: str, salt: bytes) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return dk.hex()


def _verify_password(password: str, salt_hex: str, expected_hex: str) -> bool:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex),
                            _PBKDF2_ROUNDS)
    return hmac.compare_digest(dk.hex(), expected_hex)


def _demo_record(username: str) -> Optional[Dict[str, Any]]:
    """演示账号的哈希记录（第一次用时算，然后复用）。"""
    if username in _HASH_CACHE:
        cached = _HASH_CACHE[username]
        if cached.startswith("!"):
            return None
        salt_hex, hash_hex = cached.split("$", 1)
        return {"salt": salt_hex, "hash": hash_hex}
    for u in DEMO_USERS:
        if u["username"] == username:
            salt = secrets.token_bytes(16)
            digest = _hash_password(u["password"], salt)
            _HASH_CACHE[username] = "%s$%s" % (salt.hex(), digest)
            return {"salt": salt.hex(), "hash": digest}
    _HASH_CACHE[username] = "!unknown"
    return None


class AuthError(Exception):
    """鉴权失败。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# 会话：内存存储（演示形态；重启后需要重新登录）
SESSION_TTL_S = 8 * 3600
_SESSIONS: Dict[str, Dict[str, Any]] = {}


def auth_required() -> bool:
    """是否强制鉴权（默认关，不打断审核与验证脚本）。

    **只从 config 读**（`src/tradedesk/config.py` 是环境变量唯一真源），
    不在这里另读一份 os.environ —— 两处各读一次迟早会不一致。
    """
    from ..config import get_settings
    return bool(get_settings().auth_required)


def login(username: str, password: str) -> Dict[str, Any]:
    """校验账号密码，签发会话令牌。

    失败时不区分"账号不存在"与"密码错"（不给攻击者额外信息）。
    """
    username = (username or "").strip()
    if not username or not password:
        raise AuthError("missing_credentials", "请输入账号与密码")
    rec = _demo_record(username)
    if rec is None or not _verify_password(password, rec["salt"], rec["hash"]):
        raise AuthError("invalid_credentials", "账号或密码不正确")
    user = next(u for u in DEMO_USERS if u["username"] == username)
    role = ROLE_BY_ID.get(user["role"], ROLE_BY_ID["sales"])
    token = secrets.token_urlsafe(32)
    _SESSIONS[token] = {
        "username": user["username"],
        "name": user["name"],
        "role": user["role"],
        "expires_at": time.time() + SESSION_TTL_S,
    }
    return {"token": token, "user": _public_user(_SESSIONS[token])}


def logout(token: str) -> bool:
    return _SESSIONS.pop(token, None) is not None


def session_user(token: str) -> Optional[Dict[str, Any]]:
    """取会话对应的用户；过期或不存在返回 None。"""
    if not token:
        return None
    s = _SESSIONS.get(token)
    if not s:
        return None
    if s["expires_at"] < time.time():
        _SESSIONS.pop(token, None)
        return None
    return _public_user(s)


def _public_user(s: Dict[str, Any]) -> Dict[str, Any]:
    role = ROLE_BY_ID.get(s["role"], ROLE_BY_ID["sales"])
    return {
        "username": s["username"],
        "name": s["name"],
        "role": s["role"],
        "role_name": role["name"],
        "views": role["views"],
        "price_fields": role["price_fields"],
        "can": role["can"],
    }


def has_capability(user: Optional[Dict[str, Any]], capability: str) -> bool:
    """会话用户是否具备某项能力（`can` 列表）。

    写操作前的角色闸门用它：能力不在角色目录里 → 不许做。
    能力名与 `ROLES[].can` 一一对应，改角色表即改权限，不必改业务代码。
    """
    if not user:
        return False
    return capability in (user.get("can") or [])


def role_catalog() -> Dict[str, Any]:
    """角色目录（前端的规则来源，避免前后端各写一份）。"""
    return {
        "roles": ROLES,
        "price_fields": PRICE_FIELDS,
        "auth_required": auth_required(),
        "demo_accounts": [{"username": u["username"], "password": u["password"],
                           "role_name": ROLE_BY_ID[u["role"]]["name"], "name": u["name"]}
                          for u in DEMO_USERS],
        "notice": ("本地演示形态：角色来自**账号密码登录**，不是点选角色；本地演示鉴权不是鉴权（生产鉴权用 OIDC，"
                   "PRD 13.4.11 / D-11，本期不实现）。"),
    }
