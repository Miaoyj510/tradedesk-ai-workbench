# -*- coding: utf-8 -*-
"""知识库种子数据（多语种 + 标识 + 时效）。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List

from ..domain.models import KBChunk

KB_VERSION = "kb-2026.11-b"


def _d(s: str) -> datetime:
    return datetime.fromisoformat(s + "+00:00")


def build_demo_chunks(kb_version: str = KB_VERSION) -> List[KBChunk]:
    chunks: List[KBChunk] = []

    def add(**kw):
        kw.setdefault("kb_version", kb_version)
        chunks.append(KBChunk(**kw))

    # ---------------- 产品条目（结构化字段走 extra，文本供语义/词法召回） ----------------
    add(chunk_id="KB-P-X200", doc_id="SKU-X-200", doc_type="产品条目",
        title="X-200 手动液压搬运车 / Manual Hydraulic Hand Truck",
        text=("X-200 手动液压搬运车，铸铁泵体配尼龙轮，额定载荷 2 吨，最小起订量 MOQ 1000 台，"
              "交期 25-30 天，FOB 深圳，参考价 USD 4.85-5.20 每台，HS 编码 8479.90。"
              "Manual hydraulic hand truck, 2 ton capacity, MOQ 1000, lead time 25-30 days, FOB Shenzhen. "
              "Carretilla hidráulica manual, capacidad 2 toneladas, MOQ 1000, plazo de entrega 25-30 días, FOB Shenzhen. "
              "عربة يدوية هيدروليكية، حمولة 2 طن، الحد الأدنى للطلب 1000، مدة التسليم 25-30 يومًا، FOB Shenzhen."),
        lang="zh", identifiers={"sku": "X-200", "hs_code": "8479.90"},
        countries=[], hs_codes=["8479.90"],
        tags=["产品", "搬运车", "MOQ", "lead time", "FOB", "Shenzhen", "价格", "交期", "precio", "plazo"],
        extra={"material": "铸铁泵体 + 尼龙轮", "capacity": "额定载荷 2 吨", "moq": 1000,
               "lead_time_days": "25-30", "price_low": 4.85, "price_high": 5.20, "currency": "USD",
               "terms": "FOB", "port": "Shenzhen", "certificates": ["CERT-CE-2041"]})

    add(chunk_id="KB-P-M77", doc_id="SKU-M-77", doc_type="产品条目",
        title="M-77 手动吊装夹具 / Manual Lifting Clamp",
        text=("M-77 手动吊装夹具，铸钢钳口配尼龙滚轮，安全载荷 SWL 500 kg，最小起订量 MOQ 100 台，"
              "交期 20-25 天，FOB 宁波，参考价 USD 6.40-6.90 每台，HS 编码 8479.89。"
              "Manual lifting clamp, safe working load 500 kg, MOQ 100, lead time 20-25 days, FOB Ningbo. "
              "Eslinga de elevación manual, carga de trabajo segura 500 kg, MOQ 100, plazo de entrega 20-25 días, FOB Ningbo. "
              "مشبك رفع يدوي، حمولة 500 كجم، الحد الأدنى للطلب 100، مدة التسليم 20-25 يومًا، FOB Ningbo."),
        lang="zh", identifiers={"sku": "M-77", "hs_code": "8479.89"},
        countries=[], hs_codes=["8479.89"],
        tags=["产品", "吊装", "夹具", "SWL", "capacity", "MOQ", "lead time", "FOB", "Ningbo", "价格"],
        extra={"material": "铸钢 + 尼龙滚轮钳口", "capacity": "安全载荷 500 kg", "moq": 100,
               "lead_time_days": "20-25", "price_low": 6.40, "price_high": 6.90, "currency": "USD",
               "terms": "FOB", "port": "Ningbo", "certificates": ["CERT-ISO-9001"]})

    add(chunk_id="KB-P-X300", doc_id="SKU-X-300", doc_type="产品条目",
        title="X-300 电动搬运车 / Electric Pallet Truck",
        text=("X-300 电动搬运车，锂电动力，载荷 1.5 吨，最小起订量 MOQ 50 台，交期 40-45 天，FOB 青岛，"
              "参考价 USD 12.30-13.10 每台，HS 编码 8427.10。"
              "Electric pallet truck, 1.5 ton, MOQ 50, lead time 40-45 days, FOB Qingdao. "
              "Transpaleta eléctrica, MOQ 50, plazo de entrega 40-45 días, FOB Qingdao."),
        lang="zh", identifiers={"sku": "X-300", "hs_code": "8427.10"},
        countries=[], hs_codes=["8427.10"],
        tags=["产品", "电动", "搬运车", "MOQ", "lead time", "FOB", "Qingdao"],
        extra={"material": "锂电动力 + 钢制车架", "capacity": "载荷 1.5 吨", "moq": 50,
               "lead_time_days": "40-45", "price_low": 12.30, "price_high": 13.10, "currency": "USD",
               "terms": "FOB", "port": "Qingdao", "certificates": []})

    # ---------------- 证书 ----------------
    add(chunk_id="KB-C-CE2041", doc_id="CERT-CE-2041", doc_type="证书",
        title="CE 符合性声明 CERT-CE-2041（覆盖 X-200）",
        text=("CE 符合性声明，适用型号 X-200 / X-210，发证方 TÜV SÜD (0086)，"
              "依据 EN 12195 与机械指令 2006/42/EC，有效期至 2027-03-31。"
              "CE declaration covering X-200, valid until 2027-03-31."),
        lang="zh", identifiers={"cert_no": "CERT-CE-2041", "doc_no": "CERT-CE-2041"},
        countries=["EU", "DE", "ES", "FR", "IT", "NL", "PT"], hs_codes=["8479.90"],
        tags=["证书", "CE", "认证", "certificate", "certificación"],
        effective_from=_d("2024-01-01"), valid_until=_d("2027-03-31"),
        extra={"cert_type": "CE", "covers": ["X-200", "X-210"], "issuer": "TÜV SÜD (0086)"})

    add(chunk_id="KB-C-ISO9001", doc_id="CERT-ISO-9001", doc_type="证书",
        title="工厂资质 CERT-ISO-9001（ISO 9001 质量体系）",
        text="工厂质量体系证书 ISO 9001，覆盖全部生产型号，有效期至 2027-01-15。Plant ISO 9001 certificate, valid until 2027-01-15.",
        lang="zh", identifiers={"cert_no": "CERT-ISO-9001", "doc_no": "CERT-ISO-9001"},
        countries=[], hs_codes=[], tags=["证书", "ISO 9001", "工厂资质"],
        effective_from=_d("2024-01-01"), valid_until=_d("2027-01-15"),
        extra={"cert_type": "ISO 9001", "covers": []})

    # 下面两张在今天(2026-11-19)已经过期 —— 必须被时效过滤剔除
    add(chunk_id="KB-C-TCM77118", doc_id="TC-M77-118", doc_type="证书",
        title="载荷测试报告 TC-M77-118（覆盖 M-77，已过期）",
        text="M-77 载荷测试报告 SGS 出具，覆盖 M-77，有效期至 2026-08-12（已过期，不得引用）。Test certificate expired 2026-08-12.",
        lang="zh", identifiers={"cert_no": "TC-M77-118", "doc_no": "TC-M77-118"},
        countries=[], hs_codes=["8479.89"], tags=["证书", "测试报告", "过期"],
        effective_from=_d("2024-01-01"), valid_until=_d("2026-08-12"),
        extra={"cert_type": "Test Certificate", "covers": ["M-77"], "expired": True})

    add(chunk_id="KB-C-CE1890", doc_id="CERT-CE-1890", doc_type="证书",
        title="旧版 CE 证书 CERT-CE-1890（覆盖 X-190 系列，已废止）",
        text="旧版 CE 证书，覆盖 X-190 系列，已于 2025-11-30 废止，被 CERT-CE-2041 取代。Obsolete CE certificate.",
        lang="zh", identifiers={"cert_no": "CERT-CE-1890", "doc_no": "CERT-CE-1890"},
        countries=["EU"], hs_codes=[], tags=["证书", "CE", "废止"],
        effective_from=_d("2022-01-01"), effective_to=_d("2025-11-30"),
        extra={"cert_type": "CE", "covers": ["X-190"], "obsolete": True})

    # ---------------- 法规条款 ----------------
    add(chunk_id="KB-R-EU112", doc_id="REG-EU-112", doc_type="法规条款",
        title="欧盟机械指令 2006/42/EC：CE 标识与符合性声明（REG-EU-112）",
        text=("欧盟机械指令 2006/42/EC 要求投放欧盟市场的机械产品加贴 CE 标识并出具符合性声明；"
              "自 2026-01-01 起，进口商须留存技术文件 10 年。"
              "EU Machinery Directive: CE marking and declaration of conformity; importers must keep "
              "technical documentation for 10 years since 2026-01-01."),
        lang="zh", identifiers={"clause_no": "REG-EU-112", "doc_no": "REG-EU-112"},
        countries=["EU", "DE", "ES", "FR", "IT", "NL", "PT"], hs_codes=["8479.90", "8479.89"],
        tags=["法规", "CE", "欧盟", "机械指令", "declaration of conformity"],
        effective_from=_d("2026-01-01"), effective_to=_d("2031-01-01"),
        extra={"regulation": "2006/42/EC", "requirement": "CE 标识 + 符合性声明 + 技术文件留存 10 年"})

    add(chunk_id="KB-R-BR077", doc_id="REG-BR-077", doc_type="法规条款",
        title="巴西 ANATEL 认证要求（REG-BR-077）",
        text=("巴西进口商需确认电信/机械类产品的 ANATEL 认证状态；自 2025-03-01 起，"
              "无有效 ANATEL 认证的批次不得清关。Brazil ANATEL certification required for cleared batches since 2025-03-01."),
        lang="zh", identifiers={"clause_no": "REG-BR-077", "doc_no": "REG-BR-077"},
        countries=["BR"], hs_codes=["8479.90", "8427.10"], tags=["法规", "巴西", "ANATEL", "清关"],
        effective_from=_d("2025-03-01"), effective_to=_d("2030-03-01"),
        extra={"regulation": "ANATEL", "requirement": "有效 ANATEL 认证方可清关"})

    add(chunk_id="KB-R-US204", doc_id="REG-US-204", doc_type="法规条款",
        title="美国 CPSC 手推车警示标签提案（REG-US-204，尚未生效）",
        text=("CPSC 关于手动搬运车警示标签的拟议规则，预计 2027-01-01 生效；"
              "在生效前**不得**作为对买家的强制要求输出。Proposed CPSC warning-label rule, effective 2027-01-01 (not yet in force)."),
        lang="zh", identifiers={"clause_no": "REG-US-204", "doc_no": "REG-US-204"},
        countries=["US"], hs_codes=["8479.90"], tags=["法规", "美国", "CPSC", "提案", "未生效"],
        effective_from=_d("2027-01-01"),
        extra={"regulation": "CPSC", "requirement": "拟议：警示标签", "status": "proposed"})

    add(chunk_id="KB-R-AE099", doc_id="REG-AE-099", doc_type="法规条款",
        title="阿联酋进口管制与最终用户核查要求（REG-AE-099）",
        text=("阿联酋进口需核查最终用户是否属于受控最终用户清单；命中即须停止发货并上报。"
              "UAE import control: screen end users against the controlled end-user list; stop shipment on hit."),
        lang="zh", identifiers={"clause_no": "REG-AE-099", "doc_no": "REG-AE-099"},
        countries=["AE"], hs_codes=[], tags=["法规", "阿联酋", "进口管制", "最终用户", "export control"],
        effective_from=_d("2024-01-01"), effective_to=_d("2030-01-01"),
        extra={"regulation": "UAE import control", "requirement": "最终用户核查"})

    add(chunk_id="KB-R-EG045", doc_id="REG-EG-045", doc_type="法规条款",
        title="埃及进口认证要求（REG-EG-045）",
        text="埃及进口商要求提供符合性证书与阿拉伯语标签；出口方需随附可回指的证书编号。Egypt import: certificate of conformity and Arabic labelling required.",
        lang="zh", identifiers={"clause_no": "REG-EG-045", "doc_no": "REG-EG-045"},
        countries=["EG"], hs_codes=[], tags=["法规", "埃及", "认证", "certificate"],
        effective_from=_d("2025-06-01"), effective_to=_d("2030-06-01"),
        extra={"regulation": "Egypt import", "requirement": "符合性证书 + 阿拉伯语标签"})

    # ---------------- 名单条目（结构化演示数据，非真实名单） ----------------
    add(chunk_id="KB-S-CN2211", doc_id="CN-2211", doc_type="名单条目",
        title="受控最终用户 CN-2211（Gulf Trading Partners LLC）",
        text=("出口管制·受控最终用户条目：Gulf Trading Partners LLC，国别 AE。"
              "命中后不得生成任何对外内容，转人工判定。Controlled end-user entry; on hit, suppress all outbound content."),
        lang="zh", identifiers={"entry_id": "CN-2211", "doc_no": "CN-2211"},
        countries=["AE"], tags=["名单", "受控最终用户", "出口管制"],
        effective_from=_d("2026-01-01"),
        extra={"list_type": "出口管制·受控最终用户", "name": "Gulf Trading Partners LLC",
               "list_version": "sanctions-2026.11-a", "demo": True})

    add(chunk_id="KB-S-CN2307", doc_id="CN-2307", doc_type="名单条目",
        title="受控最终用户 CN-2307（Demo Metal Trading SARL）",
        text="出口管制·受控最终用户条目：Demo Metal Trading SARL，国别 SA。命中后转人工判定。Controlled end-user entry (demo).",
        lang="zh", identifiers={"entry_id": "CN-2307", "doc_no": "CN-2307"},
        countries=["SA"], tags=["名单", "受控最终用户", "出口管制"],
        effective_from=_d("2026-01-01"),
        extra={"list_type": "出口管制·受控最终用户", "name": "Demo Metal Trading SARL",
               "list_version": "sanctions-2026.11-a", "demo": True})

    # ---------------- 术语表 ----------------
    add(chunk_id="KB-T-MOQ", doc_id="TERM-MOQ", doc_type="术语表",
        title="MOQ / 最小起订量 / cantidad mínima de pedido / الحد الأدنى للطلب",
        text=("MOQ（最小起订量）：es 标准译 cantidad mínima de pedido；ar 标准译 الحد الأدنى للطلب；"
              "禁用译法 pedido mínimo、MOQ mínimo。"),
        lang="zh", identifiers={"term_id": "TERM-MOQ", "doc_no": "TERM-MOQ"},
        tags=["术语表", "MOQ", "最小起订量", "cantidad mínima de pedido"],
        extra={"concept": "moq", "forbidden": ["pedido mínimo", "MOQ mínimo"]})

    add(chunk_id="KB-T-LEADTIME", doc_id="TERM-LEADTIME", doc_type="术语表",
        title="交期 / lead time / plazo de entrega / مدة التسليم",
        text="交期（lead time）：es 标准译 plazo de entrega；ar 标准译 مدة التسليم；禁用译法 tiempo de entrega（歧义）。",
        lang="zh", identifiers={"term_id": "TERM-LEADTIME", "doc_no": "TERM-LEADTIME"},
        tags=["术语表", "交期", "lead time", "plazo de entrega"],
        extra={"concept": "lead_time", "forbidden": ["tiempo de entrega"]})

    return chunks


def load_kb(store, kb_version: str = KB_VERSION, replace: bool = True) -> int:
    """把种子块写入存储（幂等）。返回块数量。"""
    if replace:
        try:
            store.clear_kb()
        except Exception:
            pass
    chunks = build_demo_chunks(kb_version)
    for c in chunks:
        store.upsert_kb_chunk(c)
    return len(chunks)
