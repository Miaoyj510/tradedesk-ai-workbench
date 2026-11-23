/* TradeDesk AI Workbench —— 数据源开关（S6-1b）
 *
 * 默认 mock：双击 index.html 就能看完整演示，不需要后端、不会报错。
 * 切到真数据：把 source 改成 'api'，或在 URL 上加 ?source=api。
 *
 * apiBase 留空 = 同源（后端与静态页同源时用）；
 * 跨地址调试时填完整前缀（如 http://127.0.0.1:8080），此时后端需允许 CORS。
 */
window.TD_CONFIG = {
  source: 'mock',      // 'mock' | 'api'
  apiBase: '',
  // 真数据模式下「演示场景”按钮改为**真发一条询盘**。
  // mock 模式下它列出已预置的线程（不发请求）。
  liveScenarios: true
};
