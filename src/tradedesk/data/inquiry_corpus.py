# -*- coding: utf-8 -*-
"""自建询盘语料（S4-4c，PRD 9.7 C1—C7 方法）。

为什么必须自建
------------
PRD 9.5 的结论：**“多语种 + 电商询盘 + 含阿拉伯语/西语”的公开语料不存在**（电商对话语料是纯中文单语，
MASSIVE 是通用助手域）。所以 ar/es/en 询盘语料必须自建。

语料的生成方法（PRD 9.7）
----------------------
- **C1 句式骨架**：五类意图（询价/打样/规格/物流/投诉）× 语种 × 场景（首询/追问/催进度/异议）
- **C3 真实感扰动**：碎片式表达、行业黑话、非母语语法、贴附依赖（"see attached"）
- **C5 标注**：意图、命中文档、应引用标识、期望草稿要点

**标注由模板结构直接推出**（而不是人工回填）—— 所以它是**精确的**，这也是自建语料相比人工标注的优势；
但同时它也是**模板化的**（句式重复度高）—— 所以数字必须附带语料覆盖度说明，不得当成真实买家问法的分布。

语种范围（PRD 12.3）：**买家语种 en / es / ar**（设指标门槛）+ **zh 内部对照**（不单独设门槛）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

CORPUS_VERSION = "inq-2026.11-v1"

# 五类意图（PRD 9.6 重映射后的询盘意图，不是 MASSIVE 的通用意图）
INTENT_PRICE = "price"        # 询价
INTENT_SAMPLE = "sample"      # 打样
INTENT_SPEC = "spec"          # 规格
INTENT_LOGISTICS = "logistics"  # 物流
INTENT_COMPLAINT = "complaint"  # 投诉
INTENTS = [INTENT_PRICE, INTENT_SAMPLE, INTENT_SPEC, INTENT_LOGISTICS, INTENT_COMPLAINT]

SCENARIOS = ["first", "followup", "chase", "objection"]

# 目的港 → 买家所在国（用于合规过滤与证据标注）
PORT_COUNTRY = {
    "Santos": "BR", "Barcelona": "ES", "Valparaíso": "CL", "Hamburg": "DE",
    "Rotterdam": "NL", "Alexandria": "EG", "Casablanca": "MA", "Jebel Ali": "AE",
    "Busan": "KR", "Ningbo": "CN",
}

MODELS = ["X-200", "M-77", "X-300"]


@dataclass
class InquiryCase:
    """一条询盘样本。"""

    case_id: str
    lang: str
    intent: str
    scenario: str
    text: str
    expect_sku: str = ""            # 期望命中的型号（投诉类为空）
    expect_country: str = ""
    expect_citations: List[str] = field(default_factory=list)
    noise: str = ""                 # C3 扰动说明（如“碎片式表达”）


def _slots(lang: str):
    """每个语种的目的港与数量槽位。"""
    ports = {
        "en": ["Santos", "Hamburg", "Rotterdam"],
        "es": ["Santos", "Barcelona", "Valparaíso"],
        "ar": ["Jebel Ali", "Alexandria", "Casablanca"],
        "zh": ["Santos", "Hamburg", "Busan"],
    }[lang]
    qtys = ["500", "1000", "2000"]
    return ports, qtys


# 句式骨架：{lng}×意图×场景。每条是 (pattern, scenario, noise)
TEMPLATES: Dict[str, Dict[str, List[tuple]]] = {
    "en": {
        INTENT_PRICE: [
            ("Could you quote {qty} pcs of {model} FOB {port}?", "first", ""),
            ("What is your best price for {model}? We need about {qty} units.", "first", ""),
            ("Please send us the unit price of {model} for {qty} pieces CIF {port}.", "first", ""),
            ("Following up on our quote request for {qty} pcs of {model} - any update?", "followup", ""),
            ("We are comparing suppliers for {model} ({qty} pcs). What is your price?", "objection", "supplier comparison"),
            ("price for {model} {qty} pcs FOB {port}", "first", "fragmented / terse"),
        ],
        INTENT_SAMPLE: [
            ("Can you send a sample of {model} before we order {qty} pcs?", "first", ""),
            ("We need one sample of {model} to test. Is that possible?", "first", ""),
            ("Please send a sample unit of {model} to {port}.", "first", ""),
            ("Any news on the sample of {model} we asked for last week?", "followup", ""),
            ("How much is a sample of {model}, and how long does it take?", "first", ""),
            ("sample {model} please, see attached drawing", "first", "attachment reference"),
        ],
        INTENT_SPEC: [
            ("What is the material and capacity of {model}?", "first", ""),
            ("Could you share the load capacity of {model} in kg?", "first", ""),
            ("Does {model} come with CE certification? We need the certificate number.", "first", ""),
            ("What is the MOQ for {model}?", "first", ""),
            ("Is the wheel of {model} nylon or rubber? Please confirm the spec.", "objection", ""),
            ("spec of {model} pls, need load data sheet", "first", "fragmented / terse"),
        ],
        INTENT_LOGISTICS: [
            ("What is the shipping cost to {port} for {qty} pcs of {model}?", "first", ""),
            ("Which port do you ship from for {model}?", "first", ""),
            ("Can you deliver {model} to {port} within 30 days?", "first", ""),
            ("Do you accept CIF {port} terms for {model}?", "first", ""),
            ("The delivery for {model} is late again. When will it arrive?", "chase", ""),
            ("how long delivery {model} to {port}? need urgent", "first", "fragmented / terse"),
        ],
        INTENT_COMPLAINT: [
            ("The goods we received are damaged. We need compensation.", "objection", ""),
            ("Half of the {qty} pcs of {model} arrived broken. Please compensate us.", "objection", ""),
            ("The delivery is late again. This is the second delay for {model}.", "chase", ""),
            ("Your colleague promised a faster shipment for {model}. We are very unhappy.", "objection", ""),
            ("The quality of the {model} we received does not match the sample.", "objection", ""),
            ("complaint about last {model} order, pls respond quickly", "chase", "fragmented / terse"),
        ],
    },
    "es": {
        INTENT_PRICE: [
            ("¿Podrías cotizar {qty} unidades del {model} FOB {port}?", "first", ""),
            ("¿Cuál es su mejor precio para {model}? Necesitamos unas {qty} piezas.", "first", ""),
            ("Por favor envíenos el precio unitario del {model} para {qty} piezas CIF {port}.", "first", ""),
            ("Seguimos esperando la cotización de {qty} unidades del {model}, ¿tienen alguna novedad?", "followup", ""),
            ("Estamos comparando proveedores para {model} ({qty} unidades). ¿Cuál es su precio?", "objection", "supplier comparison"),
            ("precio {model} {qty} uds FOB {port}", "first", "fragmented / terse"),
        ],
        INTENT_SAMPLE: [
            ("¿Pueden enviar una muestra del {model} antes de pedir {qty} unidades?", "first", ""),
            ("Necesitamos una muestra del {model} para probarla. ¿Es posible?", "first", ""),
            ("Por favor, envíen una unidad de muestra del {model} a {port}.", "first", ""),
            ("¿Hay novedades sobre la muestra del {model} que pedimos la semana pasada?", "followup", ""),
            ("¿Cuánto cuesta una muestra del {model} y cuánto tarda?", "first", ""),
            ("muestra {model} por favor, ver plano adjunto", "first", "attachment reference"),
        ],
        INTENT_SPEC: [
            ("¿Cuál es el material y la capacidad del {model}?", "first", ""),
            ("¿Nos pueden compartir la capacidad de carga del {model} en kg?", "first", ""),
            ("¿El {model} incluye certificación CE? Necesitamos el número de certificado.", "first", ""),
            ("¿Cuál es el pedido mínimo (MOQ) del {model}?", "first", ""),
            ("¿La rueda del {model} es de nailon o de caucho? Por favor, confirmen la especificación.", "objection", ""),
            ("ficha técnica {model} pls, necesito datos de carga", "first", "fragmented / terse"),
        ],
        INTENT_LOGISTICS: [
            ("¿Cuál es el costo de envío a {port} para {qty} unidades del {model}?", "first", ""),
            ("¿Desde qué puerto salen los envíos del {model}?", "first", ""),
            ("¿Pueden entregar el {model} en {port} dentro de 30 días?", "first", ""),
            ("¿Aceptan condiciones CIF {port} para el {model}?", "first", ""),
            ("La entrega del {model} vuelve a retrasarse. ¿Cuándo llegará?", "chase", ""),
            ("plazo entrega {model} a {port}? urgente por favor", "first", "fragmented / terse"),
        ],
        INTENT_COMPLAINT: [
            ("La mercancía que recibimos está dañada. Necesitamos compensación.", "objection", ""),
            ("La mitad de las {qty} unidades del {model} llegó rotas. Compénsenlos, por favor.", "objection", ""),
            ("La entrega vuelve a retrasarse. Es el segundo retraso del {model}.", "chase", ""),
            ("Su colega prometió un envío más rápido del {model}. Estamos muy descontentos.", "objection", ""),
            ("La calidad del {model} que recibimos no coincide con la muestra.", "objection", ""),
            ("reclamo por el último pedido de {model}, respondan rápido por favor", "chase", "fragmented / terse"),
        ],
    },
    "ar": {
        INTENT_PRICE: [
            ("هل تستطيعون عن سعر السعر لـ {qty} قطعة من الموديل {model} FOB {port}؟", "first", ""),
            ("ما أفضل سعر لـ {model}؟ نحتاج حوال {qty} قطعة.", "first", ""),
            ("من فضلك إرسال سعر للواحدة لـ {qty} قطعة عن {model} CIF {port}.", "first", ""),
            ("نتابع طلب العرض الذي طلبناه {model}، هل هناك جديد؟", "followup", ""),
            ("نقارن على أسماع الشراء لـ {model} ({qty} قطعة)، ما السعر تقريناك؟", "objection", "supplier comparison"),
            ("سعر {model} {qty} قطعة FOB {port}", "first", "fragmented / terse"),
        ],
        INTENT_SAMPLE: [
            ("هل يُمكن إرسال عينة تجربة من {model} قبل الطلب {qty}؟", "first", ""),
            ("نحتاج عينة من {model} لنجربها؛ هل أمكنن؟", "first", ""),
            ("من فضلك إرسال وحدة عينة من {model} إلى {port}.", "first", ""),
            ("هل توففت عناك العينة التي طلبناهاً من {model}؟", "followup", ""),
            ("كم ثمان عينة من {model}، وكم تستغرقك؟", "first", ""),
            ("عينة {model} من فضلك؛ انظر الرسم المرفق", "first", "attachment reference"),
        ],
        INTENT_SPEC: [
            ("ما مادة {model} وسعة الحمول؟", "first", ""),
            ("هل تستطيعون معيار حمولة الحمل للطراة {model} بكيلوجرام؟", "first", ""),
            ("هل يحتاج {model} شهادة CE؟ نحتاج رقم الشهادة.", "first", ""),
            ("ما الحد الأدنى للطلب من {model}؟", "first", ""),
            ("هل عجلات {model} من النايلون أم المطاط؟ يرجى تأكيد المواصفات.", "objection", ""),
            ("مواصفة {model} من فضلك، أحتاج بيانات الحمول", "first", "fragmented / terse"),
        ],
        INTENT_LOGISTICS: [
            ("ما تكلفة شحن الشحن إلى {port} لعد {qty} قطعة من {model}؟", "first", ""),
            ("من أي منفاذة الشحن من {model}؟", "first", ""),
            ("هل تستطيعون تسليم {model} إلى {port} خلاء 30 يوماً؟", "first", ""),
            ("هل تقبلون شروط CIF {port} للمنتج {model}؟", "first", ""),
            ("تأخّر الشحنة مرة ثانية. متى يصل {model}؟", "chase", ""),
            ("مدة التسليم {model} إلى {port}؟ عاجل", "first", "fragmented / terse"),
        ],
        INTENT_COMPLAINT: [
            ("الشحنة التي سلمناها تالفةة، نحتاج إن عوضاع.", "objection", ""),
            ("نصف من القطع {qty} من {model} وصلت مكسورة. نرجو تعويضناً.", "objection", ""),
            ("التوصيل متأخّر مرة أخرى. هذه التأخّر تأخّر للطلب {model}.", "chase", ""),
            ("زميلك وعد وعد موعددار شحن أسرع لـ {model}. نحن سيئان لديًاً.", "objection", ""),
            ("جودة {model} التي سلمناها لا تطابق مع العينة.", "objection", ""),
            ("شكوى بخصوص الطلب الأخير لـ {model}، يرجى الرد بسرعة.", "chase", "fragmented / terse"),
        ],
    },
    "zh": {
        INTENT_PRICE: [
            ("请报价 {qty} 台 {model} FOB {port}", "first", ""),
            ("{model} 的最佳价格是多少？我们需要 {qty} 台", "first", ""),
            ("跟进上次的 {model} 报价，有进展吗？", "followup", ""),
        ],
        INTENT_SAMPLE: [
            ("能先发一个 {model} 样品吗？", "first", ""),
            ("我们需要一个 {model} 样品测试", "first", ""),
            ("样品发出了吗？", "followup", ""),
        ],
        INTENT_SPEC: [
            ("{model} 的材质和负荷是什么？", "first", ""),
            ("{model} 有 CE 认证吗？需要证书编号", "first", ""),
            ("{model} 的起订量是多少？", "first", ""),
        ],
        INTENT_LOGISTICS: [
            ("{qty} 台 {model} 运到 {port} 的运费多少？", "first", ""),
            ("{model} 从哪个港发货？", "first", ""),
            ("{model} 的交期是多久？", "first", ""),
        ],
        INTENT_COMPLAINT: [
            ("收到的货有损坏，我们要求赔偿", "objection", ""),
            ("{qty} 台 {model} 有一半到达时损坏", "objection", ""),
            ("{model} 又延迟了，这是第二次延迟了", "chase", ""),
        ],
    },
}


# 不点名型号的样本：真实场景里买家常不知道我们有哪个型号，只问价格/交期/资质。
# 这批样本不能用 SKU 级命中评估（期望命中未知），但能看「能否找到任意产品块」与意图分类。
NO_SKU_TEMPLATES: Dict[str, Dict[str, List[str]]] = {
    "en": {
        INTENT_PRICE: ["Send me your best price.", "What is your price for the hand truck?",
                       "How much is one unit?"],
        INTENT_SAMPLE: ["Can you send a sample?", "Is a sample available before we order?",
                        "We would like to test one unit first."],
        INTENT_SPEC: ["What is the material and capacity of your hand truck?",
                      "Could you send the load data sheet?", "What is your MOQ?"],
        INTENT_LOGISTICS: ["What is the shipping cost to Santos?", "Which port do you ship from?",
                           "How long does delivery take?"],
        INTENT_COMPLAINT: ["The goods we received are damaged. We need compensation.",
                           "Your shipment is late again.", "The quality does not match the sample."],
    },
    "es": {
        INTENT_PRICE: ["¿Cuál es su mejor precio?", "¿Cuánto cuesta su producto?",
                       "¿Cuál es el precio de una unidad?"],
        INTENT_SAMPLE: ["¿Pueden enviar una muestra?",
                        "¿Hay una muestra disponible antes de pedir?",
                        "¿Nos dejan probar una unidad primero?"],
        INTENT_SPEC: ["¿Cuál es el material y la capacidad de su carretilla?",
                      "¿Nos pueden enviar la ficha técnica?", "¿Cuál es su pedido mínimo?"],
        INTENT_LOGISTICS: ["¿Cuánto cuesta el envío a Santos?", "¿Desde qué puerto envían los envíos?",
                           "¿Cuánto tarda la entrega?"],
        INTENT_COMPLAINT: ["La mercancía que recibimos está dañada. Necesitamos compensación.",
                           "Su envío vuelve a retrasarse.",
                           "La calidad no coincide con la muestra."],
    },
    "ar": {
        INTENT_PRICE: ["ما أفضل سعركم؟",
                       "كم سعر منتجم؟",
                       "كم تكلفة الوحدة الواحدة؟"],
        INTENT_SAMPLE: ["هل تستطيعون إرسال عينة؟",
                        "هل يُوجد عينة قبل الطلب؟",
                        "نريد تجربة وحدة واحدة أولاً من فبل الطلب؟"],
        INTENT_SPEC: ["ما مادة العربة وسعتها؟",
                      "هل تستطيعون بعدة المواصفات؟",
                      "ما الحد الأدنى للطلب لديكم؟"],
        INTENT_LOGISTICS: ["ما تكلفة الشحن إلى سانتوس؟",
                           "من أي منفاذة الشحن؟",
                           "كم تستغلق التوصيل؟"],
        INTENT_COMPLAINT: ["البضاعة التي استلمناها تالفة. نحتاج تعويضاً.",
                           "شحنتكم يتأخّر متأخّراً.",
                           "جودة الشحنة لا تطابق مع العينة."],
    },
    "zh": {
        INTENT_PRICE: ["请报一下最佳价格", "你们产品多少钱"],
        INTENT_SAMPLE: ["能先发一个样品吗", "我们想先试一下一台"],
        INTENT_SPEC: ["你们的材质和载荷是什么？", "请发越容量表"],
        INTENT_LOGISTICS: ["运到圣佬斯的运费多少？", "从哪个港发货？"],
        INTENT_COMPLAINT: ["收到的货有损坏，我们要求赔偿", "交货又延迟了"],
    },
}


def build_corpus() -> List[InquiryCase]:
    """按 C1 句式骨架 × 意图 × 场景 生成语料。

    每条样本的标注（意图/命中型号/期望引用）由模板结构直接推出—— 没有人工回填导致的标注误差。
    """
    cases: List[InquiryCase] = []
    seq = 0
    for lang in ("en", "es", "ar", "zh"):
        ports, qtys = _slots(lang)
        for intent in INTENTS:
            for idx, (pattern, scenario, noise) in enumerate(TEMPLATES[lang][intent]):
                seq += 1
                model = MODELS[idx % len(MODELS)]
                port = ports[idx % len(ports)]
                qty = qtys[idx % len(qtys)]
                text = pattern.format(model=model, port=port, qty=qty)
                expect_sku = model if intent != INTENT_COMPLAINT else ""
                expect_cites = [f"SPEC-{model}"] if expect_sku else []
                cases.append(InquiryCase(
                    case_id=f"{lang}-{intent}-{idx + 1}",
                    lang=lang, intent=intent, scenario=scenario, text=text,
                    expect_sku=expect_sku,
                    expect_country=PORT_COUNTRY.get(port, ""),
                    expect_citations=expect_cites,
                    noise=noise))
    # 批次二：不点名型号的样本
    for lang in ("en", "es", "ar", "zh"):
        for intent in INTENTS:
            for idx, pattern in enumerate(NO_SKU_TEMPLATES[lang][intent]):
                seq += 1
                cases.append(InquiryCase(
                    case_id=f"{lang}-{intent}-nosku-{idx + 1}",
                    lang=lang, intent=intent, scenario="first", text=pattern,
                    expect_sku="",           # 买家未说型号 → 不能用 SKU 级命中评估
                    expect_country="",       # 也未说目的港 → 国别未知 → 应转人工
                    expect_citations=[],
                    noise="no model named"))
    return cases


def corpus_summary(cases: List[InquiryCase]) -> Dict[str, Dict[str, int]]:
    """语料覆盖度统计（按语种/意图）——报告里必须附上。"""
    out: Dict[str, Dict[str, int]] = {}
    for c in cases:
        out.setdefault(c.lang, {}).setdefault(c.intent, 0)
        out[c.lang][c.intent] += 1
    return out
