# -*- coding: utf-8 -*-
"""嵌入器实现（S4-4a）。

三个实现，一个协议（`retrieval/base.py` 的 `Embedder`）：

| 实现 | 语义能力 | 用途 |
| --- | --- | --- |
| `RemoteEmbedder` | ✓ 真语义 | 云端嵌入 API（MiniMax / OpenAI 兼容）——生产与测试的真实语义通道 |
| `HashEmbedder` | ✗ 假 | 离线确定性嵌入（字符 n-gram 哈希）——只用于测试与“接线”，**不具备语义能力** |
| `NullEmbedder` | ✗ 假 | 占位（旧接口保留） |

为什么这个接口里要加 `semantic` 标志位
--------------------------------
PRD 9.5 的两层口径写得很清楚：公开检索基准（MIRACL/MLDR/…）只能证明“检索管线没问题”，
证明不了“询盘场景做得好”。同理，假嵌入也不能被冒充语义召回能力。
所以每个嵌入器都必须声明自己**到底有没有语义能力**，管线里看到 `semantic=False`
就不得开启精确语义召回、不得在评测里报语义能力的数字。
"""
from __future__ import annotations

import hashlib
import json
import math
import time
import urllib.error
import urllib.request
from typing import Callable, List, Optional, Sequence


class EmbeddingError(RuntimeError):
    """嵌入 API 调用失败。"""


def _normalize(vec: List[float]) -> List[float]:
    """L2 归一：使余弦相似度可以取内积（向量与模长无关）。"""
    norm = math.sqrt(sum(x * x for x in vec))
    if norm <= 0:
        return [0.0] * len(vec)
    return [x / norm for x in vec]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """余弦相似度（两边都已归一时，就是点积）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


# ---------------------------------------------------------------------------
# 云端嵌入 API
# ---------------------------------------------------------------------------
# transport 类型：(url, headers, payload, timeout) -> 响应的 JSON dict。
# 注入这个口的目的是让脚本在**不联网**的情况下也能测请求体与错误处理。
Transport = Callable[[str, dict, dict, int], dict]


def _urllib_transport(url: str, headers: dict, payload: dict, timeout: int) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_error(exc: "urllib.error.HTTPError") -> EmbeddingError:
    """HTTP 4xx/5xx = **业务错误**（鉴权失败、参数错、模型名错），不是网络故障。

    为什么必须区分：`urllib.error.HTTPError` 是 `URLError` 的子类，
    如果一律当网络错误处理，会把 401 重试三次（每次都计费）
    并且报错会说“调用失败”而不是“鉴权失败”——排查时会被引导到错的方向。
    """
    try:
        body = exc.read().decode("utf-8", "replace")[:300]
    except Exception:                      # pragma: no cover - 读体失败不能盖掉原错误
        body = ""
    return EmbeddingError(f"嵌入 API 返回 HTTP {exc.code}：{body}")


class RemoteEmbedder:
    """云端嵌入客户端（MiniMax 主流，也支持 OpenAI 兼容形态）。

    为什么走云端而不是本地 BGE-M3
    --------------------------
    本机没有可用 GPU（Intel Iris Xe 共享显存 128MB）、16GB 内存，BGE-M3 的 CPU 推理会把检索拖成主功能的阻塞项；
    而且 Docker Hub 拉不到模型镜像。PRD 13.4.5 选型轴③允许替换嵌入实现（接口不变），
    这里就是那个接口后面的可替换实现。**模型口径与 PRD 写的 BGE-M3 不同，要在评测报告里写清。**

    MiniMax 接口要点
    ------------------
    - 地址：`{base_url}/v1/embeddings`（国内 `api.minimaxi.com`，国际 `api.minimax.chat`）
    - 鉴权：`Authorization: Bearer <key>`（**失败时 HTTP 仍是 200**，真正的错误在 `base_resp.status_code` 里）
    - 请求体：`{"model": "embo-01", "texts": [...], "type": "db"|"query"}`
    - 响应：**实测为** `{"base_resp": {...}, "total_tokens": n, "vectors": [[...], ...]}`
      （**不是** OpenAI 那套 `data[].embedding`）—— 解析器两种形态都支持，以免接口变动时静默失败。
    - 维度：`embo-01` 实际输出 **1536 维**
    """

    name = "remote"
    semantic = True

    def __init__(self, base_url: str, api_key: str, model: str = "embo-01",
                 dim: int = 1536, timeout_s: int = 30, retries: int = 2,
                 batch_size: int = 16, provider: str = "minimax",
                 group_id: str = "", transport: Optional[Transport] = None) -> None:
        if not base_url:
            raise ValueError("RemoteEmbedder 需要 base_url")
        if not api_key:
            raise ValueError("RemoteEmbedder 需要 api_key")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.dim = dim
        self.timeout_s = timeout_s
        self.retries = max(0, retries)
        self.batch_size = max(1, batch_size)
        self.provider = provider
        self.group_id = group_id
        self._transport = transport or _urllib_transport
        self.calls = 0          # 调用计数，用于计量与成本监控

    # ---- 请求组装 ----
    def _url(self) -> str:
        url = f"{self.base_url}/v1/embeddings"
        if self.provider == "minimax" and self.group_id:
            url = f"{url}?GroupId={self.group_id}"
        return url

    def _payload(self, texts: List[str], kind: str) -> dict:
        if self.provider == "openai":
            return {"model": self.model, "input": texts}
        return {"model": self.model, "texts": texts, "type": "db" if kind == "doc" else "query"}

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    # ---- 响应解析 ----
    @staticmethod
    def _parse(resp: dict) -> List[List[float]]:
        """容忍多种返回形态，但**不容忍错误被当成结果**。"""
        if not isinstance(resp, dict):
            raise EmbeddingError(f"嵌入 API 返回不是 JSON 对象：{str(resp)[:120]}")
        base = resp.get("base_resp") or {}
        code = base.get("status_code")
        if code not in (None, 0):
            raise EmbeddingError(f"嵌入 API 报错：{code} {base.get('status_msg', '')}")
        data = resp.get("data")
        vectors: List[List[float]] = []
        if isinstance(data, list) and data:
            for item in data:
                vec = item.get("embedding") if isinstance(item, dict) else None
                if isinstance(vec, list):
                    vectors.append([float(x) for x in vec])
        elif isinstance(resp.get("embeddings"), list):
            vectors = [[float(x) for x in v] for v in resp["embeddings"]]
        elif isinstance(resp.get("vectors"), list):
            vectors = [[float(x) for x in v] for v in resp["vectors"]]
        if not vectors:
            raise EmbeddingError(f"嵌入 API 响应里没有向量：{str(resp)[:160]}")
        return vectors

    def _post(self, texts: List[str], kind: str) -> List[List[float]]:
        url = self._url()
        payload = self._payload(texts, kind)
        headers = self._headers()
        last_err: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                self.calls += 1
                resp = self._transport(url, headers, payload, self.timeout_s)
                return self._parse(resp)
            except EmbeddingError:
                raise                       # 业务错误（如 key 无效）不重试
            except urllib.error.HTTPError as exc:
                raise _http_error(exc)       # HTTP 4xx/5xx = 业务错误，不重试
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_err = exc
                if attempt < self.retries:
                    time.sleep(0.4 * (attempt + 1))
        raise EmbeddingError(f"嵌入 API 调用失败（已重试 {self.retries} 次）：{last_err}")

    # ---- 对外接口 ----
    def embed(self, texts: List[str]) -> List[List[float]]:
        """批量嵌入文本（文档类，type=db）。"""
        if not texts:
            return []
        out: List[List[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i:i + self.batch_size]
            vecs = self._post(batch, "doc")
            if len(vecs) != len(batch):
                raise EmbeddingError(f"嵌入 API 返回数量不匹配：请求 {len(batch)} 条，返回 {len(vecs)} 条")
            for v in vecs:
                if len(v) != self.dim:
                    raise EmbeddingError(f"嵌入维度与预期不符：预期 {self.dim}，实际 {len(v)}")
                out.append(_normalize(v))
        return out

    def embed_query(self, text: str) -> List[float]:
        """单条查询嵌入（ctype=query）。"""
        vecs = self._post([text], "query")
        if not vecs:
            raise EmbeddingError("嵌入 API 没有返回查询向量")
        # 查询侧与文档侧同口径校验维度（R15-06）：文档侧错会报错，
        # 查询侧错了只是静默地与所有向量点积为 0（“召不到”）。
        if len(vecs[0]) != self.dim:
            raise EmbeddingError(f"嵌入维度与预期不符：预期 {self.dim}，实际 {len(vecs[0])}")
        return _normalize(vecs[0])


# ---------------------------------------------------------------------------
# 离线确定性嵌入（测试与接线用）
# ---------------------------------------------------------------------------
class HashEmbedder:
    """确定性的字符 n-gram 哈希嵌入。

    **不具备语义能力**（`semantic = False`）：它只能把相同的字符片段映射到相近的桶里，
    对“同义改写”、“跨语种同义”一概无助。用途：
      - 无云端 key 时让向量通道一路能跑通（接线、表格、索引的测试）；
      - 单元测试里提供稳定可预期的向量。
    """

    name = "hash"
    semantic = False

    def __init__(self, dim: int = 256, ngram: int = 3) -> None:
        if dim <= 0:
            raise ValueError("dim 必须为正整数")
        self.dim = dim
        self.ngram = ngram

    def embed(self, texts: List[str]) -> List[List[float]]:
        out = []
        for t in texts:
            from .lexical import normalize_text
            s = normalize_text(t).replace(" ", "")
            vec = [0.0] * self.dim
            if s:
                grams = [s[i:i + self.ngram] for i in range(max(0, len(s) - self.ngram + 1))]
                for g in grams:
                    h = hashlib.blake2b(g.encode("utf-8"), digest_size=8).digest()
                    idx = int.from_bytes(h[:4], "big") % self.dim
                    sign = 1.0 if h[4] % 2 == 0 else -1.0
                    vec[idx] += sign
            out.append(_normalize(vec))
        return out

    def embed_query(self, text: str) -> List[float]:
        return self.embed([text])[0]


class NullEmbedder:
    """占位实现：固定维度的字符 n-gram 散列。

    **不具备语义能力**（`semantic = False`）。R15-05：旧版本词表随批次变动（单条 3 维 / 两条 6 维），
    且不归一 —— 向量不可比，“余弦相似度”也不成立。
    """

    name = "null-embedder"
    semantic = False
    dim = 256

    def embed(self, texts: List[str]) -> List[List[float]]:
        from .lexical import char_ngrams
        out: List[List[float]] = []
        for t in texts:
            # 每条文本一个独立的累加器（R17-04）：旧写法把 vec 放在循环外，
            # 所有文本积加到同一个器里 → 每条都返回同一条向量，余弦恒为 1，检索无从排序。
            vec = [0.0] * self.dim
            for g in char_ngrams(t):
                h = hashlib.blake2b(g.encode("utf-8"), digest_size=8).digest()
                idx = int.from_bytes(h[:4], "big") % self.dim
                sign = 1.0 if h[4] % 2 == 0 else -1.0
                vec[idx] += sign
            out.append(_normalize(vec))
        return out

    def embed_query(self, text: str) -> List[float]:
        return self.embed([text])[0]


class OpenAICompatibleEmbedder:
    """OpenAI 兼容形态的嵌入客户端（阿里云百炼 / 智谱 / 火山方舟等）。

    与 MiniMax 的自定义形态（texts + type）不同，这里用 OpenAI 的 `{"model", "input": [...]}` 与 `data[].embedding`。
    """

    name = "openai-compatible"
    semantic = True

    def __init__(self, provider=None, api_key: str = "", base_url: str = "", model: str = "",
                 dim: int = 1024, timeout_s: int = 30, retries: int = 2,
                 batch_size: int = 16, transport=None) -> None:
        self.provider = provider
        self.api_key = api_key
        self.base_url = (base_url or (provider.embedding_base_url if provider else "")).rstrip("/")
        self.model = model or (provider.embedding_model if provider else "")
        self.dim = dim
        self.timeout_s = timeout_s
        self.retries = max(0, retries)
        self.batch_size = max(1, batch_size)
        self._transport = transport or _urllib_transport
        self.calls = 0

    def _url(self) -> str:
        path = self.provider.embedding_path if self.provider else "/v1/embeddings"
        return f"{self.base_url}{path}"

    def _payload(self, texts: List[str]) -> dict:
        return {"model": self.model, "input": texts}

    def _parse(self, resp: dict) -> List[List[float]]:
        if not isinstance(resp, dict):
            raise EmbeddingError(f"嵌入 API 返回不是 JSON 对象：{str(resp)[:120]}")
        base = resp.get("base_resp") or {}
        code = base.get("status_code")
        if code not in (None, 0):
            raise EmbeddingError(f"嵌入 API 报错：{code} {base.get('status_msg', '')}")
        data = resp.get("data")
        if not isinstance(data, list) or not data:
            raise EmbeddingError(f"嵌入 API 响应里没有向量：{str(resp)[:160]}")
        out = []
        for item in data:
            vec = item.get("embedding") if isinstance(item, dict) else None
            if not isinstance(vec, list):
                raise EmbeddingError(f"嵌入 API 向量格式不对：{str(item)[:120]}")
            out.append([float(x) for x in vec])
        return out

    def _post(self, texts: List[str]) -> List[List[float]]:
        url = self._url()
        payload = self._payload(texts)
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        last: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                self.calls += 1
                return self._parse(self._transport(url, headers, payload, self.timeout_s))
            except EmbeddingError:
                raise
            except urllib.error.HTTPError as exc:
                raise _http_error(exc)       # HTTP 4xx/5xx = 业务错误，不重试
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = exc
                if attempt < self.retries:
                    time.sleep(0.4 * (attempt + 1))
        raise EmbeddingError(f"嵌入 API 调用失败（已重试 {self.retries} 次）：{last}")

    def embed(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        out: List[List[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i:i + self.batch_size]
            vecs = self._post(batch)
            if len(vecs) != len(batch):
                raise EmbeddingError(f"嵌入数量不匹配：请求 {len(batch)} 条，返回 {len(vecs)} 条")
            for v in vecs:
                if len(v) != self.dim:
                    raise EmbeddingError(f"嵌入维度与预期不符：预期 {self.dim}，实际 {len(v)}")
                out.append(_normalize(v))
        return out

    def embed_query(self, text: str) -> List[float]:
        vecs = self._post([text])
        if not vecs:
            raise EmbeddingError("嵌入 API 没有返回查询向量")
        if len(vecs[0]) != self.dim:
            raise EmbeddingError(f"嵌入维度与预期不符：预期 {self.dim}，实际 {len(vecs[0])}")
        return _normalize(vecs[0])
