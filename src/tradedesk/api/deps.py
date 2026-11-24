# -*- coding: utf-8 -*-
"""FastAPI 依赖：存储、注册表、调度器，以及**闸门令牌校验**。

闸门令牌走请求头 `X-Gate-Token`；缺失或无效 → 403，且不产生任何对外动作。
"""
from __future__ import annotations

import threading
from typing import Dict, Optional

from fastapi import Header, HTTPException, Request

from ..agents.inquiry import InquiryAgent
from ..agents.draft_writer import ModelDraftWriter
from ..data.kb_seed import KB_VERSION, load_kb
from ..agents.registry import MERCH_SPEC, GROWTH_SPEC, Registry
from ..agents.sentinel import SentinelAgent
from ..config import Config, get_settings
from ..runtime.gates import GateTokenService
from ..retrieval.store_retriever import StoreRetriever
from ..retrieval.indexer import load_index
from ..release.manifest import baseline_components
from ..release.service import ReleaseService
from ..runtime.supervisor import Supervisor
from ..store.base import StateStore
from ..store.sqlite_store import SqliteStateStore


def build_registry(retriever=None, chat=None, retriever_factory=None) -> Registry:
    """注册本期两个实现型角色 + 两个仅声明型角色。

    `chat` 为云端对话客户端（S5-1a）：为 None 时 Eva 只走模板路径（离线可测）；
    有了它才走真模型出稿。**两条路径都不自动发送**（写操作与发送分离）。
    """
    reg = Registry()
    reg.register(InquiryAgent(retriever=retriever,
                              writer=ModelDraftWriter(chat) if chat is not None else None,
                              retriever_factory=retriever_factory))
    reg.register(SentinelAgent())
    reg.register_spec(MERCH_SPEC)
    reg.register_spec(GROWTH_SPEC)
    return reg


class AppContext:
    """把依赖装到一个对象里，测试时方便替换。"""

    def _build_chat(self):
        """按配置建云端对话客户端（S5-1a）；没配厂商或没 key 就返回 None。

        **还要求 `TRADEDESK_LLM_DRAFT_ENABLED=1`**（默认关）——否则 Eva 只走模板路径。
        为什么要一个开关：开启后每条询盘都会发起一次**付费**的云端调用，
        且草稿从“确定性模板”变成“模型写的”——审核断言与单测都会随机器人改变，
        也会悄悄产生费用。默认关是安全侧，上线时显式打开。
        """
        if not getattr(self.config, "llm_draft_enabled", False):
            return None
        from ..llm.factory import build_chat_client
        return build_chat_client(self.config)

    def _build_embedder(self):
        """按配置建嵌入器；未开启或没有 key 就返回 None。"""
        cfg = self.config
        if not getattr(cfg, "embedder_enabled", False):
            return None
        # key 的判定交给工厂：厂商各自的环境变量名不同，
        # 在这里写死 "cfg.llm_api_key" 会把“配了 MiniMax 但用阿里云”的情况误判成“没 key”。
        from ..llm.factory import build_embedder
        return build_embedder(cfg)

    def __init__(self, store: StateStore, registry: Optional[Registry] = None,
                 config: Optional[Config] = None, chat=None) -> None:
        self.store = store
        self.config = config or get_settings()
        # 知识库：库里没有或版本对不上时才重新灌种子（生产应走版本化发布，见 PRD 13.4.8）
        self.kb_version = self.config.kb_version or KB_VERSION
        try:
            existing = self.store.list_kb_chunks(self.kb_version)
            if not existing:
                load_kb(self.store, self.kb_version)
        except Exception as exc:
            # 灌种子失败不阻断启动：检索层降级为"空库"，Agent 走低置信兜底
            print(f"[deps] 知识库种子灌入失败（检索将返回空结果）：{exc}")
        # 向量通道（S4）：只在显式开启且拿得到 key 时才建，
        # 否则启动会去调云端 API（计费）；索引失败也不阻断启动 ——
        # 检索层降级为词法 + 结构化双通道，且不得对外宣称有语义召回能力。
        self.embedder = self._build_embedder()
        self.vector_index = None
        # 启动时**只读**现成的向量索引，不自动建。
        # 建索引是**显式动作**（scripts/index_kb.py 或 dev.ps1 index）：
        #   - 为什么不在启动时建？每个进程启动都去调云端 API（计费），
        #     而且审核脚本每次都新建一个空库 → 每次都要重新嵌入全部块；
        #   - 生产形态下索引应该是发布流水线的一步（PRD 13.4.8），而不是每次启动。
        try:
            self.vector_index = load_index(self.store, self.kb_version)
            if len(self.vector_index):
                print(f"[deps] 向量索引已加载：{len(self.vector_index)} 条"
                      f"（需重建时跑 dev.ps1 index）")
            elif self.store.list_kb_chunks(self.kb_version):
                # “有块但没有向量”不能静默（R15-04）：否则之后会被误判成“模型不行”
                print("[deps] 知识库有块但**向量索引为空**（尚未建索引，或维度变了）："
                      "检索将降级为词法+结构化双通道；建索引跑 dev.ps1 index")
        except Exception as exc:
            print(f"[deps] 向量索引加载失败（检索降级为词法+结构化双通道）：{exc}")
        # 检索器在嵌入器与向量索引就绪后创建：稠密通道才能接上去（S4-4b）。
        self.retriever = StoreRetriever(self.store, self.kb_version,
                                        embedder=self.embedder, vector_index=self.vector_index)
        # ---- 灰度检索器工厂（S6-3）----
        # 同一进程内不同企业可能命中不同的知识库版本 → 检索器**按版本缓存**，不共用一份。
        # 向量索引按版本加载且**只读**（load_index 不建索引）——建索引是显式动作（dev.ps1 index）。
        self._retriever_cache: Dict[str, StoreRetriever] = {self.kb_version: self.retriever}
        self._retriever_lock = threading.Lock()

        def _retriever_factory(kb_version: str) -> StoreRetriever:
            key = str(kb_version or "")
            if not key:
                return self.retriever
            with self._retriever_lock:
                cached = self._retriever_cache.get(key)
            if cached is not None:
                return cached
            index = None
            try:
                index = load_index(self.store, key)
            except Exception:
                index = None
            built = StoreRetriever(self.store, key, embedder=self.embedder, vector_index=index)
            with self._retriever_lock:
                return self._retriever_cache.setdefault(key, built)

        self.retriever_factory = _retriever_factory

        # ---- 发布/灰度服务（S6-3；PRD 13.4.8）----
        # 基线 = 本机正在跑的这一套（镜像 / AgentSpec / 提示词 / 知识库 / 名单）。
        # 没有任何发布记录时 resolve() 返回 baseline → 行为与引入灰度之前完全一致。
        self.release_defaults = baseline_components(self.kb_version, self.config.list_version)
        self.release_service = ReleaseService(self.store, self.release_defaults)
        # 云端对话客户端（S5-1a）：没 key 时为 None → Eva 自动退回模板路径。
        # `chat` 参数用于测试注入假客户端（不联网、不计费）。
        self.chat = chat if chat is not None else self._build_chat()
        if self.chat is not None:
            print(f"[deps] 云端模型已接入：{self.chat.provider.name} / {self.chat.model}"
                  "（Eva 将走模型出稿路径；草稿仍需人工签核）")
        elif not getattr(self.config, "llm_draft_enabled", False):
            print("[deps] 模型出稿未开启（TRADEDESK_LLM_DRAFT_ENABLED=0）：Eva 只走模板路径")
        else:
            print("[deps] 未配云端模型 key（或厂商未配置）：Eva 只走模板路径")
        self.registry = registry or build_registry(retriever=self.retriever, chat=self.chat,
                                                   retriever_factory=self.retriever_factory)
        self.tokens = GateTokenService(store, ttl_seconds=self.config.gate_token_ttl_s)
        self.supervisor = Supervisor(
            store=store, registry=self.registry, token_service=self.tokens,
            low_confidence_threshold=self.config.low_confidence_threshold,
            amount_threshold=self.config.amount_threshold_usd,
            # SLA 按业务类型可配置（FR-ORC-08）：首响 / 报价 / 跟进
            sla_first_response=self.config.sla_first_response_s,
            sla_quote=self.config.sla_quote_s,
            sla_followup=self.config.sla_followup_s,
            # 灰度分流（S6-3）：没有发布记录时行为不变
            release_service=self.release_service,
        )


def offline_config(cfg: Optional[Config] = None) -> Config:
    """返回一份**强制关掉云端对话**的配置副本（审核 / 验证脚本用）。

    为什么需要：模型出稿路径由 `TRADEDESK_LLM_DRAFT_ENABLED` 控制，而这个开关在 .env 里。
    如果审核脚本直接用环境配置，那么本机一旦开了开关，审核断言就会随模型输出变动
    （且会真的产生费用）——审核必须是**可重复**的，所以在这里强制关掉。
    只关对话出稿，**不动嵌入**（audit_s4 需要嵌入通道，否则它自己会开它）。
    """
    import copy
    c = copy.deepcopy(cfg) if cfg is not None else copy.deepcopy(get_settings())
    c.llm_draft_enabled = False
    return c


def get_context() -> AppContext:
    """默认上下文。

    **配了 `TRADEDESK_DATABASE_URL` 就用 PostgreSQL**（S6-2 的容器形态），否则用 SQLite（本地零依赖）。
    为什么要这个分支：原来 compose 里的 db 容器在 compose 形态下**没人用**（
    应用一直跑 SQLite）——那么“容器化部署”只做了一半，数据库容器是永远的。
    """
    cfg = get_settings()
    if cfg.database_url:
        from ..store.postgres_store import PostgresStateStore
        store = PostgresStateStore(cfg.database_url)
        print("[deps] 使用 PostgreSQL：" + cfg.database_url.split("@")[-1])
    else:
        store = SqliteStateStore(cfg.db_path)
    return AppContext(store=store, config=cfg)


def make_gate_token_guard(ctx: "AppContext"):
    """闸门令牌守卫工厂。

    没有令牌直接 403（FR-ORC-09 / NFR-SEC-04），**并且必须留痕** ——
    不带令牌的越权尝试恰恰是最该留痕的一类（PRD 11.4「越权尝试」）。
    """

    def require_gate_token(
        x_gate_token: Optional[str] = Header(default=None),
        request: Request = None,  # type: ignore[assignment]
    ) -> str:
        # 路径参数在路由匹配后才有值，从 request 里取（Path 参数不允许默认值）
        thread_id = ""
        try:
            thread_id = request.path_params.get("thread_id", "") if request is not None else ""
        except Exception:
            thread_id = ""
        if not x_gate_token or not x_gate_token.strip():
            try:
                ctx.store.append_audit("system", "write.rejected", thread_id,
                                       {"reason": "缺少闸门令牌", "path_action": "write"})
            except Exception:
                pass
            raise HTTPException(
                status_code=403,
                detail={"code": "gate_token_required",
                        "message": "写操作必须携带闸门令牌（请求头 X-Gate-Token）"},
            )
        return x_gate_token.strip()

    return require_gate_token