# -*- coding: utf-8 -*-
"""S6-4 测试：本地演示认证 + 角色可见域。

全部离线：不联网、不计费。
"""
import pytest
from fastapi.testclient import TestClient

from tradedesk.api.app import create_app
from tradedesk.api.deps import AppContext
from tradedesk.store.sqlite_store import SqliteStateStore


@pytest.fixture
def client(tmpdir_local):
    store = SqliteStateStore(str(tmpdir_local / "auth.db"))
    c = TestClient(create_app(AppContext(store=store)))
    yield c
    store.close()


def test_login_with_correct_password(client):
    r = client.post("/auth/login", json={"username": "sales", "password": "demo1234"})
    assert r.status_code == 200
    d = r.json()
    assert d["token"]
    assert d["user"]["role"] == "sales"
    assert d["user"]["name"]


def test_login_with_wrong_password_is_rejected(client):
    r = client.post("/auth/login", json={"username": "sales", "password": "wrong"})
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "invalid_credentials"


def test_unknown_user_is_not_distinguishable_from_wrong_password(client):
    """不能用“账号不存在”区分“密码错”（不给攻击者额外信息）。"""
    a = client.post("/auth/login", json={"username": "no-such-user", "password": "x"})
    b = client.post("/auth/login", json={"username": "sales", "password": "x"})
    assert a.status_code == b.status_code == 401
    assert a.json()["detail"]["message"] == b.json()["detail"]["message"]


def test_empty_credentials_are_rejected(client):
    r = client.post("/auth/login", json={"username": "", "password": ""})
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "missing_credentials"


def test_session_is_required_for_me(client):
    assert client.get("/auth/me").status_code == 401


def test_me_returns_the_logged_in_user(client):
    tok = client.post("/auth/login", json={"username": "manager", "password": "demo1234"}).json()["token"]
    r = client.get("/auth/me", headers={"X-Session-Token": tok})
    assert r.status_code == 200
    assert r.json()["user"]["role"] == "manager"


def test_logout_invalidates_the_session(client):
    tok = client.post("/auth/login", json={"username": "sales", "password": "demo1234"}).json()["token"]
    assert client.get("/auth/me", headers={"X-Session-Token": tok}).status_code == 200
    assert client.post("/auth/logout", headers={"X-Session-Token": tok}).json()["ok"] is True
    assert client.get("/auth/me", headers={"X-Session-Token": tok}).status_code == 401


def test_password_is_not_stored_in_plaintext():
    """密码只以 PBKDF2 派生值存留——目录里不得出现明文。"""
    from tradedesk.api import auth
    assert auth.DEMO_PASSWORD not in repr(auth._HASH_CACHE.values())
    rec = auth._demo_record("sales")
    assert rec is not None
    assert auth.DEMO_PASSWORD not in rec["hash"]
    assert rec["salt"] != rec["hash"]


def test_role_catalog_covers_four_roles_and_price_visibility():
    """PRD 11.2 的四类可见域：成本价只给业务员与授权经理。"""
    from tradedesk.api.auth import role_catalog
    cat = role_catalog()
    ids = {r["id"] for r in cat["roles"]}
    assert ids == {"sales", "manager", "compliance", "exec"}
    by = {r["id"]: r for r in cat["roles"]}
    assert by["sales"]["price_fields"] == ["reference_price"]
    assert "internal_cost" in by["manager"]["price_fields"]
    assert by["compliance"]["price_fields"] == []
    assert by["exec"]["price_fields"] == []
    assert "sign_off" in by["sales"]["can"]
    assert "sign_off" not in by["compliance"]["can"]
    assert "不是鉴权" in cat["notice"]


def test_auth_is_off_by_default(monkeypatch):
    """鉴权默认关：否则审核与验证脚本会被拒。"""
    from tradedesk.api.auth import auth_required
    from tradedesk.config import reset_settings
    monkeypatch.delenv("TRADEDESK_AUTH_REQUIRED", raising=False)
    reset_settings()
    assert auth_required() is False
    monkeypatch.setenv("TRADEDESK_AUTH_REQUIRED", "1")
    reset_settings()          # config 有缓存，改完环境变量要清一次
    assert auth_required() is True
    reset_settings()


def test_auth_required_blocks_business_endpoints(monkeypatch, tmpdir_local):
    from tradedesk.config import reset_settings
    monkeypatch.setenv("TRADEDESK_AUTH_REQUIRED", "1")
    reset_settings()
    store = SqliteStateStore(str(tmpdir_local / "req.db"))
    c = TestClient(create_app(AppContext(store=store)))
    try:
        assert c.get("/health").status_code == 200          # 健康检查不拦
        assert c.get("/workbench").status_code == 401       # 业务端点拦
        r = c.post("/auth/login", json={"username": "sales", "password": "demo1234"})
        assert r.status_code == 200
        tok = r.json()["token"]
        assert c.get("/workbench", headers={"X-Session-Token": tok}).status_code == 200
    finally:
        store.close()
        reset_settings()
