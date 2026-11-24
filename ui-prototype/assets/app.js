/* TradeDesk AI Workbench — 交互脚本（原型：本地 mock 数据） */
(function () {
  // D 可重定向：真数据模式下会用后端读模型替换掉 mock 部分（S6-1b）。
  let D = window.TD;
  let currentSource = 'mock';   // mock | api
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));

  /* ---------- 图标（Phosphor 风格线性 SVG，无 emoji） ---------- */
  const ICONS = {
    inbox: '<path d="M3 12h5l2 3h4l2-3h5M3 12l2.5-7h13L21 12v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    chat: '<path d="M21 12a8 8 0 0 1-8 8H7l-4 3V12a8 8 0 0 1 8-8h2a8 8 0 0 1 8 8z"/>',
    chart: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
    database: '<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
    book: '<path d="M4 5a2 2 0 0 1 2-2h12v18H6a2 2 0 0 1-2-2z"/><path d="M8 3v18"/>',
    shield: '<path d="M12 3l7 3v5c0 4.5-3 8-7 10-4-2-7-5.5-7-10V6z"/><path d="M9 12l2 2 4-4"/>',
    warning: '<path d="M12 4l9 16H3z"/><path d="M12 10v4M12 17.5v.5"/>',
    clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7v5l3.5 2"/>',
    check: '<path d="M4 12.5l5 5L20 6.5"/>',
    x: '<path d="M6 6l12 12M18 6L6 18"/>',
    pencil: '<path d="M4 20h4L19 9a2.5 2.5 0 0 0-3.5-3.5L4.5 16.5z"/>',
    send: '<path d="M4 12l16-8-6 8 6 8z"/>',
    paperclip: '<path d="M20 11l-8.5 8.5a5 5 0 0 1-7-7L13 4a3.5 3.5 0 0 1 5 5l-8.5 8.5a2 2 0 0 1-3-3L14 7"/>',
    globe: '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c2.5 2.5 2.5 14 0 17M12 3.5c-2.5 2.5-2.5 14 0 17"/>',
    search: '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/>',
    bell: '<path d="M18 8a6 6 0 1 0-12 0c0 6-2 7-2 7h16s-2-1-2-7"/><path d="M10.5 20a2 2 0 0 0 3 0"/>',
    users: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><path d="M16 5.2a3.5 3.5 0 0 1 0 6.6M17.5 14.5A6.5 6.5 0 0 1 21.5 20"/>',
    lock: '<rect x="4.5" y="10" width="15" height="10" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',
    file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
    coins: '<ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v6c0 1.7 3.1 3 7 3s7-1.3 7-3V6"/><path d="M5 12v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6"/>',
    filter: '<path d="M3 5h18l-7 8v6l-4 2v-8z"/>',
    chevron: '<path d="M9 6l6 6-6 6"/>',
    caret: '<path d="M6 9l6 6 6-6"/>',
    arrowRight: '<path d="M4 12h15M13 6l6 6-6 6"/>',
    spark: '<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"/>',
    calendar: '<rect x="4" y="5" width="16" height="16" rx="2"/><path d="M4 10h16M9 3v4M15 3v4"/>',
    translate: '<path d="M4 6h10M9 4v2c0 4-2 7-5 8"/><path d="M6 11c1.5 3 4 5 7 5.5"/><path d="M14 20l4-9 4 9M15.6 17h4.8"/>'
  };
  function icon(name, cls) {
    return '<svg class="icon ' + (cls || '') + '" viewBox="0 0 24 24" aria-hidden="true" focusable="false">' + (ICONS[name] || '') + '</svg>';
  }
  window.TD.icon = icon;

  const STATE_LABEL = {
    needs_signoff: ['待签核', 'badge-warning'],
    blocked: ['名单阻断', 'badge-danger'],
    ready: ['草稿待确认', 'badge-info'],
    needs_confirmation: ['需人工确认', 'badge-warning'],
    human_takeover: ['人工接管', 'badge-neutral'],
    sent: ['已发送', 'badge-success']
  };
  const RISK_LABEL = { low: '低风险', mid: '中风险', high: '高风险' };
  const INTENTS = { price: '询价', sample: '打样', spec: '规格', logistics: '物流', complaint: '投诉' };

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  }

  /* ---------- Toast ---------- */
  function toast(msg, kind) {
    let host = $('.toast-host');
    if (!host) { host = document.createElement('div'); host.className = 'toast-host'; document.body.appendChild(host); }
    const el = document.createElement('div');
    el.className = 'toast' + (kind ? ' ' + kind : '');
    el.setAttribute('role', 'status');
    el.innerHTML = icon(kind === 'danger' ? 'warning' : 'check') + '<span>' + esc(msg) + '</span>';
    host.appendChild(el);
    setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity 240ms ease'; setTimeout(() => el.remove(), 260); }, 3600);
  }
  window.TD.toast = toast;
  D.toast = toast;

  /* ================= 身份与角色可见域（S6-4） ================= */
  let USER = null;

  function applyUser(u) {
    USER = u;
    if (!u) return;
    document.body.classList.add('role-' + u.role);
    const el = document.getElementById('current-user');
    if (el) el.innerHTML = '当前用户：<b>' + esc(u.name) + '</b>（' + esc(u.role_name) + '）';
    const so = document.getElementById('signout');
    if (so && !so.dataset.bound) {
      so.dataset.bound = '1';
      so.addEventListener('click', function () {
        if (window.TDAuth) window.TDAuth.logout();
      });
    }
  }

  /* ================= 数据源（mock / api） ================= */
  function setSourceBadge(text, kind) {
    const el = $('#data-source');
    if (!el) return;
    el.textContent = text;
    el.className = 'hint' + (kind ? ' badge-' + kind : '');
  }

  async function reloadLive() {
    const live = await window.TDApi.loadWorkbench();
    // api 模式**不要**把 mock 数据合并进来（R21-03 / B5）。
    // 原来 `Object.assign({}, D, live)` 会把 mock 独有的字段（pipeline / gates /
    // riskEvents / kbAssets / terms …）原样留在 D 里 —— 后端没给的字段，界面却显示着
    // mock 的内容。那是"界面说谎"，比缺一块更糟。
    // mock 模式才合并（那时 live 可能是空壳）。
    D = currentSource === 'api' ? live : Object.assign({}, D, live);
    window.TD = D;
    D.toast = toast;
    syncKpis();
    return D;
  }

  async function loadSource() {
    const cfg = window.TD_CONFIG || {};
    let src = cfg.source || 'mock';
    const m = /[?&]source=(mock|api)/.exec(window.location.search || '');
    if (m) src = m[1];
    if (src !== 'api' || !window.TDApi) {
      currentSource = 'mock';
      setSourceBadge('演示数据（mock）');
      return 'mock';
    }
    try {
      // 先把来源标成 api，再取数 —— reloadLive 靠它决定"合不合并 mock"
      currentSource = 'api';
      await reloadLive();
      setSourceBadge('真实数据（api）', 'success');
      return 'api';
    } catch (err) {
      currentSource = 'mock';
      setSourceBadge('后端不可达，已降级为演示数据', 'danger');
      toast('后端不可达，已降级为演示数据：' + err.message, 'danger');
      return 'mock-fallback';
    }
  }

  /* ================= 工作台 ================= */
  const state = {
    // 不在模块加载时取第一个线程：真数据模式下这时后端还没拿到数据。
    selected: null,
    filters: { stage: 'all', lang: 'all', risk: 'all', wait: 'all' },
    q: '',
    pending: 0,
    cost: 0
  };

  function syncKpis() {
    state.pending = (D.kpis && D.kpis.pending) || 0;
    state.cost = (D.kpis && D.kpis.costToday) || 0;
    // 口径说明渲染到数据来源标识旁（B6）：读模型给的 notes 必须在界面上看得见。
    // 用 querySelector 而不是 getElementById：审核脚本的 DOM 桩只实现了 querySelector。
    const src = document.querySelector('#data-source');
    if (src && D.notes && D.notes.length) {
      src.textContent = (src.textContent || '').replace(/\s*·\s*口径说明[\s\S]*$/, '') +
        ' · 口径说明：' + D.notes.join('；');
    }
  }

  // 名单命中条目 ID：只从证据链取（R21-02）。
  // 写死演示条目会让风控界面对外失真 —— 合规岗看到"命中 CN-2211"，
  // 实际命中的可能是 CN-2307，两个不同主体、不同的一致性程序。
  // 取当前界面上的草稿正文（业务员可能已在界面上改过）。
  function currentDraftBody(t) {
    const node = document.querySelector('.draft-body');
    const fromDom = node && node.textContent ? node.textContent.trim() : '';
    return fromDom || (t.draft && t.draft.body) || '';
  }

  function blockedEntryId(t) {
    const ev = (t.evidence || []).find(e => /^CN-\d+$/.test(e.v || ''));
    return ev ? ev.v : '';
  }

  function matchThread(t) {
    const f = state.filters;
    if (f.stage !== 'all' && t.stage !== f.stage) return false;
    if (f.lang !== 'all' && t.lang !== f.lang) return false;
    if (f.risk !== 'all' && t.risk !== f.risk) return false;
    // 第 4 类筛选：等待人审时长（PRD FR-ORC-10）。后端已给 t.wait（分钟）。
    if (f.wait !== 'all') {
      const w = Number(t.wait) || 0;
      if (f.wait === 'lt30' && w >= 30) return false;
      if (f.wait === '30to240' && (w < 30 || w > 240)) return false;
      if (f.wait === 'gt240' && w <= 240) return false;
    }
    if (state.q) {
      const q = state.q.toLowerCase();
      if (!(t.company.toLowerCase().includes(q) || t.title.toLowerCase().includes(q) || t.id.toLowerCase().includes(q) || t.country.includes(q))) return false;
    }
    return true;
  }

  function threadItem(t) {
    const slaCls = t.deadlines[0] && t.deadlines[0].level === 'over' ? 'sla-over' : (t.sla && t.sla <= 60 ? 'sla-warn' : 'sla-ok');
    const st = STATE_LABEL[t.state] || ['—', 'badge-neutral'];
    const intent = (t.intent || []).map(i => '<span class="badge badge-neutral">' + esc(INTENTS[i] || i) + '</span>').join('');
    return '' +
      '<li><button class="thread" role="option" aria-selected="' + (t.id === state.selected) + '" data-id="' + t.id + '">' +
        '<div class="thread-top">' +
          '<span class="risk-dot risk-' + t.risk + '" title="' + RISK_LABEL[t.risk] + '"></span>' +
          '<span class="thread-co">' + esc(t.company) + '</span>' +
          '<span class="badge badge-lang lang-' + t.lang + '" style="margin-left:auto">' + t.lang.toUpperCase() + '</span>' +
        '</div>' +
        '<div class="thread-title">' + esc(t.title) + '</div>' +
        '<div class="thread-meta">' +
          '<span>' + t.id + '</span><span>' + esc(t.country) + '</span>' +
          '<span class="badge ' + st[1] + '">' + st[0] + '</span>' +
        '</div>' +
        '<div class="thread-foot">' + intent +
          '<span class="sla ' + slaCls + '" style="margin-left:auto">' + icon('clock', 'icon-sm') +
            (t.deadlines[0] ? esc(t.deadlines[0].remain) : '—') + '</span>' +
        '</div>' +
      '</button></li>';
  }

  function renderThreads() {
    const list = $('#thread-list');
    if (!list) return;
    const items = D.threads.filter(matchThread);
    list.innerHTML = items.length ? items.map(threadItem).join('')
      : '<li style="padding:16px 12px;color:var(--color-muted-foreground);font-size:12px">没有符合条件的线程</li>';
    $('#thread-count').textContent = items.length + ' / ' + D.threads.length;
    $$('.thread', list).forEach(b => b.addEventListener('click', () => { state.selected = b.dataset.id; renderThreads(); renderConversation(); renderReview(); }));
  }

  function msgHTML(m) {
    const isAr = m.lang === 'ar';
    const tr = isAr ? '买家原文（阿拉伯语 · RTL）' : (m.lang === 'es' ? '买家原文（西班牙语）' : (m.lang === 'en' ? '买家原文（英语）' : '买家原文'));
    // 固定译文只用于 mock 演示（R21-04）：真数据模式下 m.lang 仍是 en/es/ar，
    // 于是每条真消息下面都挂着一段**与本次消息无关**的固定译文 ——
    // 业务员会据此理解买家意图，错了会一路带到报价。
    // 真数据模式的译文由后端给（后端没给就不显示这一行）。
    const zh = currentSource === 'api' ? null : {
      es: '我们是一家巴西经销商。能否为 X-200 提供 5000 台 FOB 深圳的报价？另外想确认该型号是否有 CE 认证以及交货期。附件为技术图纸。',
      en: '你好，能告诉我 M-77 夹具的材质和安全工作载荷吗？我们用于吊装场景。另外是否有 CE/ISO 认证？',
      ar: '你好，我们是从阿联酋进口的。我们需要 M-77 型号的样品和符合性证书。3000 件的价格是多少？'
    }[m.lang];
    return '' +
      '<div class="msg">' +
        '<div class="msg-role">' +
          '<span class="avatar avatar-buyer">' + esc(m.name.slice(0, 1)) + '</span>' +
          '<span class="msg-name">' + esc(m.name) + '</span>' +
          '<span class="badge badge-neutral">' + m.lang.toUpperCase() + '</span>' +
          '<span class="msg-time">' + esc(m.time) + '</span>' +
        '</div>' +
        '<div class="bubble' + (isAr ? ' bubble-ar' : '') + '">' +
          '<div class="sr-only">' + tr + '</div>' +
          esc(m.text) +
          (m.attach ? '<div class="attach">' + icon('paperclip', 'icon-sm') + esc(m.attach) + '</div>' : '') +
          (zh ? '<div class="trans">' + icon('translate', 'icon-sm') + ' 中文理解：' + esc(zh) + '</div>' : '') +
        '</div>' +
      '</div>';
  }

  function renderConversation() {
    const t = D.threads.find(x => x.id === state.selected);
    const conv = $('#conversation');
    if (!t || !conv) return;
    const stage = D.STAGE[t.stage];
    const st = STATE_LABEL[t.state] || ['—', 'badge-neutral'];

    let html = '<div class="conv-head">' +
      '<div class="conv-title">' +
        '<h2>' + esc(t.company) + '</h2>' +
        '<span class="badge badge-lang lang-' + t.lang + '">' + t.lang.toUpperCase() + '</span>' +
        '<span class="badge badge-neutral">' + stage + '</span>' +
        '<span class="badge ' + st[1] + '">' + st[0] + '</span>' +
        '<span class="badge ' + (t.risk === 'high' ? 'badge-danger' : t.risk === 'mid' ? 'badge-warning' : 'badge-success') + '">' + RISK_LABEL[t.risk] + '</span>' +
      '</div>' +
      '<div class="conv-sub">' + t.id + ' · ' + esc(t.country) + ' · ' + esc(t.title) + '</div>' +
      '<div class="ctx-strip">' +
        '<div class="ctx-cell"><div class="k">' + icon('check', 'icon-sm') + ' 已承诺事项</div><div class="v">' +
          (t.commitments.length ? t.commitments.map(esc).join('<br>') : '<span style="color:var(--color-muted-foreground)">暂无</span>') + '</div></div>' +
        '<div class="ctx-cell"><div class="k">' + icon('chat', 'icon-sm') + ' 未决问题</div><div class="v">' +
          (t.open.length ? t.open.map(esc).join('<br>') : '<span style="color:var(--color-muted-foreground)">暂无</span>') + '</div></div>' +
        '<div class="ctx-cell"><div class="k">' + icon('clock', 'icon-sm') + ' 时限</div><div class="v">' +
          t.deadlines.map(d => '<span class="sla ' + (d.level === 'over' ? 'sla-over' : d.level === 'warn' ? 'sla-warn' : 'sla-ok') + '">' + esc(d.label) + '：' + esc(d.remain) + '</span>').join('<br>') + '</div></div>' +
      '</div>' +
      '</div>';

    html += t.messages.map(msgHTML).join('');

    /* 草稿 / 阻断 / 追问 三种形态 */
    if (t.state === 'blocked') {
      html += '<div class="draft"><div class="draft-hd">' + icon('shield', 'icon-sm') + '<h3>Sentinel 已阻断，未生成任何回复</h3></div>' +
        '<div class="blocked"><h3>' + icon('warning') + '名单命中 · 系统保持沉默</h3>' +
        '<p>该买家的最终用户命中出口管制受控条目 <b>' + esc(blockedEntryId(t) || '（条目 ID 未在证据链中给出）') + '</b>。按照 GATE-03，Inquiry 的输出已被抑制：不生成报价、交期，也不发出任何问候。' +
        '只有人工在证据链上做出书面判定后，线程才会继续。UI 默认不放行。</p></div></div>';
    } else if (t.draft) {
      const d = t.draft;
      const confColor = d.confidence >= 0.7 ? 'var(--color-success)' : 'var(--color-warning)';
      html += '<div class="draft draft-body"><div class="draft-hd">' + icon('spark', 'icon-sm') + '<h3>Inquiry 草稿</h3>' +
        '<span class="badge badge-neutral">' + esc(d.lang.toUpperCase()) + ' 输出</span>' +
        '<span class="badge badge-info">置信度 ' + d.confidence.toFixed(2) + '</span>' +
        '<span class="badge badge-neutral">' + d.intent.map(i => esc(i)).join(' / ') + '</span>' +
        '</div>' +
        // 正文转义（R21-01）。要保留换行就用 CSS white-space:pre-wrap，
        // 不要用 <br> 拼接未转义内容。
        '<div class="draft-body">' + esc(d.body) + '</div>' +
        (t.citations && t.citations.length ?
          '<div class="citation">' + t.citations.map(c =>
            '<span class="cite' + (c.expiring ? ' expired' : '') + '" title="' + esc(c.note) + '">' + icon(c.type === '证书' ? 'file' : c.type === '产品条目' ? 'coins' : c.type === '术语表' ? 'book' : 'shield', 'icon-sm') + esc(c.id) +
            (c.expiring ? '（临近到期）' : '') + '</span>').join('') + '</div>'
          : '<div class="citation"><span style="font-size:12px;color:var(--color-muted-foreground)">无引用 —— 不允许发送</span></div>') +
        '<div class="meta-row">' +
          '<span style="font-size:12px;color:var(--color-muted-foreground)">置信度</span>' +
          '<span class="bar-track" style="width:140px"><span class="bar-fill" style="width:' + (d.confidence * 100) + '%;background:' + confColor + '"></span></span>' +
          '<span style="font-size:12px;font-variant-numeric:tabular-nums">' + d.confidence.toFixed(2) + '</span>' +
          '<span style="font-size:12px;color:var(--color-muted-foreground)">｜建议动作：' + esc(d.next) + '</span>' +
        '</div>';
      if (t.state === 'needs_confirmation') {
        html += '<div class="conflict low" style="margin-top:12px">' + icon('warning', 'icon-sm') +
          '<span><b>置信度低于阈值 0.55</b> —— 按兜底规则只输出追问，不猜测规格与价格。Agent 的价值之一就是"不知道"也能给出可用输出。</span></div>' +
          '<div class="suggest">' + ['型号 / SKU？', '目标数量？', '目的港与贸易术语？'].map(q => '<button class="chip" data-ask="' + esc(q) + '">' + esc(q) + '</button>').join('') + '</div>';
      }
      html += '</div>';
    } else {
      html += '<div class="draft"><div class="conflict">' + icon('users', 'icon-sm') +
        '<span><b>人工接管中</b> —— 业务员 李静 已接管该线程，Inquiry 仅在后台提供建议，不再主动发送任何内容。</span></div></div>';
    }

    const el = $('#conversation');
    el.innerHTML = html;
    $$('.cite', el).forEach(c => c.addEventListener('click', () => {
      if (c.classList.contains('expired')) { toast('该证书已临近到期，发送前需附换证后的报告编号', 'warn'); return; }
      toast('引用 ' + c.textContent.trim() + ' · ' + (c.getAttribute('title') || ''));
    }));
    $$('[data-ask]', el).forEach(b => b.addEventListener('click', () => {
      toast('追问已加入草稿（演示）：' + b.dataset.ask);
      const body = $('.draft-body', el);
      if (body) body.textContent += '\n· ' + b.dataset.ask;
    }));
  }

  function renderReview() {
    const t = D.threads.find(x => x.id === state.selected);
    const host = $('#review');
    if (!t || !host) return;

    let html = '<div class="panel-hd"><h2>签核与风控</h2><span class="hint">' + icon('lock', 'icon-sm') + ' 写操作需闸门令牌</span></div>' +
      '<div class="panel-body">';

    html += '<div class="section-title">触发闸门</div><div class="gates">';
    if (!t.gates.length) {
      html += '<div class="gate"><span class="badge badge-success">' + icon('check', 'icon-sm') + '</span><div><div class="g-title">无强制闸门</div><div class="g-desc">该草稿不含价格、交期、合同或名单风险项</div></div></div>';
    }
    t.gates.forEach(g => {
      html += '<div class="gate' + (g.hit ? ' hit' : '') + '">' +
        '<span class="badge ' + (g.hit ? 'badge-danger' : 'badge-success') + '">' + (g.hit ? icon('warning', 'icon-sm') : icon('check', 'icon-sm')) + '</span>' +
        '<div><div class="g-title">' + esc(g.title) + ' <span style="color:var(--color-muted-foreground);font-weight:400">' + esc(g.id) + '</span></div>' +
        '<div class="g-desc">' + esc(g.desc) + '</div></div></div>';
    });
    html += '</div>';

    html += '<div class="section-title" style="margin-top:20px">Sentinel 证据链</div><div class="evidence">' +
      t.evidence.map(e => '<div class="evidence-row"><span class="k">' + esc(e.k) + '</span><span class="v">' + esc(e.v) + '</span></div>').join('') +
      '</div>';

    if (t.state === 'blocked') {
      html += '<div class="actions"><button class="btn btn-danger" data-act="block">维持阻断</button>' +
        '<button class="btn" data-act="escalate">提交合规复核</button>' +
        '<button class="btn btn-ghost" data-act="transfer">转交风控同事</button></div>';
    } else if (t.state === 'sent') {
      html += '<div class="actions"><span class="badge badge-success">' + icon('check', 'icon-sm') + ' 已发送并留痕</span></div>';
    } else if (t.state === 'human_takeover') {
      html += '<div class="actions"><button class="btn" data-act="transfer">转回 Agent 建议模式</button></div>';
    } else {
      html += '<div class="actions">' +
        '<button class="btn btn-primary" data-act="approve">' + icon('check', 'icon-sm') + ' 通过并发送</button>' +
        '<button class="btn" data-act="modify">' + icon('pencil', 'icon-sm') + ' 修改后通过</button>' +
        '<button class="btn btn-danger-ghost" data-act="reject">' + icon('x', 'icon-sm') + ' 驳回并说明原因</button>' +
        '<button class="btn btn-ghost" data-act="transfer">' + icon('users', 'icon-sm') + ' 转交</button>' +
        '</div>' +
        '<p style="font-size:11px;color:var(--color-muted-foreground);margin-top:10px">驳回原因会被归类并回灌提示词，是"人在环"产生复利的主要来源。</p>';
    }

    html += '<div class="section-title" style="margin-top:24px">审计时间线</div><ul class="timeline">';
    t.timeline.forEach(x => {
      // x.text 必须转义（R21-01）：转交原因可经 HTTP 接口任意写进事件流，
      // 不转义就是**存储型** XSS —— 写一次，所有打开该线程的人执行一次。
      html += '<li class="' + (x.accent ? 'is-accent' : '') + '"><div class="tl-time">' + esc(x.time) + '</div><div class="tl-text">' + esc(x.text) + '</div></li>';
    });
    html += '</ul></div>';
    host.innerHTML = html;

    $$('[data-act]', host).forEach(b => b.addEventListener('click', () => onAction(t, b.dataset.act)));
  }

  async function onAction(t, act) {
    if (currentSource === 'api' && window.TDApi) {
      return await onActionApi(t, act);
    }
    const map = {
      approve: ['签核通过，草稿已发送（演示）', ''],
      modify: ['已按你的修改发送（演示）', ''],
      reject: ['已驳回，原因将回灌提示词（演示）', 'warn'],
      transfer: ['已转交同事，Agent 停止主动发送（演示）', ''],
      block: ['维持阻断：未生成任何回复（演示）', 'danger'],
      escalate: ['已提交合规复核（演示）', 'warn']
    };
    const [msg, kind] = map[act] || ['已处理', ''];
    t.timeline.push({ time: '刚刚', text: '业务员 <b>李静</b> ' + esc(msg), accent: true });
    if (act === 'approve') { t.state = 'sent'; state.pending = Math.max(0, state.pending - 1); }
    if (act === 'reject') { t.state = 'needs_confirmation'; state.pending = Math.max(0, state.pending - 1); }
    if (act === 'transfer') t.state = 'human_takeover';
    $('#kpi-pending').textContent = state.pending;
    toast(msg, kind);
    renderThreads(); renderConversation(); renderReview();
  }

  async function onActionApi(t, act) {
    const draftId = t.draft_id;
    if (!draftId) { toast('该线程没有草稿，无法签核', 'warn'); return; }
    try {
      if (act === 'approve') {
        // 必须带上当前界面上的草稿正文（B14）：
        // 业务员在界面上改过（追问 chip 会改 .draft-body），不带上就会被静默丢弃，
        // 而 toast 还说"已签核并发送"——用户以为发的是改过的。
        await window.TDApi.signoff(t.id, draftId, 'approve', { body: currentDraftBody(t) });
        // 签核通过后发送是**两个写操作**，需要两张一次性闸门令牌。
        await window.TDApi.send(t.id, draftId);
        await reloadLive();
        toast('已签核并发送', '');
      } else if (act === 'transfer') {
        // FR-HUM-06 的四种动作之一：转交他人。
        // 转交后 Agent 停止主动发送，线程进入人工接管。
        const reason = window.prompt('转交原因（会进审计时间线，会回灌给接手同事）', '交由同事跟进');
        if (!reason) { toast('已取消转交', 'warn'); return; }
        await window.TDApi.signoff(t.id, draftId, 'transfer', { reason: reason });
        await reloadLive();
        toast('已转交同事，Agent 停止主动发送', '');
      } else if (act === 'modify') {
        await window.TDApi.signoff(t.id, draftId, 'modify', { body: t.draft && t.draft.body });
        await reloadLive();
        toast('已按原文签核通过（未修改内容）', '');
      } else if (act === 'reject') {
        const reason = window.prompt('驳回必须带原因（会被归类并回灌提示词），例如：价格不符');
        if (!reason) { toast('已取消驳回', 'warn'); return; }
        await window.TDApi.signoff(t.id, draftId, 'reject', { reason: reason });
        await reloadLive();
        toast('已驳回，原因将回灌提示词', 'warn');
      } else {
        toast('该动作在真数据模式下暂未开放（后端接口不支持）', 'warn');
      }
      renderThreads(); renderConversation(); renderReview();
    } catch (err) {
      toast('操作失败：' + err.message, 'danger');
    }
  }

  function initWorkbench() {
    if (!$('#thread-list')) return;
    syncKpis();
    if (!state.selected || !D.threads.some(t => t.id === state.selected)) {
      state.selected = D.threads.length ? D.threads[0].id : null;
    }
    renderThreads(); renderConversation(); renderReview();

    $('#kpi-pending').textContent = D.kpis.pending;
    $('#kpi-sla').textContent = D.kpis.slaSoon;
    $('#kpi-cost').textContent = '¥' + D.kpis.costToday.toFixed(2);

    $$('.filter-row').forEach(row => {
      row.addEventListener('click', e => {
        const chip = e.target.closest('.chip');
        if (!chip) return;
        const group = chip.parentElement.dataset.group;
        state.filters[group] = chip.dataset.value;
        $$('.chip', row).forEach(c => c.setAttribute('aria-pressed', String(c === chip)));
        renderThreads();
      });
    });
    $('#thread-search').addEventListener('input', e => { state.q = e.target.value.trim(); renderThreads(); });

    const sel = $('#demo-script');
    if (sel) {
      const opts = currentSource === 'api'
        ? window.TDApi.scenarios.map(s => ({ id: s.id, label: s.label, payload: s.payload }))
        : (D.demoScripts || []).map(s => ({ id: s.id, label: s.label }));
      sel.innerHTML = opts.map(s => '<option value="' + esc(s.id) + '">' + esc(s.label) + '</option>').join('');
      sel.addEventListener('change', async e => {
        const picked = opts.find(o => o.id === e.target.value);
        if (currentSource === 'api' && picked && picked.payload) {
          try {
            const r = await window.TDApi.inbound(picked.payload);
            await reloadLive();
            state.selected = r.thread_id;
            renderThreads(); renderConversation(); renderReview();
            toast('已真实发起一条询盘 · ' + r.thread_id);
          } catch (err) {
            toast('发起询盘失败：' + err.message, 'danger');
          }
          return;
        }
        state.selected = e.target.value;
        renderThreads(); renderConversation(); renderReview();
        toast('已切换到演示线程 ' + e.target.value);
      });
    }

    $('#thread-list').addEventListener('keydown', e => {
      if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
      const ids = D.threads.filter(matchThread).map(t => t.id);
      const i = ids.indexOf(state.selected);
      const next = e.key === 'ArrowDown' ? Math.min(ids.length - 1, i + 1) : Math.max(0, i - 1);
      if (ids[next]) { state.selected = ids[next]; renderThreads(); renderConversation(); renderReview(); $('#thread-list').querySelector('[aria-selected="true"]').focus(); }
      e.preventDefault();
    });
  }

  /* ================= 运营看板 ================= */
  function initDashboard() {
    const s = $('#stage-chart');
    if (!s) return;
    // 顶栏 KPI 一律读模型（B11）：原来四个 KPI 是写死的 mock 数字，
    // 真数据模式下没人更新它们 —— 页面会一直显示"今日已发送 41 / 草稿可用率 78%"。
    const K = D.kpis || {};
    const setKpi = (id, val) => { const n = document.querySelector('#' + id); if (n) n.textContent = val; };
    setKpi('kpi-sent', String(K.sent !== undefined && K.sent !== null ? K.sent : '—'));
    setKpi('kpi-draft-ready', String(K.draftReady !== undefined && K.draftReady !== null ? K.draftReady : '—'));
    setKpi('kpi-first-response', K.avgFirstResponse || '—');
    const unitCost = (K.costThreadCount > 0) ? (K.costToday / K.costThreadCount) : null;
    setKpi('kpi-unit-cost', unitCost === null ? '—'
      : '¥' + unitCost.toFixed(3) + ' / 条（硬指标 ≤ ¥0.20）');
    // 上线门槛卡片：后端没给的指标一律显示"待评测"，不写死"已达标"的结论（B15）。
    const gateSet = (id, val) => { const n = document.querySelector('#' + id); if (n) n.textContent = val; };
    gateSet('gate-cite', '待评测');
    gateSet('gate-recall', '待评测');
    gateSet('gate-cost', unitCost === null ? '待评测' : ('¥' + unitCost.toFixed(3) + ' / 条'));
    const maxStage = Math.max.apply(null, D.stages.map(x => x.n));
    s.innerHTML = D.stages.map(x =>
      '<tr><td>' + x.name + '</td>' +
      '<td style="width:45%"><div class="bar-track"><div class="bar-fill" style="width:' + (x.n / maxStage * 100) + '%;background:' + x.color + '"></div></div></td>' +
      '<td class="num">' + x.n + '</td><td class="num">' + (x.n / D.stages.reduce((a, b) => a + b.n, 0) * 100).toFixed(0) + '%</td></tr>'
    ).join('');

    $('#cost-lang').innerHTML = '<tr><th>语种</th><th class="num">询盘条数</th><th class="num">单位成本（元/条询盘）</th>' +
      '<th class="num">调用次数</th><th class="num">小计</th></tr>' +
      (D.costByLang || []).map(c =>
      '<tr><td>' + esc(c.lang.toUpperCase()) + '</td><td class="num">' + c.n + '</td><td class="num">¥' + c.y.toFixed(3) + '</td>' +
      '<td class="num">' + (c.calls || 0) + '</td><td class="num">¥' + (c.n * c.y).toFixed(2) + '</td></tr>').join('') +
      '<tr><td><b>合计</b></td><td class="num"><b>' + (D.costByLang || []).reduce((a, b) => a + b.n, 0) + '</b></td>' +
      '<td class="num"><b>—</b></td><td class="num"><b>' + (D.costByLang || []).reduce((a, b) => a + (b.calls || 0), 0) + '</b></td>' +
      '<td class="num"><b>¥' + (D.costByLang || []).reduce((a, b) => a + b.n * b.y, 0).toFixed(2) + '</b></td></tr>';

    // 成本趋势折线（纯 SVG，无第三方库）
    const w = 560, h = 150, pad = 26;
    const vals = D.costTrend.map(d => d.v);
    const min = Math.min.apply(null, vals) * 0.8, max = Math.max.apply(null, vals) * 1.15;
    const x = i => pad + i * (w - pad * 2) / (D.costTrend.length - 1);
    const y = v => h - pad - (v - min) / (max - min) * (h - pad * 2);
    const pts = D.costTrend.map((d, i) => x(i) + ',' + y(d.v)).join(' ');
    $('#cost-trend').innerHTML =
      '<svg viewBox="0 0 ' + w + ' ' + h + '" width="100%" role="img" aria-label="近 7 日单位询盘成本趋势">' +
      '<polyline points="' + pts + '" fill="none" stroke="#2563EB" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>' +
      D.costTrend.map((d, i) => '<circle cx="' + x(i) + '" cy="' + y(d.v) + '" r="3" fill="#2563EB"/>' +
        '<text x="' + x(i) + '" y="' + (h - 6) + '" font-size="10" fill="#64748B" text-anchor="middle">' + d.d + '</text>' +
        '<text x="' + x(i) + '" y="' + (y(d.v) - 8) + '" font-size="10" fill="#334155" text-anchor="middle">¥' + d.v.toFixed(3) + '</text>').join('') +
      '<text x="4" y="' + (h - pad + 4) + '" font-size="10" fill="#64748B">单位：元 / 条询盘</text></svg>';

    const qualityRows = (D.quality && D.quality.length)
      ? D.quality.map(q => '<tr><td>' + esc(q.label) + (q.lang === 'ar' ? ' <span class="badge badge-warning">缩减版</span>' : '') + '</td>' +
          '<td class="num">' + q.top5.toFixed(2) + '</td><td class="num">' + q.cross.toFixed(2) + '</td><td class="num">' + q.f1.toFixed(2) + '</td>' +
          '<td class="num">' + q.cite + '</td><td class="num">' + q.spec + '</td><td class="num">' + q.inject + '</td><td class="num">' + q.recall + '</td></tr>').join('')
      // 空表要说明原因（B7）：后端不伪造是对的，但"空着"必须解释，
      // 否则读者以为功能坏了。
      : '<tr><td colspan="8" style="color:var(--color-muted-foreground);font-size:12px">' +
        '暂无分语种评测数据。分语种质量来自 <code>dev.ps1 eval</code> 的评测产物，**不是实时数据**；' +
        '运行评测脚本后回填。公开检索基准（MIRACL / MLDR / Mr. TyDi / mMARCO / MKQA）只作检索管线体检，不并入本表。</td></tr>';
    const qt = $('#quality-table');
    if (qt) {
      qt.innerHTML = '<tr><th>语种</th><th class="num">检索 Top-5</th><th class="num">跨语 Top-1</th><th class="num">意图 F1</th>' +
        '<th class="num">引用正确率</th><th class="num">规格准确率</th><th class="num">注入拦截</th><th class="num">名单召回</th></tr>' +
        qualityRows;
    }
    // 读模型的口径说明必须落到界面上（B6）：否则"诚实披露"只停在 JSON 里。
    const notesHost = $('#wb-notes');
    if (notesHost) {
      notesHost.innerHTML = (D.notes && D.notes.length)
        ? '<div class="note-block"><b>口径说明</b><ul>' +
          D.notes.map(n => '<li>' + esc(n) + '</li>').join('') + '</ul></div>'
        : '';
    }

    $('#risk-events').innerHTML = D.riskEvents.map(e =>
      '<tr><td style="font-family:var(--font-mono)">' + e.time + '</td>' +
      '<td><span class="badge ' + (e.level === 'high' ? 'badge-danger' : e.level === 'mid' ? 'badge-warning' : 'badge-neutral') + '">' + e.type + '</span></td>' +
      '<td style="white-space:normal">' + e.text + '</td></tr>').join('');

    $('#gate-table').innerHTML = '<tr><th>闸门</th><th>说明</th><th>强制</th><th>今日</th></tr>' +
      D.gates.map(g => '<tr><td>' + g.name + '</td><td style="white-space:normal;color:var(--color-muted-foreground)">' + g.desc + '</td>' +
        '<td>' + (g.must ? '<span class="badge badge-danger">强制</span>' : '<span class="badge badge-neutral">非强制</span>') + '</td>' +
        '<td class="num">' + g.state + '</td></tr>').join('');
  }

  /* ================= 知识库页 ================= */
  function initKnowledge() {
    const host = $('#kb-results');
    if (!host) return;

    // 知识库版本号一律来自读模型（B10 / B12）：
    // 原来在代码里写死了一个版本号，而实际在用的是另一个 ——
    // 界面挂着一个已经不存在的库版本，业务员会据此判断引用来自哪个版本。
    // （注释里也不写具体版本号：写死的版本号会被静态检查当成"还在用"。）
    const kbVer = document.querySelector('#kb-version');
    if (kbVer) kbVer.textContent = D.kb_version || '未提供';

    // 管线步骤只在后端给了 pipeline 时才渲染（R21-03 / B5）：
    // 后端没提供的字段，界面不许拿 mock 顶上 —— 那是"界面说谎"。
    const pipeHost = $('#pipeline-steps');
    if (pipeHost) {
      pipeHost.innerHTML = (D.pipeline && D.pipeline.length)
        ? D.pipeline.map((s, i) =>
            '<div class="step"><div class="n">' + (i + 1) + '</div><div class="t">' + esc(s.t) + '</div><div class="d">' + esc(s.d) + '</div></div>').join('')
        : '<div class="empty-note"><div class="t">后端未提供管线定义</div>' +
          '<div class="d">本页不展示演示态的固定管线；真实编排轨迹请看线程详情的事件流与审计时间线。</div></div>';
    }

    $('#term-table').innerHTML = '<div class="term-row head"><div>中文</div><div>English</div><div>Español</div><div>العربية / 禁用译法</div></div>' +
      D.terms.map(t => '<div class="term-row"><div>' + t.zh + '</div><div>' + t.en + '</div><div>' + t.es + '</div>' +
        '<div><span style="direction:rtl;display:inline-block">' + t.ar + '</span><br><span class="forbid">' + t.forbid + '</span></div></div>').join('');

    function render(dateStr) {
      const d = new Date(dateStr + 'T00:00:00');
      const rows = D.kbAssets.map(a => {
        let state = '有效', stateCls = 'badge-success';
        if (a.expired) { state = '已废止 · 禁止引用'; stateCls = 'badge-danger'; }
        else if (a.valid && a.valid.indexOf('未生效') >= 0) { state = '未生效 · 过滤'; stateCls = 'badge-neutral'; }
        else if (a.valid && a.valid.indexOf('临近') >= 0) { state = '临近到期'; stateCls = 'badge-warning'; }
        return '<div class="evidence-card">' +
          '<div class="ev-top"><span class="badge badge-neutral">' + a.type + '</span>' +
          '<b style="font-family:var(--font-mono);font-size:12px">' + a.id + '</b>' +
          '<span class="badge ' + stateCls + '">' + state + '</span>' +
          '<span class="ev-score">score ' + a.score.toFixed(3) + '</span></div>' +
          '<div class="ev-body">' + a.body + '</div>' +
          '<div class="ev-foot">' + a.tags.map(t => '<code>' + t + '</code>').join('') +
          '<span>有效期：' + esc(a.valid) + '</span><span>库版本：' + esc(D.kb_version || '未提供') + '</span></div></div>';
      }).join('');
      host.innerHTML = rows;
      $('#kb-date-label').textContent = '检索基准日：' + dateStr + '（法规与证书按「买家所在国 + 当前日期」过滤）';
    }
    const di = $('#kb-date');
    di.value = '2026-11-17';
    di.addEventListener('change', e => render(e.target.value));
    render(di.value);

    const q = $('#kb-query');
    q.addEventListener('input', e => {
      const v = e.target.value.trim().toLowerCase();
      $$('.evidence-card', host).forEach(card => {
        card.style.display = !v || card.textContent.toLowerCase().indexOf(v) >= 0 ? '' : 'none';
      });
    });
  }

  document.addEventListener('DOMContentLoaded', async () => {
    // 先确认身份：未登录就去登录页（登录页内已实现跳转）
    if (window.TDAuth) {
      const u = await window.TDAuth.require();
      if (!u) return;
      applyUser(u);
    }
    await loadSource();
    initWorkbench(); initDashboard(); initKnowledge();
  });
})();