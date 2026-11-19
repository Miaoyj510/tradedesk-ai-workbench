/* TradeDesk AI Workbench — 演示数据（严格对应 PRD 的状态机/闸门/引用口径） */
window.TD = (function () {
  const STAGE = {
    INQUIRY: '询盘', SAMPLING: '样品', QUOTING: '报价', ORDERED: '订单',
    DOCS: '单证', PAYMENT: '收款', CLOSED: '已关闭', LOST: '已流失'
  };
  const INTENT = {
    price: '询价', sample: '打样', spec: '规格', logistics: '物流', complaint: '投诉'
  };

  const threads = [
    {
      id: 'T-1042', company: 'Brasil Distribuidora Ltda', country: '巴西', flag: 'BR', lang: 'es',
      stage: 'QUOTING', risk: 'mid', state: 'needs_signoff', sla: 42, wait: 6, intent: ['price', 'spec'],
      title: '5000 pcs SKU X-200 · FOB 深圳 · 要求 CE 认证',
      draft: {
        lang: 'es',
        body: 'Hola Nuria:\n\nGracias por su consulta. Para el modelo X-200 (SKU X-200, versión 2), nuestro precio de referencia en condiciones FOB Shenzhen para 5000 unidades es de USD 4.85–5.20 por unidad, y el plazo de entrega estimado es de 25–30 días después del depósito.\n\nEl modelo está cubierto por el certificado <mark>CE</mark> <mark>CERT-CE-2041</mark>, válido hasta 2027-03-31, y se pueden enviar los certificados de origen y de inspección con el primer envío.\n\nQuedo a la espera de su confirmación sobre el puerto de destino para cotizar el flete.',
        intent: ['询价', '规格'], confidence: 0.86,
        next: '请价格负责人签核参考价区间与交期后发送'
      },
      citations: [
        { id: 'SKU X-200 v2', type: '产品条目', note: 'MOQ 1000 · 交期 25–30 天 · FOB 深圳' },
        { id: 'CERT-CE-2041', type: '证书', note: '有效期至 2027-03-31 · 覆盖 X-200' },
        { id: 'TERM-MOQ', type: '术语表', note: 'MOQ / cantidad mínima de pedido' },
        { id: 'REG-EU-112', type: '法规条款', note: 'CE 标识适用要求 · 2026-01-01 生效' }
      ],
      commitments: ['样品已寄出（11-08，SF 7712345665）', '交期参考 25–30 天（未签核）'],
      open: ['买家未确认目的港', '需确认是否需要第三方检验（SGS）'],
      deadlines: [{ label: '报价 SLA', remain: '42 分钟', level: 'warn' }, { label: '客户跟进', remain: '今天 18:00', level: 'ok' }],
      gates: [
        { id: 'GATE-01', title: '价格承诺', desc: '草稿含参考价区间 USD 4.85–5.20', hit: true },
        { id: 'GATE-02', title: '交期承诺', desc: '草稿含交期 25–30 天', hit: true },
        { id: 'GATE-05', title: '认证声明', desc: '引用 CERT-CE-2041（有效期至 2027-03-31）', hit: false }
      ],
      evidence: [
        { k: '名单版本', v: 'sanctions-2026.11-a' },
        { k: '核验结果', v: '未命中（主体 / 国别 / 物项 三维）' },
        { k: '命中字段', v: '—' }
      ],
      messages: [
        { who: 'buyer', name: 'Núria Almeida', time: '今天 09:14', lang: 'es', text: 'Hola, somos un distribuidor en Brasil. ¿Podrían cotizar 5000 unidades del X-200 en FOB Shenzhen? Además necesitamos confirmar si el modelo tiene certificación CE y el plazo de entrega. Adjunto el dibujo técnico.', attach: 'X-200_dibujo.pdf · 2 页' }
      ],
      timeline: [
        { time: '09:14', text: '买家来信（es）· 附件 1 个', accent: false },
        { time: '09:14', text: 'Sentinel 核验通过 · 名单 <b>sanctions-2026.11-a</b>', accent: false },
        { time: '09:15', text: 'Inquiry 生成草稿 · 置信度 <b>0.86</b> · 引用 4 条', accent: true },
        { time: '09:15', text: '触发闸门 <b>GATE-01 / GATE-02</b>（价格 / 交期），等待人工签核', accent: true }
      ]
    },
    {
      id: 'T-1043', company: 'Gulf Trading Partners LLC', country: '阿联酋', flag: 'AE', lang: 'ar',
      stage: 'INQUIRY', risk: 'high', state: 'blocked', sla: 0, wait: 3, intent: ['price', 'sample'],
      title: '索取 M-77 样品与最低报价',
      draft: null,
      citations: [],
      commitments: [],
      open: ['等待合规人工判定'],
      deadlines: [{ label: '合规复核', remain: '已超时 3 分钟', level: 'over' }],
      gates: [
        { id: 'GATE-03', title: '制裁 / 管制名单命中', desc: '最终用户命中受控条目 CN-2211（名单版本 sanctions-2026.11-a）', hit: true }
      ],
      evidence: [
        { k: '条目 ID', v: 'CN-2211' },
        { k: '命中字段', v: '最终用户名称（相似度 0.93）' },
        { k: '名单类型', v: '出口管制 · 受控最终用户' },
        { k: '名单版本', v: 'sanctions-2026.11-a' },
        { k: '核验时间', v: '2026-11-17 09:02:41 UTC' }
      ],
      messages: [
        { who: 'buyer', name: 'خالد الرشيد', time: '今天 09:02', lang: 'ar', text: 'مرحبا، نحن مستوردون من الإمارات. نطلب عينة من الموديل M-77 ونحتاج شهادة المطابقة. ما هو السعر للكمية 3000؟', attach: 'M-77_photo.jpg' }
      ],
      timeline: [
        { time: '09:02', text: '买家来信（ar，RTL）· 附件 1 个', accent: false },
        { time: '09:02', text: 'Sentinel 命中受控条目 <b>CN-2211</b>', accent: true },
        { time: '09:02', text: '<b>已抑制 Inquiry 输出</b>——命中状态下不生成任何回复', accent: true },
        { time: '09:05', text: '闸门 <b>GATE-03</b> 等待人工判定（默认不放行）', accent: false }
      ]
    },
    {
      id: 'T-1044', company: 'Northwind Instruments Inc.', country: '美国', flag: 'US', lang: 'en',
      stage: 'INQUIRY', risk: 'low', state: 'ready', sla: 240, wait: 2, intent: ['spec'],
      title: '询问 M-77 材质与吊装承重',
      draft: {
        lang: 'en',
        body: 'Hi Mark,\n\nThanks for your enquiry about the M-77 lifting clamp. The body is cast steel with a nylon-roller jaw, rated safe working load 500 kg (test certificate <mark>TC-M77-118</mark>, valid until 2026-08-12 — a re-test is due, so we will attach the renewed report before shipment).\n\nMinimum order quantity is 100 pcs; delivery is 20–25 days after order confirmation.\n\nPlease confirm the working load you need and we will confirm certification.',
        intent: ['规格'], confidence: 0.79,
        next: '证书临近到期，发送前需附换证后的报告编号'
      },
      citations: [
        { id: 'SKU M-77 v1', type: '产品条目', note: '铸钢 · 安全载荷 500kg · MOQ 100' },
        { id: 'TC-M77-118', type: '证书', note: '有效期至 2026-08-12 · 临近到期', expiring: true },
        { id: 'TERM-SWL', type: '术语表', note: 'Safe working load / 额定起重量' }
      ],
      commitments: ['交期参考 20–25 天（未签核）'],
      open: ['买家未说明所需安全载荷', '换证报告编号待补充'],
      deadlines: [{ label: '首次响应 SLA', remain: '4 小时', level: 'ok' }],
      gates: [
        { id: 'GATE-05', title: '认证声明', desc: '引用 TC-M77-118（距到期 4 个月）', hit: false },
        { id: 'GATE-02', title: '交期承诺', desc: '草稿含交期 20–25 天', hit: true }
      ],
      evidence: [{ k: '名单版本', v: 'sanctions-2026.11-a' }, { k: '核验结果', v: '未命中' }, { k: '命中字段', v: '—' }],
      messages: [
        { who: 'buyer', name: 'Mark Ellison', time: '今天 09:10', lang: 'en', text: 'Hi, could you tell me the material of the M-77 clamp and its safe working load? We need a lifting application. Also is CE/ISO certification available?' }
      ],
      timeline: [
        { time: '09:10', text: '买家来信（en）', accent: false },
        { time: '09:11', text: 'Sentinel 核验通过', accent: false },
        { time: '09:11', text: 'Inquiry 生成草稿 · 置信度 <b>0.79</b>', accent: true },
        { time: '09:12', text: '闸门 <b>GATE-02</b>（交期）待签核', accent: true }
      ]
    },
    {
      id: 'T-1046', company: 'Meridian Home Goods Ltd', country: '英国', flag: 'UK', lang: 'en',
      stage: 'INQUIRY', risk: 'low', state: 'needs_confirmation', sla: 95, wait: 14, intent: ['price'],
      title: '"Send me your best price."（信息不足）',
      draft: {
        lang: 'en',
        intent: ['询价'], confidence: 0.41,
        body: 'Hi,\n\nHappy to help with pricing. To quote accurately, could you confirm:\n1) which model / SKU you need\n2) target quantity\n3) destination port and Incoterm\n\nOnce confirmed we will reply with a reference price range and lead time.',
        next: '置信度 0.41 低于阈值 0.55 —— 只发追问，不给实质报价'
      },
      citations: [],
      commitments: [],
      open: ['型号', '数量', '目的港与贸易术语'],
      deadlines: [{ label: '首次响应 SLA', remain: '1 小时 35 分', level: 'warn' }],
      gates: [{ id: 'GATE-06', title: '置信度低于阈值', desc: '检索证据不足（无产品匹配）', hit: true }],
      evidence: [{ k: '名单版本', v: 'sanctions-2026.11-a' }, { k: '核验结果', v: '未命中' }, { k: '命中字段', v: '—' }],
      messages: [
        { who: 'buyer', name: 'Priya Nair', time: '今天 09:00', lang: 'en', text: 'Send me your best price.' }
      ],
      timeline: [
        { time: '09:00', text: '买家来信（en）· 无附件', accent: false },
        { time: '09:00', text: 'Sentinel 核验通过', accent: false },
        { time: '09:01', text: 'Inquiry 意图=<b>询价</b>，但无可用产品证据', accent: false },
        { time: '09:01', text: '置信度 <b>0.41</b> &lt; 阈值 0.55 → 输出<b>追问</b>，不给报价', accent: true }
      ]
    },
    {
      id: 'T-1048', company: 'Coastal Imports Inc.', country: '加拿大', flag: 'CA', lang: 'en',
      stage: 'SAMPLING', risk: 'low', state: 'ready', sla: 600, wait: 22, intent: ['sample', 'logistics'],
      title: '样品已签收，索取批量报价与订舱信息',
      draft: {
        lang: 'en',
        body: 'Hi Sarah,\n\nGreat to hear the samples arrived safely. For 10,000 pcs we can offer a reference price of USD 4.60–4.95/pc FOB Ningbo; sea freight via COSCO is approx. USD 780 per 40HQ to Vancouver, transit around 22 days.\n\nPlease confirm the split of the order into 2 shipments if helpful for your cash flow.',
        intent: ['打样', '物流'], confidence: 0.83,
        next: '涉及订舱与运费，签核后发送'
      },
      citations: [
        { id: 'SKU X-200 v2', type: '产品条目', note: '阶梯价 MOQ 1000 / 10000' },
        { id: 'SHIP-77', type: '内部记录', note: '样品签收 11-14 · 批次 S-2210' }
      ],
      commitments: ['样品已签收（11-14）', '参考价未签核'],
      open: ['是否分两批发货'],
      deadlines: [{ label: '跟进 SLA', remain: '10 小时', level: 'ok' }],
      gates: [{ id: 'GATE-01', title: '价格承诺', desc: '草稿含阶梯参考价', hit: true }],
      evidence: [{ k: '名单版本', v: 'sanctions-2026.11-a' }, { k: '核验结果', v: '未命中' }, { k: '命中字段', v: '—' }],
      messages: [
        { who: 'buyer', name: 'Sarah Whitfield', time: '昨天 16:20', lang: 'en', text: 'Samples arrived safely, thanks. Now please quote 10,000 pcs FOB Ningbo and let us know the sea freight to Vancouver.' }
      ],
      timeline: [
        { time: '昨天 16:20', text: '买家来信（en）', accent: false },
        { time: '昨天 16:21', text: 'Inquiry 生成草稿 · 置信度 <b>0.83</b>', accent: true },
        { time: '昨天 16:21', text: '闸门 <b>GATE-01</b>（价格）待签核', accent: true }
      ]
    },
    {
      id: 'T-1049', company: 'Ventura Comercial SA', country: '墨西哥', flag: 'MX', lang: 'es',
      stage: 'INQUIRY', risk: 'mid', state: 'human_takeover', sla: 0, wait: 118, intent: ['complaint'],
      title: '到货破损索赔（投诉）',
      draft: null,
      citations: [],
      commitments: ['已承诺 11-05 前发出补发件（实际 11-09 发出，已延误）'],
      open: ['买家未确认补发件是否收到', '赔付方案待定'],
      deadlines: [{ label: '投诉响应', remain: '已超时 118 分钟', level: 'over' }],
      gates: [{ id: 'GATE-09', title: '投诉意图', desc: '默认转人工，Agent 不自动应答', hit: true }],
      evidence: [{ k: '名单版本', v: 'sanctions-2026.11-a' }, { k: '核验结果', v: '未命中' }, { k: '命中字段', v: '—' }],
      messages: [
        { who: 'buyer', name: 'Luis Herrera', time: '昨天 11:30', lang: 'es', text: 'La mercancía llegó dañada: 12 cajas Mercancía dañada. Adjunto fotos. Necesito una solución urgente.' }
      ],
      timeline: [
        { time: '昨天 11:30', text: '买家来信（es，投诉意图）· 附件 3 张', accent: false },
        { time: '昨天 11:30', text: 'Intent=<b>投诉</b> → 规则 <b>GATE-09</b> 转人工', accent: true },
        { time: '昨天 13:02', text: '业务员 <b>李静</b> 接管线程，Agent 转为仅提供建议', accent: true }
      ]
    },
    {
      id: 'T-1050', company: 'Atlas Hardware GmbH', country: '德国', flag: 'DE', lang: 'en',
      stage: 'ORDERED', risk: 'low', state: 'sent', sla: 0, wait: 3, intent: ['price'],
      title: '订单已确认，等待 PI 签回',
      draft: {
        lang: 'en',
        body: 'Hi Jonas,\n\nThank you for confirming. Your order for 2,000 pcs X-200 is recorded (reference SO-2026-1187). The proforma invoice is attached; payment terms are 30% T/T deposit, 70% against copy of B/L. Production starts on receipt of the deposit.',
        intent: ['询价'], confidence: 0.88, next: '已发送，等待买家签回 PI'
      },
      citations: [
        { id: 'SO-2026-1187', type: '订单', note: '2,000 pcs X-200 · 30% T/T 定金' },
        { id: 'PI-2026-1187', type: '单据', note: '形式发票 · 待签回' }
      ],
      commitments: ['交期 30 天', '付款条件 30% T/T 定金'],
      open: ['买家签回 PI'],
      deadlines: [{ label: '订单跟进', remain: '明天', level: 'ok' }],
      gates: [],
      evidence: [{ k: '名单版本', v: 'sanctions-2026.11-a' }, { k: '核验结果', v: '未命中' }, { k: '命中字段', v: '—' }],
      messages: [
        { who: 'buyer', name: 'Jonas Weber', time: '昨天 08:40', lang: 'en', text: 'Confirmed — please send the PI so we can arrange the deposit.' }
      ],
      timeline: [
        { time: '昨天 08:40', text: '买家来信（en）', accent: false },
        { time: '昨天 08:41', text: '草稿通过签核并发送', accent: true },
        { time: '昨天 08:41', text: '线程阶段 → <b>ORDERED</b>，记录已承诺事项 2 条', accent: false }
      ]
    },
    {
      id: 'T-1051', company: 'Andes Textiles SpA', country: '智利', flag: 'CL', lang: 'es',
      stage: 'INQUIRY', risk: 'low', state: 'ready', sla: 1500, wait: 4320, intent: ['spec'],
      title: '3 天前留言，跨天续接（M-77 材质）',
      draft: {
        lang: 'es',
        body: 'Hola,\n\nRegarding the M-77 clamp: the body is cast steel with a nylon-roller jaw, rated 500 kg. The minimum order quantity is 100 pcs and delivery is 20–25 days after order confirmation.\n\nDo you have the ISO 9001 certificate of the plant?',
        intent: ['规格'], confidence: 0.85, next: '已从线程状态续接，未重复回答上次问题'
      },
      citations: [
        { id: 'SKU M-77 v1', type: '产品条目', note: '铸钢 · SWL 500kg · MOQ 100' },
        { id: 'CERT-ISO-9001', type: '证书', note: '工厂资质 · 有效期至 2027-01-15' }
      ],
      commitments: ['交期参考 20–25 天（未签核）'],
      open: ['买家是否需要海运拼箱报价'],
      deadlines: [{ label: '首次响应 SLA', remain: '25 小时（昨天已超时）', level: 'over' }],
      gates: [{ id: 'GATE-02', title: '交期承诺', desc: '草稿含交期 20–25 天', hit: true }],
      evidence: [{ k: '名单版本', v: 'sanctions-2026.11-a' }, { k: '核验结果', v: '未命中' }, { k: '命中字段', v: '—' }],
      messages: [
        { who: 'buyer', name: 'Camila Rojas', time: '3 天前 14:05', lang: 'es', text: 'Hola, estamos interesados en el M-77. ¿Qué material tiene? Adjunto la especificación que nos enviaron.' }
      ],
      timeline: [
        { time: '3 天前 14:05', text: '买家来信（es）· 附件 1 个', accent: false },
        { time: '3 天前 14:06', text: 'Sentinel 核验通过', accent: false },
        { time: '3 天前 14:06', text: '草稿生成但线程阶段停留 INQUIRY，<b>未签核</b>', accent: false },
        { time: '今天 09:12', text: '系统重启后<b>从 ThreadState 恢复该线程</b>（未丢失上下文）', accent: true }
      ]
    }
  ];

  const kpis = {
    pending: 12, slaSoon: 3, costToday: 1.86, budget: 2.4,
    sent: 41, avgFirstResponse: '3 分 12 秒', draftReady: 78
  };

  const stages = [
    { key: 'INQUIRY', name: '询盘', n: 18, color: '#2563EB' },
    { key: 'SAMPLING', name: '样品', n: 7, color: '#0891B2' },
    { key: 'QUOTING', name: '报价', n: 6, color: '#7C3AED' },
    { key: 'ORDERED', name: '订单', n: 4, color: '#059669' },
    { key: 'DOCS', name: '单证', n: 2, color: '#B45309' },
    { key: 'PAYMENT', name: '收款', n: 1, color: '#475569' }
  ];

  const quality = [
    { lang: 'en', label: '英语', top5: 0.91, cross: 0.78, f1: 0.94, cite: '100%', spec: '99.4%', inject: '100%', recall: '99.8%' },
    { lang: 'es', label: '西语', top5: 0.88, cross: 0.74, f1: 0.92, cite: '100%', spec: '99.1%', inject: '100%', recall: '99.6%' },
    { lang: 'ar', label: '阿语', top5: 0.78, cross: 0.59, f1: 0.87, cite: '100%', spec: '97.6%', inject: '100%', recall: '99.2%' },
    { lang: 'zh', label: '中文（对照）', top5: 0.93, cross: 0.81, f1: 0.95, cite: '100%', spec: '99.6%', inject: '100%', recall: '99.9%' }
  ];

  const costByLang = [
    { lang: 'en', n: 41, y: 0.041 },
    { lang: 'es', n: 33, y: 0.052 },
    { lang: 'ar', n: 18, y: 0.083 }
  ];

  const costTrend = [
    { d: '11-11', v: 0.058 }, { d: '11-12', v: 0.061 }, { d: '11-13', v: 0.055 },
    { d: '11-14', v: 0.063 }, { d: '11-15', v: 0.057 }, { d: '11-16', v: 0.052 }, { d: '11-17', v: 0.046 }
  ];

  const riskEvents = [
    { time: '09:02:41', type: '名单命中', level: 'high', text: 'T-1043 最终用户命中受控条目 CN-2211，已抑制输出' },
    { time: '08:47:12', type: '注入拦截', level: 'high', text: 'T-1045 买家消息含指令型文本「ignore previous instructions」，已阻断转人工' },
    { time: '08:31:02', type: '降级', level: 'mid', text: '法规库版本过期（reg-2026.09-b），相关话术转人工' },
    { time: '08:12:55', type: '超时升级', level: 'mid', text: 'T-1049 投诉线程超时 118 分钟，已升级至经理' },
    { time: '昨天 17:40', type: '模型降级', level: 'low', text: '云 API 限流，2 条草稿降级到轻量档位重生成' }
  ];

  const gates = [
    { name: 'GATE-01 价格承诺', desc: '草稿含价格/折扣/底价', must: true, state: '触发 3 条' },
    { name: 'GATE-02 交期承诺', desc: '草稿含交期/产能', must: true, state: '触发 2 条' },
    { name: 'GATE-03 名单命中', desc: '制裁/管制名单，默认不放行', must: true, state: '触发 2 条' },
    { name: 'GATE-04 合同条款', desc: '合同与签署必须人工', must: true, state: '触发 0 条' },
    { name: 'GATE-05 认证声明', desc: '抽样 + 引用校验', must: false, state: '触发 5 条' },
    { name: 'GATE-06 低置信', desc: '阈值 0.55', must: false, state: '触发 4 条' },
    { name: 'GATE-07 注入特征', desc: '买家消息当数据不当指令', must: false, state: '触发 1 条' },
    { name: 'GATE-08 歧义追问', desc: '信息不足时追问', must: false, state: '触发 3 条' },
    { name: 'GATE-09 投诉意图', desc: '默认转人工', must: false, state: '触发 1 条' },
    { name: 'GATE-10 法规时效冲突', desc: '目的国规则冲突', must: false, state: '触发 0 条' },
    { name: 'GATE-11 金额阈值', desc: '超阈值/敏感客户', must: false, state: '触发 1 条' }
  ];

  /* 知识库页：检索证据块（ar/es/en 语料自建层） */
  const kbAssets = [
    { id: 'SKU X-200 v2', type: '产品条目', lang: 'zh', score: 0.912, valid: '长期', tags: ['MOQ 1000', '交期 25–30 天', 'HS 8479.90', '受控否'],
      body: 'X-200 手动液压搬运车：额定载荷 2 吨，泵体铸铁 + 尼龙轮，FOB 深圳，25–30 天交期，MOQ 1000，可出口受控品目 HS 8479.90。' },
    { id: 'CERT-CE-2041', type: '证书', lang: 'zh', score: 0.874, valid: '至 2027-03-31', tags: ['CE', 'EN 12195', '覆盖 X-200'],
      body: 'CE 符合性声明，适用型号 X-200/X-210，有效期至 2027-03-31，发证方：TÜV SÜD（编号 0086）。' },
    { id: 'REG-EU-112', type: '法规条款', lang: 'zh', score: 0.861, valid: '2026-01-01 生效', tags: ['欧盟', '机械指令 2006/42/EC', 'CE 标识'],
      body: '欧盟机械指令 2006/42/EC 下的 CE 标识与符合性声明要求；自 2026-01-01 起，进口商须留存技术文件 10 年。' },
    { id: 'TERM-MOQ', type: '术语表', lang: 'zh', score: 0.842, valid: '长期', tags: ['MOQ', 'es: cantidad mínima de pedido'],
      body: 'MOQ（最小起订量）：es 标准译 "cantidad mínima de pedido"；禁用译法 "pedido mínimo"、"MOQ mínimo"。' },
    { id: 'REG-US-204', type: '法规条款', lang: 'zh', score: 0.703, valid: '2027-01-01 生效（未生效）', tags: ['美国', 'CPSC 提案', '提案阶段'],
      body: 'CPSC 关于手动搬运车警示标签的拟议规则，提案阶段，预计 2027-01-01 生效——本期不得作为要求输出。' },
    { id: 'CERT-CE-1890', type: '证书', lang: 'zh', score: 0.688, valid: '已于 2025-11-30 废止', tags: ['CE', '过期', '禁止引用'], expired: true,
      body: '旧版 CE 证书，覆盖 X-190 系列，有效期至 2025-11-30，已被 CERT-CE-2041 取代。' },
    { id: 'SAN-CN-2211', type: '名单条目', lang: 'zh', score: 0.944, valid: 'sanctions-2026.11-a', tags: ['出口管制', '受控最终用户'],
      body: '受控最终用户条目：条目 ID CN-2211，命中名称 Gulf Trading Partners LLC，命中字段：最终用户名称。' }
  ];

  const terms = [
    { zh: '最小起订量', en: 'MOQ', es: 'cantidad mínima de pedido', ar: 'الحد الأدنى للطلب', forbid: 'pedido mínimo' },
    { zh: '装运港', en: 'Port of Loading', es: 'Puerto de carga', ar: 'ميناء التحميل', forbid: 'puerto de embarque' },
    { zh: '船上交货价', en: 'FOB', es: 'FOB (puerto de carga)', ar: 'فوب تسليم على ظهر السفينة', forbid: 'FOB Puerto' },
    { zh: '额定起重量', en: 'Safe working load', es: 'Carga de trabajo segura', ar: 'قدرة العمل الآمنة', forbid: 'carga segura' },
    { zh: '检验证书', en: 'Certificate of Inspection', es: 'Certificado de inspección', ar: 'شهادة الفحص', forbid: 'certificado de revis' }
  ];

  const pipeline = [
    { t: '语种识别与归一', d: 'RTL / 字符形态归一' },
    { t: '术语表映射', d: 'MOQ 等概念归一' },
    { t: '查询扩展', d: '同义词 + 业务别名' },
    { t: '属性过滤', d: '结构化参数精确匹配' },
    { t: '语义召回', d: '稠密 + 字面双通道' },
    { t: '融合重排', d: 'RRF + 跨语 cross-encoder' },
    { t: '引用装配', d: '带标识与有效期' }
  ];

  const demoScripts = [
    { id: 'T-1042', label: '脚本 A · 英语询价（引用与闸门）' },
    { id: 'T-1051', label: '脚本 B · 西语跨语种召回（中文产品库）' },
    { id: 'T-1043', label: '脚本 C · 阿语 + 受控名单（命中即不回复）' },
    { id: 'T-1046', label: '脚本 D · 歧义追问（不猜价格）' }
  ];

  return { STAGE, INTENT, threads, kpis, stages, quality, costByLang, costTrend, riskEvents, gates, kbAssets, terms, pipeline, demoScripts };
})();