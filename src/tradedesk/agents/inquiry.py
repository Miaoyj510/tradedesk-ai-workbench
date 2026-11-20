# -*- coding: utf-8 -*-
"""Inquiry —— 询盘应答 Agent（PRD 7.3 / FR-INQ-01~12）。

边界（写在 AgentSpec 里，可被校验）：
  只做：意图识别（询盘五类）、产品匹配、回复草稿生成、引用装配、跟进建议
  不做：报价决策、合同、投放、任何写操作

本期实现说明
  模型层是**确定性模板 + 证据匹配**，不是真模型：
    - 真实 LLM 生成在 S5 接入（MiniMax，能力已抽象为 LLMProvider）
    - 真实向量检索在 S4 接入（当前证据来自 data/seed.py 的演示知识快照）
  这样 S2 阶段可以完全离线、可重复验证，规则与证据装配逻辑也不会被推迟到 LLM 之后。
"""
from __future__ import annotations

import re
from datetime import date
from typing import Dict, List, Optional, Tuple

from ..retrieval.base import SearchQuery
from ..domain.agentspec import AgentSpec, AgentStatus, ToolMode, ToolSpec
from ..domain.enums import Intent, Language
from ..domain.models import Citation, Draft
from .base import AgentRequest, AgentResult, AgentStatus as ResultStatus, BaseAgent

# 询盘五类的多语关键词（对应 PRD 9.6 的重映射结果）
INTENT_KEYWORDS: Dict[Intent, List[str]] = {
    Intent.PRICE: ["price", "precio", "prezzo", "quote", "cotizar", "cost",
                   "价格", "报价", "单价", "سعر", "ثمن"],
    Intent.SAMPLE: ["sample", "muestra", "échantillon", "样品", "打样",
                    "عينة", "نموذج"],
    Intent.SPEC: ["material", "specification", "spec", "材质", "规格", "载荷",
                  "capacidad", "مواصفات", "خامة"],
    Intent.LOGISTICS: ["shipping", "freight", "logistics", "port", "incoterm",
                       "运费", "物流", "港口", "شحن", "ميناء"],
    Intent.COMPLAINT: ["complaint", "problem", "damaged", "索赔", "投诉", "破损",
                      "queja", "reclamación", "شكوى"],
}

SKU_PATTERN = re.compile(r"\b([XM]-\d{2,4})\b", re.I)

# 三语草稿模板（保持业务口径一致：参考价、交期、认证都来自知识快照）
TEMPLATES = {
    Language.EN: (
        "Hi,\n\n"
        "Thank you for your enquiry about {sku}. Our reference price for {moq} pcs is "
        "USD {price_low}–{price_high} per unit {terms} {port}, with a lead time of {lead_time} days after order confirmation.\n\n"
        "{cert_line}\n\n"
        "Please confirm {ask_line} so that we can prepare the full quotation."
    ),
    Language.ES: (
        "Hola:\n\n"
        "Gracias por su consulta sobre {sku}. Nuestro precio de referencia para {moq} unidades es de "
        "USD {price_low}–{price_high} por unidad {terms} {port}, con un plazo de entrega de {lead_time} días tras la confirmación del pedido.\n\n"
        "{cert_line}\n\n"
        "Confírmenos {ask_line} para preparar la cotización completa."
    ),
    Language.AR: (
        "مرحبا،\n\n"
        "شكرا لاستفسارك عن {sku}. سعرنا المرجعي لـ {moq} قطعة هو "
        "USD {price_low}–{price_high} للوحدة {terms} {port}، ومدة التسليم {lead_time} يومًا بعد تأكيد الطلب.\n\n"
        "{cert_line}\n\n"
        "نرجو تأكيد {ask_line} حتى نتمكن من إعداد عرض السعر الكامل."
    ),
    Language.ZH: (
        "您好：\n\n"
        "关于 {sku} 的询价：{moq} 台起订，参考价 USD {price_low}–{price_high}/台，{terms} {port}，"
        "交期 {lead_time} 天（订单确认后）。\n\n"
        "{cert_line}\n\n"
        "请确认 {ask_line}，我们据此出具完整报价。"
    ),
}

CERT_LINE = {
    Language.EN: "The model is covered by certificate {cert_no} ({cert_type}), valid until {valid_until}.",
    Language.ES: "El modelo está cubierto por el certificado {cert_no} ({cert_type}), válido hasta {valid_until}.",
    Language.AR: "الطراز مشمول بالشهادة {cert_no} ({cert_type})، سارية حتى {valid_until}.",
    Language.ZH: "该型号由证书 {cert_no}（{cert_type}）覆盖，有效期至 {valid_until}。",
}

ASK_LINE = {
    Language.EN: "the destination port, quantity and the Incoterm you prefer",
    Language.ES: "el puerto de destino, la cantidad y el Incoterm que prefiere",
    Language.AR: "ميناء التسليم والكمية وشروط الشحن المطلوبة",
    Language.ZH: "目的港、数量与贸易术语",
}

INQUIRY_SPEC = AgentSpec(
    agent_id="inquiry.reply",
    name="询盘应答 Agent",
    display_name="Inquiry",
    owner_role="外贸业务员",
    status=AgentStatus.ACTIVE,
    version="1.0.0",
    scope_included=["意图识别（询盘五类）", "产品匹配", "回复草稿生成", "引用装配", "跟进建议"],
    scope_excluded=["报价决策", "合同", "投放", "任何写操作", "自动发送"],
    inputs=["thread", "message", "language", "context"],
    outputs=["draft", "intent_labels", "confidence", "citations", "next_action"],
    guards=["低置信输出「需人工确认」", "命中名单不生成任何回复", "消息歧义发起追问而不是猜测"],
    gates=["GATE-01", "GATE-02", "GATE-05", "GATE-06", "GATE-08"],
    tools=[
        ToolSpec(name="kb.search", mode=ToolMode.READ_ONLY),
        ToolSpec(name="kb.get_product", mode=ToolMode.READ_ONLY),
        ToolSpec(name="kb.get_certificate", mode=ToolMode.READ_ONLY),
        ToolSpec(name="crm.read_profile", mode=ToolMode.READ_ONLY),
    ],
    sla={"first_response_seconds": 300},
    observability=["trace_id", "thread_id", "agent_id", "citations", "confidence"],
)


def classify_intent(message: str) -> List[Intent]:
    """意图识别：多语关键词命中，可多标签。S5 接 LLM 后替换此函数。"""
    text = (message or "").lower()
    hits = []
    for intent, words in INTENT_KEYWORDS.items():
        if any(w.lower() in text for w in words):
            hits.append(intent)
    if not hits:
        hits.append(Intent.SPEC)   # 没有明确意图词时按"规格询问"兜底（保守：触发追问）
    return hits


class InquiryAgent(BaseAgent):
    """询盘应答：意图识别 + 检索取证 + 草稿装配（确定性模板，S5 换真实模型）。

    取证优先走**检索管线**（S3）：产品、证书、法规都来自知识库块，
    价格/交期/认证从命中块的结构化字段取 —— 草稿不再写死常量。
    没有检索器时退回旧的本地匹配（仅兜底，便于单测）。
    """

    def __init__(self, retriever=None) -> None:
        super().__init__(INQUIRY_SPEC)
        self.retriever = retriever

    # ---- 证据装配 ----
    # R11-05 / R11-06：**不再有"写死常量"这条兜底路径**。
    # 知识库是价格、规格、证书的唯一真源：检索没确认产品就只发追问，宁可少答一句，
    # 也不猜型号与报价。旧兜底会报出 S1 时代的 "SKU X-200 v1" —— 那个标识在知识库里
    # 查不到，等于给买家一条断链的引用。

    # ---- 检索取证（S3）----
    def _retrieve(self, request: AgentRequest):
        """返回 (产品块, 命中列表)；检索器不可用时返回 (None, [])。"""
        if self.retriever is None:
            return None, []
        profile = request.context.get("customer") or {}
        query = SearchQuery(
            text=request.message,
            lang=request.language.value,
            buyer_country=str(profile.get("country", "")),
            today=date.today(),
            top_k=8,
        )
        hits = self.retriever.search(query)
        # 只有**结构化通道**（型号/证书号等精确命中）确认的产品才能进草稿。
        # 词法通道命中的产品只说明"这类产品可能相关"，不足以据此报具体型号的价格与交期。
        product = next((h.chunk for h in hits
                        if h.chunk.doc_type == "产品条目" and "structured" in h.channels), None)
        return product, hits

    # ---- 主流程 ----
    def handle(self, request: AgentRequest) -> AgentResult:
        message = request.message
        lang = request.language
        intents = classify_intent(message)
        product_chunk, hits = self._retrieve(request)
        if product_chunk is not None:
            # 知识库块：结构化字段在 extra，标识（型号/HS）在 identifiers
            info = dict(product_chunk.extra)
            info["sku"] = product_chunk.identifiers.get("sku", product_chunk.doc_id)
            info.setdefault("certificates", [])
            info.setdefault("name", product_chunk.title)
            products = [info]
            citations = self._citations_from_hits(
                hits, product=product_chunk,
                buyer_country=str((request.context.get("customer") or {}).get("country", "")))
        else:
            # 检索没确认产品 → 只发追问（R11-05：不得回退到写死常量报型号与价格）
            products = []
            citations = []

        # 置信度：命中产品 + 证据越多越可信；无证据 → 低置信（走 GATE-06）
        confidence = 0.35 + (0.25 if products else 0.0) + min(0.25, 0.05 * len(citations))
        needs_human = confidence < 0.55

        if not products:
            body = self._clarify_body(lang, intents)
            draft = Draft(thread_id=request.thread.thread_id, lang=lang, body=body,
                          intent_labels=intents, confidence=confidence,
                          citations=[], needs_human_confirmation=True,
                          next_action="信息不足：只发追问，不猜测规格与价格")
            return AgentResult(
                agent_id=self.agent_id, status=ResultStatus.NEED_HUMAN, draft=draft,
                intent_labels=intents, confidence=confidence,
                notes=["未匹配到产品，按澄清处理"], next_action=draft.next_action,
            )

        p = products[0]
        # 证书行：只认**未过期**的证书（过期证书不得出现在对外内容里）
        cert_line = "—"
        expired_notes = []
        # 证书只从检索命中里取（不再有写死清单）
        cert_candidates = [h.chunk for h in hits if h.chunk.doc_type == "证书"]
        for c in cert_candidates:
            cert_no = c.identifiers.get("cert_no", c.doc_id)
            ctype = c.extra.get("cert_type", "证书")
            valid_until = c.valid_until.date().isoformat() if c.valid_until else "—"
            if isinstance(valid_until, str) and valid_until < date.today().isoformat():
                expired_notes.append(f"{cert_no} 已于 {valid_until} 过期，不予引用")
                continue
            cert_line = CERT_LINE[lang].format(cert_no=cert_no, cert_type=ctype, valid_until=valid_until)
            break
        body = TEMPLATES[lang].format(
            sku=p["sku"], moq=p["moq"], price_low=p["price_low"], price_high=p["price_high"],
            terms=p["terms"], port=p["port"], lead_time=p["lead_time_days"],
            cert_line=cert_line, ask_line=ASK_LINE[lang],
        )
        draft = Draft(
            thread_id=request.thread.thread_id, lang=lang, body=body,
            intent_labels=intents, confidence=round(confidence, 2), citations=citations,
            needs_human_confirmation=needs_human,
            next_action=("置信度不足，请人工确认后再发送" if needs_human
                         else "价格与交期需人工签核后发送"),
            model="stub-template",
        )
        return AgentResult(
            agent_id=self.agent_id,
            status=ResultStatus.NEED_HUMAN if needs_human else ResultStatus.OK,
            draft=draft, intent_labels=intents, confidence=round(confidence, 2),
            evidence=[c.model_dump(mode="json") for c in citations],
            next_action=draft.next_action,
        )

    def _citations_from_hits(self, hits, product=None, buyer_country: str = "") -> List[Citation]:
        """引用来自检索命中：每条都必须带可回指标识（PRD 9.3 / FR-KB-07）。

        只保留**与本单相关**的块：命中的产品、覆盖该型号的证书、适用于买家国别或该型号
        HS 编码的法规、以及本次命中的术语表。过期块与"尚未生效"的条款不进引用
        （后者可见但不作为当前要求输出）。
        """
        cites: List[Citation] = []
        sku = (product.identifiers.get("sku") if product is not None else "") or ""
        hs = set(product.hs_codes) if product is not None else set()
        country = (buyer_country or "").upper()
        for h in hits:
            c = h.chunk
            if h.expired or h.not_yet_effective:
                continue
            if c.doc_type == "证书":
                covers = [str(x).upper() for x in (c.extra.get("covers") or [])]
                if sku and covers and sku.upper() not in covers:
                    continue        # 与本型号无关的证书不引用
            if c.doc_type == "法规条款":
                # 国别未知 → 一条**国别专属**条款都不能进对外内容（R11-03）：
                # 一封报价同时承诺欧盟/埃及/巴西三国的合规要求，是对买家的失真陈述。
                if not country:
                    if c.countries:
                        continue
                elif c.countries and country not in [x.upper() for x in c.countries]:
                    continue        # 不适用于买家国别的法规不引用
                if hs and c.hs_codes and not (hs & set(c.hs_codes)):
                    continue
            if c.doc_type == "术语表":
                pass
            if c.doc_type == "产品条目":
                # 只引用**本次选中的**那个产品，别把其它型号也写进对外内容
                if product is not None and c.chunk_id != product.chunk_id:
                    continue
                cites.append(Citation(citation_id=c.identifiers.get("sku", c.doc_id), type="产品条目",
                                      note=f"{c.title} · 库版本 {c.kb_version}"))
            elif c.doc_type == "证书":
                cites.append(Citation(citation_id=c.identifiers.get("cert_no", c.doc_id), type="证书",
                                      note=f"有效期至 {c.valid_until.date() if c.valid_until else '—'}"
                                           f" · 库版本 {c.kb_version}"))
            elif c.doc_type == "法规条款":
                cites.append(Citation(citation_id=c.identifiers.get("clause_no", c.doc_id), type="法规条款",
                                      note=f"生效 {c.effective_from.date() if c.effective_from else '—'}"
                                           f" · 库版本 {c.kb_version}"))
            elif c.doc_type == "术语表":
                cites.append(Citation(citation_id=c.identifiers.get("term_id", c.doc_id), type="术语表",
                                      note=f"{c.title} · 库版本 {c.kb_version}"))
        return cites

    @staticmethod
    def _clarify_body(lang: Language, intents: List[Intent]) -> str:
        if lang == Language.ES:
            return ("Hola:\n\nPara cotizar con precisión, ¿podría confirmarnos el modelo / SKU, "
                    "la cantidad, el puerto de destino y el Incoterm que prefiere?")
        if lang == Language.AR:
            return ("مرحبا،\n\nلإعداد عرض السعر بدقة، هل يمكنك تأكيد الموديل / SKU والكمية "
                    "وميناء التسليم وشروط الشحن المطلوبة؟")
        if lang == Language.ZH:
            return "您好：\n\n为了准确报价，请确认型号 / SKU、数量、目的港与贸易术语。"
        return ("Hi,\n\nTo quote accurately, could you confirm the model / SKU, the quantity, "
                "the destination port and your preferred Incoterm?")