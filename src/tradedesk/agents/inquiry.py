# -*- coding: utf-8 -*-
"""Inquiry —— 询盘应答 Agent（PRD 7.3 / FR-INQ-01~12）。

边界（写在 AgentSpec 里，可被校验）：
  只做：意图识别（询盘五类）、产品匹配、回复草稿生成、引用装配、跟进建议
  不做：报价决策、合同、投放、任何写操作

本期实现说明
  模型层有**两条路径**（S5-1a 起）：
    - 模板路径（默认）：**确定性模板 + 证据匹配**，全离线可重复验证。
    - 模型路径：真实云端模型写“话术”，而**价格 / 交期 / 数量 / 证书号不给模型看**——
      模型只拿到占位符（例如 {{价格}}这种符号），真值由代码从结构化字段回填。
      模型写出数字 → 判为越权 → 整段丢弃、退回模板草稿。
  真实向量检索在 S4 接入（当前证据来自知识库块）。
  红线：模型只能出**草稿**，报价人工签核、合同必须人工确认；不自动定价、不自动签约、不自动付款。
"""
from __future__ import annotations

import re
from datetime import date
from typing import Dict, List, Optional, Tuple

from ..retrieval.base import SearchHit, SearchQuery
from ..domain.agentspec import AgentSpec, AgentStatus, ToolMode, ToolSpec
from ..domain.enums import Intent, Language
from ..domain.models import Citation, Draft
from .base import AgentRequest, AgentResult, AgentStatus as ResultStatus, BaseAgent
from .draft_writer import DraftRejected, ModelDraftWriter, numbers_of, render_facts

# 询盘五类的多语关键词（对应 PRD 9.6 的重映射结果）
# 词表是**子串匹配**（如 "late" 会命中 "late delivery"），所以词之间的包含关系会影响命中顺序。
# 排序约束：**投诉先于物流**（“交货延迟”属于投诉，不能被 "delivery" 抹走）。
INTENT_KEYWORDS: Dict[Intent, List[str]] = {
    Intent.PRICE: ["price", "precio", "prezzo", "quote", "cotizar", "cost",
                   "价格", "报价", "单价", "سعر", "ثمن"],
    Intent.SAMPLE: ["sample", "muestra", "échantillon", "样品", "打样",
                    "عينة", "نموذج"],
    Intent.SPEC: ["material", "specification", "spec", "材质", "规格", "载荷",
                  "capacidad", "مواصفات", "خامة"],
    Intent.COMPLAINT: ["complaint", "problem", "damaged", "broken", "compensation", "late", "delay",
                       "quality", "unhappy", "索赔", "投诉", "破损", "赔偿",
                       "质量", "延误", "损坏",
                       "queja", "reclamación", "reclamo", "dañado", "roto", "compensación",
                       "calidad", "retraso", "descontentos",
                       "شكوى", "تالف", "معو", "عيب",
                       "تأخير", "عوض"],
    Intent.LOGISTICS: ["shipping", "freight", "logistics", "port", "incoterm", "deliver", "shipment",
                       "运费", "物流", "港口", "交货", "运输",
                       "envío", "flete", "entregar",
                       "شحن", "توصيل", "نقل", "ميناء", "تسليم"],
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

CERT_LINE_SYSTEM = {
    Language.EN: "Our factory quality management system is certified under {cert_no} ({cert_type}), valid until {valid_until}.",
    Language.ES: "Nuestro sistema de gestión de calidad de fábrica está certificado según {cert_no} ({cert_type}), válido hasta {valid_until}.",
    Language.AR: "نظام إدارة الجودة في مصنعنا معتمد بموجب {cert_no} ({cert_type})، صالح حتى {valid_until}.",
    Language.ZH: "工厂质量管理体系通过 {cert_no}（{cert_type}）认证，有效期至 {valid_until}。",
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

    def __init__(self, retriever=None, writer=None) -> None:
        super().__init__(INQUIRY_SPEC)
        self.retriever = retriever
        # 模型出稿执行器（S5-1a）：为 None 则只走模板路径（离线可测）。
        self.writer = writer

    # ---- 证据装配 ----
    # R11-05 / R11-06：**不再有"写死常量"这条兜底路径**。
    # 知识库是价格、规格、证书的唯一真源：检索没确认产品就只发追问，宁可少答一句，
    # 也不猜型号与报价。旧兜底会报出 S1 时代的 "SKU X-200 v1" —— 那个标识在知识库里
    # 查不到，等于给买家一条断链的引用。

    # ---- 检索取证（S3）----
    def _retrieve(self, request: AgentRequest):
        """返回 (产品块, 命中列表, 规格记录)。

        三者的职责：
          产品块  —— 描述性文本版本（四语之一），用于判断"是哪个型号"
          命中列表 —— 证据（证书、法规、术语、产品块）
          规格记录 —— 数值的唯一真源（价格/交期/MOQ/负荷）
        """
        if self.retriever is None:
            return None, [], None
        profile = request.context.get("customer") or {}
        country = str(profile.get("country", "") or "")
        query = SearchQuery(
            text=request.message,
            lang=request.language.value,
            buyer_country=country,
            today=date.today(),
            top_k=8,
        )
        hits = list(self.retriever.search(query))
        # 只有**结构化通道**（型号/证书号等精确命中）确认的产品才能进草稿。
        # 词法通道命中的产品只说明"这类产品可能相关"，不足以据此报具体型号的价格与交期。
        product = self._pick_product(hits, request.language.value)
        if product is None:
            return None, hits, None
        sku = str(product.identifiers.get("sku", ""))
        # 数值只从**结构化规格记录**取（数值的唯一真源）
        spec = self._fetch_spec(sku, country, request.language.value) if sku else None
        # 回查到的块可能已在原始命中里（每家问“CE certificate”时词法就命中了它）
        # —— 直接 append 会让同一张证书在引用列表里出现两次（R14-01），按 chunk_id 去重。
        seen_chunks = {h.chunk.chunk_id for h in hits}

        def _add(chunk) -> None:
            if chunk is None or chunk.chunk_id in seen_chunks:
                return
            seen_chunks.add(chunk.chunk_id)
            hits.append(SearchHit(chunk=chunk, score=1.0, channel="structured",
                                  reasons=[f"结构化命中 {chunk.doc_id}"]))

        # 规格记录也要进 hits：报价里的价格/MOQ/交期就是它的 extra，
        # 它不在引用列表里就没有可回指的来源（R14-02）。
        _add(spec)
        cert_nos = ((spec.extra.get("certificates") if spec is not None else None)
                    or product.extra.get("certificates") or [])
        for cert_no in cert_nos:
            _add(self._fetch_cert(str(cert_no), country, request.language.value))
        return product, hits, spec

    @staticmethod
    def _pick_product(hits, lang: str):
        """挑产品：必须是结构化通道确认的；同型号有多语版本时优先买家语种。"""
        cands = [h.chunk for h in hits
                 if h.chunk.doc_type == "产品条目" and "structured" in h.channels]
        if not cands:
            return None
        for c in cands:
            if c.lang == lang:
                return c
        return cands[0]

    def _fetch_spec(self, sku: str, country: str, lang: str):
        """按型号取结构化规格记录（数值的唯一真源）。"""
        # 按**标识**取规格记录，不需要语义 → 不走稠密通道（节省一次云端调用）
        query = SearchQuery(text=sku, lang=lang, buyer_country=country,
                            today=date.today(), top_k=5, use_dense=False)
        for h in self.retriever.search(query):
            if h.chunk.doc_type == "产品规格" and h.chunk.identifiers.get("sku") == sku:
                return h.chunk
        return None

    def _fetch_cert(self, cert_no: str, country: str, lang: str):
        """按证书号精确回查证书块（证书不按买家国别过滤，FR-KB-06）。"""
        query = SearchQuery(text=cert_no, lang=lang, buyer_country=country,
                            today=date.today(), top_k=3, use_dense=False)
        for h in self.retriever.search(query):
            if h.chunk.doc_type == "证书" and h.chunk.identifiers.get("cert_no") == cert_no:
                return h.chunk
        return None

    # ---- 主流程 ----
    def handle(self, request: AgentRequest) -> AgentResult:
        message = request.message
        lang = request.language
        intents = classify_intent(message)
        product_chunk, hits, spec_chunk = self._retrieve(request)
        if product_chunk is not None:
            # 知识库块：数值在结构化规格记录的 extra，标识（型号/HS）在 identifiers
            info = dict(spec_chunk.extra if spec_chunk is not None else product_chunk.extra)
            info["sku"] = product_chunk.identifiers.get("sku", product_chunk.doc_id)
            info.setdefault("certificates", [])
            info.setdefault("name", product_chunk.title)
            if "price_low" not in info or "lead_time_days" not in info:
                # 找不到结构化规格记录 → 不猜价格，走追问
                info = {}
            products = [info] if info else []
            citations = self._citations_from_hits(
                hits, product=product_chunk,
                buyer_country=str((request.context.get("customer") or {}).get("country", "")))
        else:
            # 检索没确认产品 → 只发追问（R11-05：不得回退到写死常量报型号与价格）
            products = []
            citations = []

        # 置信度：命中产品 + 证据越多越可信；无证据 → 低置信（走 GATE-06）
        country = str((request.context.get("customer") or {}).get("country", "") or "").strip()
        confidence = 0.35 + (0.25 if products else 0.0) + min(0.25, 0.05 * len(citations))
        # 国别未知 → 合规要求无法落地（FR-GRC-05），必须转人工并先问目的港（R12-03）
        needs_human = confidence < 0.55 or not country

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
        # 证书行：只用**产品结构化字段里声明、且 covers 覆盖本型号**的证书：
        #   - covers 为空的是工厂体系证书，走体系模板，不能说"型号被覆盖"（R11-10）
        #   - 过期证书不进对外内容（时效闸门）
        #   - 别的型号的证书，强制性地不能说成覆盖本型号（R12-01）
        cert_line = "—"
        expired_notes = []
        sku = str(p.get("sku", ""))
        cert_nos = [str(x) for x in (p.get("certificates") or [])]
        cert_by_no = {str(h.chunk.identifiers.get("cert_no", h.chunk.doc_id)): h.chunk
                      for h in hits if h.chunk.doc_type == "证书"}
        for cert_no in cert_nos:
            c = cert_by_no.get(cert_no)
            if c is None:
                continue
            covers = [str(x).upper() for x in (c.extra.get("covers") or [])]
            if covers and sku.upper() not in covers:
                continue
            ctype = c.extra.get("cert_type", "证书")
            valid_until = c.valid_until.date().isoformat() if c.valid_until else "—"
            if c.valid_until is not None and c.valid_until.date() < date.today():
                expired_notes.append(f"{cert_no} 已于 {valid_until} 过期，不予引用")
                continue
            tpl = CERT_LINE[lang] if covers else CERT_LINE_SYSTEM[lang]
            cert_line = tpl.format(cert_no=cert_no, cert_type=ctype, valid_until=valid_until)
            break
        body = TEMPLATES[lang].format(
            sku=p["sku"], moq=p["moq"], price_low=p["price_low"], price_high=p["price_high"],
            terms=p["terms"], port=p["port"], lead_time=p["lead_time_days"],
            cert_line=cert_line, ask_line=ASK_LINE[lang],
        )
        # ---- S5-1a：模型路径 ----
        # 模型只写话术，数值由代码从结构化字段回填（模型看不到真值）。
        # 任何异常（调用失败 / 越权 / 漏占位符）都退回上面的模板正文。
        notes: List[str] = []
        llm_usage = None
        model_name = "stub-template"
        if self.writer is not None:
            has_cert = bool(cert_line and cert_line != "—")
            facts = self._facts(p, cert_line, lang)
            # 知识库真值里的数字 → 模型输出的**数字白名单**（价格/交期/MOQ 等）。
            # 模型若把买家的数量写成价格（如 "USD 5000"），它不在这个集合里 → 被判越权。
            truth_numbers = set()
            for k in ("sku", "moq", "price_range", "lead_time", "cert_sentence"):
                truth_numbers |= numbers_of(str(facts.get(k) or ""))
            try:
                md = self.writer.write(lang, message,
                                       company=str((request.context.get("customer") or {}).get("company", "") or ""),
                                       country=country, intents=intents, with_cert=has_cert,
                                       truth_numbers=truth_numbers)
                body = render_facts(md.body, facts, lang)
                model_name = "%s:%s" % (md.provider, md.model)
                llm_usage = md.usage()
                notes.append("模型出稿：%s（输入 %d / 输出 %d tokens，HTTP 调用 %d 次）"
                             % (model_name, md.prompt_tokens, md.completion_tokens, md.calls))
            except DraftRejected as exc:
                # 降级也要记钱（R19-05）：越权被拒时**响应里是有 usage 的**（模型已经返回了正文，
                # 正因为有正文才被判定器拒），所以 token 必须照记、成本必须照算。
                # 只有“调用失败”（HTTP 错误/超时）才拿不到 usage → 那时才空着。
                u = dict(exc.usage or {})
                u.setdefault("provider", "")
                u.setdefault("model", "")
                u.setdefault("prompt_tokens", 0)
                u.setdefault("completion_tokens", 0)
                u.setdefault("cached_tokens", 0)
                u.setdefault("total_tokens", 0)
                u["calls"] = int(u.get("calls") or exc.calls or 0)
                u.setdefault("cost_cny", None)
                u.setdefault("price_tier", "")
                u["degraded"] = True
                u["reject_reason"] = str(exc)
                if u.get("cost_cny") is None and u.get("model"):
                    from ..llm.pricing import estimate_cost
                    est = estimate_cost(str(u.get("model")),
                                       int(u.get("prompt_tokens") or 0),
                                       int(u.get("completion_tokens") or 0))
                    if est is not None:
                        u["cost_cny"], u["price_tier"] = est
                llm_usage = u
                notes.append("模型输出被拒（%s），退回模板草稿" % exc)
        if not country:
            next_action = "买家所在国未知：先确认目的港所在国（合规要求按目的国落地），再人工签核"
        elif needs_human:
            next_action = "置信度不足，请人工确认后再发送"
        else:
            next_action = "价格与交期需人工签核后发送"
        draft = Draft(
            thread_id=request.thread.thread_id, lang=lang, body=body,
            intent_labels=intents, confidence=round(confidence, 2), citations=citations,
            needs_human_confirmation=needs_human,
            next_action=next_action,
            model=model_name,
            # 草稿自带用量与成本：签核留痕要知道这份稿是谁写的、花了多少钱。
            llm_usage=llm_usage,
        )
        return AgentResult(
            agent_id=self.agent_id,
            status=ResultStatus.NEED_HUMAN if needs_human else ResultStatus.OK,
            draft=draft, intent_labels=intents, confidence=round(confidence, 2),
            evidence=[c.model_dump(mode="json") for c in citations],
            notes=notes, llm_usage=llm_usage,
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
                # 只引用**本次选中的那个产品**，别把其它型号也写进对外内容
                if product is not None and c.chunk_id != product.chunk_id:
                    continue
                # 引用标识必须能**唯一定位到一个块**（R14-03）：
                # 一个 sku 现在对应 5 个块（4 语 + 1 规格），只写 sku 指不到具体哪一块。
                cites.append(Citation(citation_id=c.identifiers.get("doc_ref", c.doc_id), type="产品条目",
                                      note=f"{c.title} · 语言 {c.lang} · 库版本 {c.kb_version}"))
            elif c.doc_type == "产品规格":
                # 规格记录 = 数值的唯一真源（FR-KB-07：产品 SKU 与规格版本）。
                # 只能引**本型号**的规格记录 —— 引用里混进别的型号的规格是对外失真（R16 答）。
                if sku and str(c.identifiers.get("sku", "")).upper() != sku.upper():
                    continue
                extra = c.extra or {}
                cites.append(Citation(citation_id=c.identifiers.get("attr_id", c.doc_id), type="产品规格",
                                      note=(f"规格版本 {c.identifiers.get('attr_id', c.doc_id)}"
                                            f" · 参考价 {extra.get('price_low')} {extra.get('currency', '')}"
                                            f" · 交期 {extra.get('lead_time_days', '')} 天"
                                            f" · 库版本 {c.kb_version}")))
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
        # 引用不得重复（R14-01）：同一张证书可能既被词法命中、又被按证书号回查命中
        seen_ids = set()
        uniq = []
        for cit in cites:
            if cit.citation_id in seen_ids:
                continue
            seen_ids.add(cit.citation_id)
            uniq.append(cit)
        return uniq

    @staticmethod
    def _facts(p: Dict[str, object], cert_line: str, lang: Language) -> Dict[str, str]:
        """模型占位符 → 真实值。**真值只来自结构化字段**。

        模型从来没有见过这些值 —— 它只见过占位符符号。
        所以“模型编价格”不是靠提示须守的，而是**保证一无可编的真值**。
        """
        lead = str(p.get("lead_time_days", "") or "")
        lead_unit = {Language.EN: "days", Language.ES: "días",
                     Language.AR: "يومًا", Language.ZH: "天"}[lang]
        per_unit = {Language.EN: "per unit", Language.ES: "por unidad",
                    Language.AR: "للوحدة", Language.ZH: "/台"}[lang]
        currency = str(p.get("currency", "USD") or "USD")
        return {
            "sku": str(p.get("sku", "") or ""),
            "moq": str(p.get("moq", "") or ""),
            "price_range": "%s %s–%s %s" % (currency, p.get("price_low", ""),
                                                 p.get("price_high", ""), per_unit),
            "terms": str(p.get("terms", "") or ""),
            "port": str(p.get("port", "") or ""),
            "lead_time": ("%s %s" % (lead, lead_unit)).strip(),
            "cert_sentence": cert_line if cert_line and cert_line != "—" else "",
            "ask_line": ASK_LINE[lang],
        }

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