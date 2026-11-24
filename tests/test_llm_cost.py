# -*- coding: utf-8 -*-
"""S5-1b 测试：成本落库 + 签核留痕 + 双后端口径一致。

全部离线：不联网、不计费。
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from tradedesk.api.app import create_app
from tradedesk.api.deps import AppContext
from tradedesk.domain.models import LLMUsage
from tradedesk.store.base import summarize_llm_usage
from tradedesk.store.sqlite_store import SqliteStateStore

GOOD_BODY = (
    "Dear buyer, thank you for your enquiry about {{sku}}. "
    "For {{moq}} units our reference price is {{price_range}}, "
    "on {{terms}} basis from {{port}}, with a lead time of {{lead_time}}. "
    "The model is covered by {{cert_sentence}} "
    "Please confirm {{ask_line}} so that we can prepare the full quotation."
)


class FakeChat:
    """假云端客户端。`model` 可以假造为已核实单价的模型名，用来验证计价路径。"""

    def __init__(self, text=GOOD_BODY, model="MiniMax-M3", prompt=688, completion=464):
        self.text = text
        self.model = model
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.messages = []
        self.calls = 0
        self.provider = type("P", (), {"key": "minimax", "name": "MiniMax"})()

    def complete(self, messages, **kw):
        self.calls += 1
        self.messages = messages
        from tradedesk.llm.chat import ChatResult
        return ChatResult(text=self.text, model=self.model, provider="minimax",
                          prompt_tokens=self.prompt_tokens,
                          completion_tokens=self.completion_tokens,
                          total_tokens=self.prompt_tokens + self.completion_tokens,
                          raw={"usage": {"prompt_tokens": self.prompt_tokens,
                                         "completion_tokens": self.completion_tokens,
                                         "prompt_tokens_details": {"cached_tokens": 0}}})


def _usage(**kw):
    base = dict(thread_id="T-1", draft_id="D-1", lang="en", provider="minimax",
                model="MiniMax-M3", prompt_tokens=688, completion_tokens=464,
                total_tokens=1152, http_calls=1, cost_cny=0.0107, price_tier="标准档")
    base.update(kw)
    return LLMUsage(**base)


# ---------------------------------------------------------------- 存储层
def test_record_and_summary_sqlite(tmpdir_local):
    store = SqliteStateStore(str(tmpdir_local / "cost.db"))
    try:
        store.record_llm_usage(_usage())
        store.record_llm_usage(_usage(thread_id="T-2", lang="es", cost_cny=0.02))
        s = store.llm_usage_summary()
        assert s["count"] == 2
        assert s["total_tokens"] == 2304
        assert s["http_calls"] == 2
        assert s["priced_count"] == 2
        assert abs(s["cost_cny"] - 0.0307) < 1e-9
        assert abs(s["avg_cost_cny"] - 0.01535) < 1e-9
        assert abs(s["max_cost_cny"] - 0.02) < 1e-9
        assert set(s["by_lang"]) == {"en", "es"}
        assert s["by_lang"]["es"]["count"] == 1
        # 按语种过滤
        assert store.llm_usage_summary(lang="es")["count"] == 1
        assert store.llm_usage_summary(lang="zh")["count"] == 0
        # 按线程过滤
        assert store.llm_usage_summary(thread_id="T-2")["count"] == 1
    finally:
        store.close()


def test_unpriced_rows_do_not_become_zero_cost(tmpdir_local):
    """单价未核实 → cost_cny 为 None，**不是 0**。

    0 元会让人误以为“不费钱”；真实是“算不出来”。
    """
    store = SqliteStateStore(str(tmpdir_local / "cost2.db"))
    try:
        store.record_llm_usage(_usage(cost_cny=None, price_tier=""))
        s = store.llm_usage_summary()
        assert s["count"] == 1
        assert s["priced_count"] == 0
        assert s["cost_cny"] is None
        assert s["avg_cost_cny"] is None
    finally:
        store.close()


def test_summary_helper_is_shared_by_both_backends():
    """汇总逻辑只有一份（store/base.py）——双后端不得各写一遍（R17 教训）。"""
    rows = [{"lang": "en", "prompt_tokens": 10, "completion_tokens": 5,
             "cached_tokens": 0, "total_tokens": 15, "http_calls": 1,
             "cost_cny": 0.01, "degraded": 0}]
    s = summarize_llm_usage(rows)
    assert s["count"] == 1 and s["priced_count"] == 1
    assert abs(s["cost_cny"] - 0.01) < 1e-9


def test_postgres_summary_matches_sqlite(tmpdir_local):
    """同一组数据在两个后端的汇总结果必须**逐条一致**。

    PostgreSQL 部分**自建一个临时库并在结束时回收**（不能往实现方的库里写测试数据）。
    """
    base = os.environ.get("TRADEDESK_TEST_DSN")
    if not base:
        pytest.skip("未提供 PostgreSQL DSN（dev.ps1 check -Dsn ...）")
    import uuid
    import psycopg
    from tradedesk.store.postgres_store import PostgresStateStore
    head, _, _db = base.rpartition("/")
    name = "test_cost_" + uuid.uuid4().hex[:8]
    with psycopg.connect(head + "/postgres", autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    pg = PostgresStateStore(head + "/" + name)
    sqlite_store = SqliteStateStore(str(tmpdir_local / "cmp.db"))
    try:
        for u in (_usage(), _usage(thread_id="T-2", lang="ar", cost_cny=0.02)):
            pg.record_llm_usage(u)
            sqlite_store.record_llm_usage(u)
        a = pg.llm_usage_summary()
        b = sqlite_store.llm_usage_summary()
        assert a["count"] == b["count"]
        assert a["total_tokens"] == b["total_tokens"]
        assert a["http_calls"] == b["http_calls"]
        assert abs((a["cost_cny"] or 0) - (b["cost_cny"] or 0)) < 1e-9
        assert set(a["by_lang"]) == set(b["by_lang"])
    finally:
        pg.close()
        sqlite_store.close()
        with psycopg.connect(head + "/postgres", autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


# ---------------------------------------------------------------- 端到端
@pytest.fixture
def wired(tmpdir_local):
    """AppContext + 假云端客户端（不联网、不计费）。"""
    store = SqliteStateStore(str(tmpdir_local / "wired.db"))
    chat = FakeChat()
    ctx = AppContext(store=store, chat=chat)
    client = TestClient(create_app(ctx))
    yield client, store, chat
    store.close()


MSG = "Quote 5000 pcs of X-200 FOB Shenzhen, do you have CE certification?"


def test_draft_carries_usage_and_cost(wired):
    client, store, chat = wired
    d = client.post("/inbound", json={"buyer_id": "BY-BR-01", "message": MSG,
                                      "language": "en", "company": "ACME", "country": "BR"}).json()
    assert d.get("draft")
    s = store.llm_usage_summary()
    assert s["count"] == 1
    assert s["http_calls"] == 1
    assert s["total_tokens"] == 1152
    # 模型名在已核实的价格表里 → 能算出成本
    assert s["priced_count"] == 1
    assert s["cost_cny"] is not None and s["cost_cny"] > 0


def test_usage_is_persisted_and_queryable_by_api(wired):
    client, store, chat = wired
    client.post("/inbound", json={"buyer_id": "BY-BR-01", "message": MSG,
                                  "language": "en", "company": "ACME", "country": "BR"})
    r = client.get("/usage/costs").json()
    assert r["count"] == 1
    assert r["by_lang"]["en"]["count"] == 1
    assert client.get("/usage/costs?lang=es").json()["count"] == 0


def test_signoff_trail_records_model_and_cost(wired):
    """签核留痕必须记住“这份稿是谁写的、花了多少钱”。"""
    client, store, chat = wired
    d = client.post("/inbound", json={"buyer_id": "BY-BR-01", "message": MSG,
                                      "language": "en", "company": "ACME", "country": "BR"}).json()
    tid, did = d["thread_id"], d["draft_id"]
    token = client.post(f"/threads/{tid}/gate-token", json={"draft_id": did}).json()["token"]
    client.post(f"/threads/{tid}/signoff", json={"draft_id": did, "action": "approve"},
                headers={"X-Gate-Token": token})
    events = client.get(f"/threads/{tid}/events").json()
    signed = [e for e in events if e["event_type"] == "signed_off"]
    assert signed, "应有签核事件"
    payload = signed[0]["payload"]
    assert payload.get("draft_model") == "minimax:MiniMax-M3", payload
    assert payload.get("llm_cost_cny") is not None
    assert payload.get("unit_price") is not None, "原有的价格要素不得丢"


def test_degraded_draft_is_flagged_on_thread(wired):
    """模型越权被拒 → 草稿走模板，且线程上要留下“降级”的痕迹。"""
    client, store, chat = wired
    chat.text = "Dear buyer, our price is USD 1.00 per unit, best price guaranteed!"
    client.post("/inbound", json={"buyer_id": "BY-BR-01", "message": MSG,
                                  "language": "en", "company": "ACME", "country": "BR"})
    s = store.llm_usage_summary()
    assert s["count"] == 1
    assert s["degraded_count"] == 1
    # 降级行也是花钱的（R19-05）：越权被拒时响应里有 usage → token 照记、成本照算。
    assert s["priced_count"] == 1, "降级的那一次调用同样是花钱的，不能从成本里被剔掉"
    assert s["cost_cny"] is not None and s["cost_cny"] > 0


def test_unknown_model_is_not_priced(wired):
    """模型不在已核实的价格表里 → 不估算金额（不猜单价）。"""
    client, store, chat = wired
    chat.model = "some-unknown-model"
    client.post("/inbound", json={"buyer_id": "BY-BR-01", "message": MSG,
                                  "language": "en", "company": "ACME", "country": "BR"})
    s = store.llm_usage_summary()
    assert s["count"] == 1
    assert s["priced_count"] == 0
    assert s["cost_cny"] is None
