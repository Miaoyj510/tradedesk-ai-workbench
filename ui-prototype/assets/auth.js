/* TradeDesk AI Workbench —— 会话管理（S6-4）
 *
 * 本地演示形态：账号密码登录 → 会话令牌存 localStorage。
 * 真实鉴权在生产环境用 OIDC（PRD 13.4.11 / D-11），本期不实现。
 */
window.TDAuth = (function () {
  const KEY = 'td_session';

  function get() {
    try { return JSON.parse(localStorage.getItem(KEY) || 'null'); } catch (e) { return null; }
  }
  function set(session) { localStorage.setItem(KEY, JSON.stringify(session)); }
  function clear() { localStorage.removeItem(KEY); }

  function headers() {
    const s = get();
    return (s && s.token) ? { 'X-Session-Token': s.token } : {};
  }

  /* 未登录就去登录页；登录了就返回用户信息。

   * 后端**不可达**（如双击本地 HTML 打开）→ 放行到 mock 演示，不阻断。
     分开“未登录”与“后端不在”是关键：前者跳登录页，后者降级演示。
   */
  async function require() {
    const s = get();
    if (s && s.token) return s.user || null;
    try {
      const res = await fetch('/auth/me', { headers: headers() });
      if (res.status === 401) {
        location.replace('login.html?next=' + encodeURIComponent(location.pathname + location.search));
        return null;
      }
      if (res.ok) {
        const d = await res.json();
        if (d && d.user) { set(d); return d.user; }
      }
    } catch (e) {
      // 后端不可达 → 离线 mock 演示（无需登录）
      document.body.classList.add('offline-demo');
      return null;
    }
    location.replace('login.html?next=' + encodeURIComponent(location.pathname + location.search));
    return null;
  }

  /* 会话失效（后端重启 / 会话过期）就清掉并回登录页。 */
  function expired() {
    clear();
    location.replace('login.html?expired=1');
  }

  async function login(username, password) {
    const res = await fetch('/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: username, password: password })
    });
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (!res.ok) {
      const d = data && data.detail;
      throw new Error((d && d.message) || ('HTTP ' + res.status));
    }
    set(data);
    return data.user;
  }

  function logout() {
    const s = get();
    const done = () => { clear(); location.replace('login.html'); };
    if (!s || !s.token) { done(); return; }
    fetch('/auth/logout', { method: 'POST', headers: headers() }).then(done, done);
  }

  return { get: get, set: set, clear: clear, headers: headers,
           require: require, expired: expired, login: login, logout: logout };
})();
