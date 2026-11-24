# -*- coding: utf-8 -*-
"""云端厂商接入层（S5）：全部离线测试，不联网、不计费。"""
import pytest

from tradedesk.config import Config
from tradedesk.llm.chat import ChatClient, LLMError, strip_reasoning
from tradedesk.llm.factory import build_chat_client, build_embedder
from tradedesk.llm.providers import PROVIDERS, get_provider, list_providers, resolve_api_key
from tradedesk.retrieval.embedders import OpenAICompatibleEmbedder, RemoteEmbedder


class FakeTransport:
    """记录请求并返回固定响应的假传输。"""

    def __init__(self, payload=None, error=None, fail_times=0):
        self.payload = payload
        self.error = error
        self.fail_times = fail_times
        self.calls = []

    def __call__(self, url, headers, payload, timeout):
        self.calls.append({"url": url, "headers": headers, "payload": payload})
        if self.fail_times > 0:
            self.fail_times -= 1
            raise OSError("假网络故障")
        if self.error is not None:
            return self.error
        return self.payload


CHAT_OK = {
    "model": "m",
    "choices": [{"message": {"content": "你好"}}],
    "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
}


# ---------------- 厂商注册表 ----------------
def test_provider_table_covers_at_least_four_vendors():
    keys = {p.key for p in list_providers()}
    assert {"minimax", "aliyun", "zhipu", "volcengine", "deepseek"} <= keys, str(keys)


@pytest.mark.parametrize("alias,expect", [
    ("qwen", "aliyun"), ("dashscope", "aliyun"), ("阿里云", "aliyun"),
    ("glm", "zhipu"), ("bigmodel", "zhipu"),
    ("doubao", "volcengine"), ("ark", "volcengine"), ("豆包", "volcengine"),
    ("MiniMax", "minimax"),
])
def test_provider_aliases(alias, expect):
    assert get_provider(alias).key == expect


def test_unknown_provider_raises():
    with pytest.raises(KeyError):
        get_provider("no-such-vendor")


def test_api_key_prefers_vendor_env(monkeypatch):
    p = get_provider("aliyun")
    monkeypatch.setenv(p.api_key_env, "FROM-VENDOR-ENV")
    assert resolve_api_key(p, "FALLBACK") == "FROM-VENDOR-ENV"
    monkeypatch.delenv(p.api_key_env, raising=False)
    assert resolve_api_key(p, "FALLBACK") == "FALLBACK"


# ---------------- 对话客户端 ----------------
@pytest.mark.parametrize("key", ["minimax", "aliyun", "zhipu", "volcengine", "deepseek"])
def test_chat_client_url_and_payload(key):
    p = get_provider(key)
    t = FakeTransport(CHAT_OK)
    c = ChatClient(provider=p, api_key="K", transport=t)
    res = c.complete([{"role": "user", "content": "hi"}])
    call = t.calls[0]
    assert call["url"].startswith(p.chat_base_url)
    assert call["url"].endswith(p.chat_path)
    assert call["headers"]["Authorization"] == "Bearer K"
    assert call["payload"]["model"] == p.chat_model
    assert call["payload"]["messages"] == [{"role": "user", "content": "hi"}]
    assert call["payload"]["temperature"] >= 0
    assert res.text == "你好"
    assert res.usage()["total_tokens"] == 18
    assert res.provider == key


def test_chat_business_error_is_not_retried():
    p = get_provider("minimax")
    t = FakeTransport({"base_resp": {"status_code": 1004, "status_msg": "login fail"}})
    c = ChatClient(provider=p, api_key="K", transport=t)
    with pytest.raises(LLMError):
        c.complete([{"role": "user", "content": "hi"}])
    assert len(t.calls) == 1


def test_chat_network_error_is_retried():
    p = get_provider("aliyun")
    t = FakeTransport(CHAT_OK, fail_times=2)
    c = ChatClient(provider=p, api_key="K", retries=2, transport=t)
    res = c.complete([{"role": "user", "content": "hi"}])
    assert res.text == "你好"
    assert len(t.calls) == 3
    assert c.calls == 3        # 计量用 HTTP 实际调用次数（重试也算）


# ---------------- 嵌入客户端（两种形态） ----------------
def test_minimax_embedding_uses_native_shape():
    t = FakeTransport({"base_resp": {"status_code": 0},
                        "vectors": [[0.1] * 8, [0.2] * 8]})
    e = RemoteEmbedder(base_url="https://x", api_key="K", model="embo-01", dim=8,
                       transport=t, batch_size=2)
    vecs = e.embed(["a", "b"])
    payload = t.calls[0]["payload"]
    assert payload["texts"] == ["a", "b"]
    assert payload["type"] == "db"
    assert len(vecs) == 2
    assert abs(sum(x * x for x in vecs[0]) - 1.0) < 1e-6


@pytest.mark.parametrize("key", ["aliyun", "zhipu", "volcengine"])
def test_openai_style_embedding_payload(key):
    p = get_provider(key)
    t = FakeTransport({"data": [{"embedding": [0.1] * 8}, {"embedding": [0.2] * 8}]})
    e = OpenAICompatibleEmbedder(provider=p, api_key="K", model=p.embedding_model, dim=8,
                                 transport=t, batch_size=2)
    vecs = e.embed(["a", "b"])
    call = t.calls[0]
    assert call["url"] == p.embedding_base_url + p.embedding_path
    assert call["payload"] == {"model": p.embedding_model, "input": ["a", "b"]}
    assert len(vecs) == 2


def test_openai_style_embedding_dimension_mismatch_raises():
    p = get_provider("aliyun")
    t = FakeTransport({"data": [{"embedding": [0.1] * 16}]})
    e = OpenAICompatibleEmbedder(provider=p, api_key="K", model=p.embedding_model, dim=8,
                                 transport=t)
    with pytest.raises(Exception) as exc:
        e.embed(["a"])
    assert "维度" in str(exc.value)


# ---------------- 工厂 ----------------
@pytest.mark.parametrize("key", ["minimax", "aliyun", "zhipu", "volcengine", "deepseek"])
def test_factory_builds_chat_client_per_vendor(monkeypatch, key):
    p = get_provider(key)
    monkeypatch.setenv(p.api_key_env, "K")
    cfg = Config(llm_provider=key, llm_api_key="")
    c = build_chat_client(cfg)
    assert c is not None
    assert c.provider.key == key
    assert c.base_url == p.chat_base_url
    assert c.model == p.chat_model


def test_factory_returns_none_without_key(monkeypatch):
    for env in ("MINIMAX_API_KEY", "DASHSCOPE_API_KEY", "ZHIPU_API_KEY",
                "ARK_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    cfg = Config(llm_provider="aliyun", llm_api_key="")
    assert build_chat_client(cfg) is None
    assert build_embedder(cfg) is None


def test_factory_switches_embedder_vendor(monkeypatch):
    p = get_provider("aliyun")
    monkeypatch.setenv(p.api_key_env, "K")
    cfg = Config(embedder_provider="aliyun", llm_api_key="")
    e = build_embedder(cfg)
    assert isinstance(e, OpenAICompatibleEmbedder)
    assert e.provider.key == "aliyun"
    assert e.model == p.embedding_model


# ---------------- HTTP 4xx = 业务错误（不重试） ----------------
def _http_error(code=401, body=b'{"error":{"message":"invalid api key"}}'):
    import urllib.error
    import io as _io
    return urllib.error.HTTPError("https://x", code, "Unauthorized",
                                  {}, _io.BytesIO(body))


def test_chat_http_401_is_business_error_not_retried():
    """OpenAI 兼容厂商（阿里云/智谱/火山）的鉴权失败是 HTTP 4xx，不是网络故障。

    若当成网络故障重试三次：每次都计费，而且报错指向"网络"，排查方向是错的。
    """
    p = get_provider("aliyun")
    t = FakeTransport(error=None)
    t.error = None

    def boom(url, headers, payload, timeout):
        t.calls.append({"url": url})
        raise _http_error()

    c = ChatClient(provider=p, api_key="BAD", transport=boom)
    with pytest.raises(LLMError) as exc:
        c.complete([{"role": "user", "content": "hi"}])
    assert "401" in str(exc.value)
    assert len(t.calls) == 1          # 只调一次，没有重试


def test_chat_openai_style_error_body_raises():
    """HTTP 200 但 body 是 {"error": {...}} —— 也算业务错误。"""
    p = get_provider("zhipu")
    t = FakeTransport({"error": {"code": "1214", "message": "模型不存在"}})
    c = ChatClient(provider=p, api_key="K", transport=t)
    with pytest.raises(LLMError) as exc:
        c.complete([{"role": "user", "content": "hi"}])
    assert "模型不存在" in str(exc.value)


def test_embedding_http_401_is_business_error_not_retried():
    from tradedesk.retrieval.embedders import OpenAICompatibleEmbedder
    p = get_provider("volcengine")
    calls = []

    def boom(url, headers, payload, timeout):
        calls.append(url)
        raise _http_error(401, b'{"error":"invalid api key"}')

    e = OpenAICompatibleEmbedder(provider=p, api_key="BAD", model=p.embedding_model,
                                 dim=8, transport=boom)
    with pytest.raises(Exception) as exc:
        e.embed(["a"])
    assert "401" in str(exc.value)
    assert len(calls) == 1              # 没有重试


# ---------------- 厂商能力边界 ----------------
def test_deepseek_has_no_embedding_interface():
    """DeepSeek 官方未提供嵌入接口（未找到）→ 语义通道必须另配厂商。"""
    p = get_provider("deepseek")
    assert not p.supports_embedding


def test_build_embedder_returns_none_for_chat_only_vendor(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "K")
    cfg = Config(embedder_provider="deepseek", llm_api_key="")
    assert build_embedder(cfg) is None


def test_build_embedder_falls_back_to_chat_provider(monkeypatch):
    """嵌入厂商留空 = 跟随对话厂商。"""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "K")
    cfg = Config(llm_provider="aliyun", embedder_provider="", llm_api_key="")
    e = build_embedder(cfg)
    assert e is not None and e.provider.key == "aliyun"


# ---------------- 通用 key 与维度优先级 ----------------
def test_generic_env_key_is_used(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setenv("TRADEDESK_LLM_API_KEY", "GENERIC")
    p = get_provider("aliyun")
    assert resolve_api_key(p, "") == "GENERIC"


def test_embedder_dim_precedence(monkeypatch):
    """TRADEDESK_EMBEDDER_DIM > 厂商表标称值。"""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "K")
    cfg = Config(embedder_provider="aliyun", embedder_dim=2048, llm_api_key="")
    e = build_embedder(cfg)
    assert e.dim == 2048
    cfg2 = Config(embedder_provider="aliyun", embedder_dim=0, llm_api_key="")
    e2 = build_embedder(cfg2)
    assert e2.dim == get_provider("aliyun").embedding_dim


def test_config_reads_generic_llm_env(monkeypatch, tmp_path):
    """配置层也要认通用变量，否则换厂商还要改代码里的默认值。"""
    from tradedesk.config import Config
    env = tmp_path / ".env"
    env.write_text("TRADEDESK_LLM_PROVIDER=aliyun\nTRADEDESK_LLM_API_KEY=K1\n"
                   "TRADEDESK_LLM_BASE_URL=https://example.test\n", encoding="utf-8")
    cfg = Config.load(project_root=tmp_path, env_file=".env")
    assert cfg.llm_provider == "aliyun"
    assert cfg.llm_api_key == "K1"
    assert cfg.llm_base_url == "https://example.test"


def test_base_url_does_not_cross_vendors(monkeypatch, tmp_path):
    """切到阿里云时不能拿 MiniMax 的地址——地址错了表现是 401/404，不是接口不存在。"""
    env = tmp_path / ".env"
    env.write_text(
        "TRADEDESK_LLM_PROVIDER=aliyun\n"
        "DASHSCOPE_API_KEY=K-ALY\n"
        "MINIMAX_BASE_URL=https://api.minimaxi.com\n", encoding="utf-8")
    cfg = Config.load(project_root=tmp_path, env_file=".env")
    assert cfg.llm_provider == "aliyun"
    assert cfg.llm_api_key == "K-ALY"                 # .env 里厂商专用的 key 也要读得到
    c = build_chat_client(cfg)
    assert c is not None
    assert c.base_url == get_provider("aliyun").chat_base_url
    assert "minimax" not in c.base_url


def test_vendor_base_url_env_is_used_when_set(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "TRADEDESK_LLM_PROVIDER=minimax\n"
        "MINIMAX_API_KEY=K-MM\n"
        "MINIMAX_BASE_URL=https://api.minimax.chat\n", encoding="utf-8")
    cfg = Config.load(project_root=tmp_path, env_file=".env")
    c = build_chat_client(cfg)
    assert c.base_url == "https://api.minimax.chat"


# ---------------- 思考块剔除（会发给买家，不能混进去） ----------------
def test_strip_reasoning_removes_think_block():
    assert strip_reasoning("<think>\u6211应该\u7b54PONG</think>\n\nPONG") == "PONG"


def test_strip_reasoning_removes_unterminated_think_block():
    assert strip_reasoning("PONG\n<think>还在想") == "PONG"


def test_strip_reasoning_keeps_normal_text():
    assert strip_reasoning("Hello, 世界") == "Hello, 世界"


def test_chat_result_has_no_reasoning_block():
    """实测事实：MiniMax-M3 把思考写在 content 里。
    如果不剔，对外发的就是一段推理过程。"""
    p = get_provider("minimax")
    t = FakeTransport({"model": "MiniMax-M3",
                        "choices": [{"message": {"content":
                                 "<think>\u5185\u90e8\u601d\u8003</think>\n\nDear buyer, our MOQ is 500 pcs."}}]})
    c = ChatClient(provider=p, api_key="K", transport=t)
    res = c.complete([{"role": "user", "content": "hi"}])
    assert res.text == "Dear buyer, our MOQ is 500 pcs."
    assert "think" not in res.text.lower()


# ---------------- 定价口径（PRD 12.4 的 ≤0.2 元/条） ----------------
def test_pricing_table_is_sourced_and_dated():
    """价格表必须带来源与取数日期（口径可核）。"""
    from tradedesk.llm.pricing import AS_OF, SOURCE_URL, PRICES
    assert SOURCE_URL.startswith("https://")
    assert len(AS_OF) == 10
    for p in PRICES:
        assert p.model and p.tier and p.note
        assert p.input_per_mtok >= 0 and p.output_per_mtok >= 0


def test_estimate_cost_uses_official_price():
    from tradedesk.llm.pricing import estimate_cost
    # 估算取**最贵的一档**（保守口径，R19-10）：表里 M3 有「≤512k 输入」与「>512k 输入」两档，
    # 同样 100 万 token，取保守档就是取更贵的那一档。
    cost, tier = estimate_cost("MiniMax-M3", 1_000_000, 0)
    assert abs(cost - 8.40) < 1e-9, f"估算应取最贵档（保守）：{cost}"
    cost2, _ = estimate_cost("MiniMax-M3", 0, 1_000_000)
    assert abs(cost2 - 33.60) < 1e-9


def test_estimate_cost_unknown_model_returns_none():
    """没核实的模型不能猜价格 → 返回 None。"""
    from tradedesk.llm.pricing import estimate_cost
    assert estimate_cost("some-unknown-model", 1000, 1000) is None


def test_pricing_range_spans_discount_and_long_context():
    from tradedesk.llm.pricing import estimate_cost_range
    rng = estimate_cost_range("MiniMax-M3", 1000, 1000)
    assert rng is not None and rng[0] < rng[1]


def test_minimax_default_model_is_not_retired():
    """MiniMax-Text-01 已下线（实测 404）——默认模型必须是当前可用的。"""
    p = get_provider("minimax")
    assert p.chat_model == "MiniMax-M3"
