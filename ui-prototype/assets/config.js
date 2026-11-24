/* TradeDesk AI Workbench —— 数据源开关（S6-1b）
 *
 * 默认 mock：双击 index.html 就能看完整演示，不需要后端、不会报错。
 * 切到真数据：把 source 改成 'api'，或在 URL 上加 ?source=api。
 *
 * apiBase 留空 = 同源（后端与静态页同源时用）。
 *
 * ⚠️ **本项目不做跨地址调试**：S6-2 起前端由后端同源托管（/ui），
 * 前后端同源就不存在跨域问题。真的把前端拆到另一个地址部署时，
 * 需要在后端配置允许的来源白名单（生产环境是运维配置项，见 PRD 13.4.6）；
 * **本地 Demo 没有实现该白名单**，所以 apiBase 填别的地址会被浏览器拦下。
 */
window.TD_CONFIG = {
  source: 'mock',      // 'mock' | 'api'
  apiBase: '',
  // 真数据模式下「演示场景”按钮改为**真发一条询盘**。
  // mock 模式下它列出已预置的线程（不发请求）。
  liveScenarios: true
};
