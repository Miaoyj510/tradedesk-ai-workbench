/* TradeDesk AI Workbench —— 真数据 API 客户端（S6-1b）
 *
 * 谁谓这里只负责两件事：发请求、把后端的读模型装成前端要的形状。
 * 业务规则（闸门、引用、队列）全部在后端 —— 前端不重算、不自定。
 */
window.TDApi = (function () {
  const cfg = window.TD_CONFIG || {};

  function base() {
    return (cfg.apiBase || '').replace(/\/$/, '');
  }

  async function req(path, opts) {
    const auth = (window.TDAuth && window.TDAuth.headers) ? window.TDAuth.headers() : {};
    const init = Object.assign({
      headers: Object.assign({ 'Content-Type': 'application/json' }, auth)
    }, opts || {});
    if (init.body && typeof init.body !== 'string') {
      init.body = JSON.stringify(init.body);
    }
    const res = await fetch(base() + path, init);
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
    if (!res.ok) {
      const d = data && data.detail;
      const msg = (d && (d.message || d.code)) || ('HTTP ' + res.status);
      // 会话失效（后端重启 / 过期）→ 清掉本地会话并回登录页
      if (res.status === 401 && window.TDAuth && window.TDAuth.expired) {
        window.TDAuth.expired();
        return null;
      }
      const err = new Error(msg);
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  /* 真实演示场景：每个场景对应一条真实询盘，走后端全链路。 */
  const SCENARIOS = [
    {
      id: 'A', label: '脚本 A · 英语询价（引用与闸门）',
      payload: {
        buyer_id: 'BY-DEMO-A', language: 'en', company: 'Brasil Distribuidora Ltda', country: 'BR',
        message: 'Quote 5000 pcs of X-200 FOB Shenzhen, do you have CE certification?'
      }
    },
    {
      id: 'B', label: '脚本 B · 西语跨语种召回',
      payload: {
        buyer_id: 'BY-DEMO-B', language: 'es', company: 'Distribuidora Sur', country: 'BR',
        message: 'Solicito precio de 3000 piezas M-77 FOB Santos y necesito el certificado CE.'
      }
    },
    {
      id: 'C', label: '脚本 C · 阿语 + 受控名单（命中即不回复）',
      payload: {
        buyer_id: 'BY-DEMO-C', language: 'ar', company: 'Gulf Trading Partners LLC', country: 'AE',
        message: 'مرحبا، نطلب التعامل عن الموديل M-77'
      }
    },
    {
      id: 'D', label: '脚本 D · 歧义追问（不猜价格）',
      payload: {
        buyer_id: 'BY-DEMO-D', language: 'en', company: 'Unknown Buyer', country: '',
        message: 'Send me your best price for the manual hydraulic hand truck.'
      }
    }
  ];

  /* ---------- 读模型 ---------- */
  async function loadWorkbench() {
    return await req('/workbench');
  }

  async function kbSearch(q, opts) {
    const o = opts || {};
    const params = new URLSearchParams();
    params.set('q', q || '');
    params.set('lang', o.lang || 'zh');
    if (o.country) params.set('country', o.country);
    if (o.sku) params.set('sku', o.sku);
    params.set('top_k', String(o.topK || 8));
    return await req('/kb/search?' + params.toString());
  }

  /* ---------- 写操作（写操作与生成分离：每次都要闸门令牌） ---------- */
  async function gateToken(threadId, draftId, action) {
    const d = await req('/threads/' + encodeURIComponent(threadId) + '/gate-token', {
      method: 'POST', body: { draft_id: draftId, action: action || 'sign_off' }
    });
    return d.token;
  }

  async function signoff(threadId, draftId, action, extra) {
    const token = await gateToken(threadId, draftId, 'sign_off');
    return await req('/threads/' + encodeURIComponent(threadId) + '/signoff', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Gate-Token': token },
      body: Object.assign({ draft_id: draftId, action: action }, extra || {})
    });
  }

  async function send(threadId, draftId) {
    // 发送用**新的**闸门令牌：令牌是一次性的（签核用过就不能再用）。
    const token = await gateToken(threadId, draftId, 'send_draft');
    return await req('/threads/' + encodeURIComponent(threadId) + '/send', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Gate-Token': token },
      body: { draft_id: draftId }
    });
  }

  async function inbound(payload) {
    return await req('/inbound', { method: 'POST', body: payload });
  }

  return { loadWorkbench, kbSearch, gateToken, signoff, send, inbound, scenarios: SCENARIOS, req: req };
})();
