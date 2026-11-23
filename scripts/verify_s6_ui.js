/* TradeDesk AI Workbench —— 前端数据源联调验证（S6-1b）
 *
 * 用法：node scripts/verify_s6_ui.js <apiBase>
 *   例如：node scripts/verify_s6_ui.js http://127.0.0.1:8099
 *
 * 验证的是**前端硬计口**：把 ui-prototype 的 config.js / api.js 真的装进 Node，
 * 对着**真实运行的后端**跑一遍——包括真发询盘、真签核、真发送。
 * 为什么不直接用浏览器：浏览器需要下载 Chromium（本机未预装，且镜像源不可达）；
 * 而前端的数据源层是**纯 JS**，用 Node 跑它能把真实的网络交互部分全部覆盖。
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.join(__dirname, '..', 'ui-prototype', 'assets');
const apiBase = process.argv[2] || 'http://127.0.0.1:8099';
let failed = 0;
let passed = 0;

function check(name, ok, detail) {
  if (ok) { passed++; console.log('  [PASS] ' + name + (detail ? ' — ' + detail : '')); }
  else { failed++; console.log('  [FAIL] ' + name + (detail ? ' — ' + detail : '')); }
}

async function main() {
  console.log('='.repeat(66));
  console.log('S6-1b 前端数据源联调验证（API：' + apiBase + '）');
  console.log('='.repeat(66));

  // ---- 1. 静态检查：文件存在 + 语法正确 ----
  for (const f of ['config.js', 'api.js', 'app.js', 'data.js']) {
    const p = path.join(ROOT, f);
    let ok = fs.existsSync(p);
    if (ok) {
      try { new (require('vm').Script)(fs.readFileSync(p, 'utf8'), { filename: f }); }
      catch (e) { ok = false; console.log('    语法错误：' + e.message); }
    }
    check('前端资源 ' + f + ' 存在且语法正确', ok);
  }
  const cfgSrc = fs.readFileSync(path.join(ROOT, 'config.js'), 'utf8');
  check('默认数据源是 mock（双击打开仍可演示）', /source:\s*'mock'/.test(cfgSrc));

  // ---- 2. 把 config.js / api.js 真的装进 Node ----
  const sandbox = { window: {}, console: console, fetch: fetch, URLSearchParams: URLSearchParams };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(ROOT, 'config.js'), 'utf8'), sandbox, { filename: 'config.js' });
  // apiBase 必须在 api.js 加载**之前**注入：api.js 在 IIFE 里就把 cfg 对象持有了。
  // 否则 Node 里拿到的 apiBase 是空字符串 → fetch('/workbench') 相对路径无基址地址 → 直接报错。
  sandbox.window.TD_CONFIG = Object.assign({}, sandbox.window.TD_CONFIG, { apiBase: apiBase, source: 'api' });
  vm.runInContext(fs.readFileSync(path.join(ROOT, 'api.js'), 'utf8'), sandbox, { filename: 'api.js' });
  const api = sandbox.window.TDApi;
  check('api.js 导出了 TDApi', !!api);
  if (!api) process.exit(1);

  // ---- 3. 真拿读模型 ----
  let wb = null;
  try {
    wb = await api.loadWorkbench();
    check('能拿到 /workbench', true);
  } catch (e) {
    check('能拿到 /workbench', false, e.message);
    console.log('='.repeat(66));
    console.log('验证结果：通过 ' + passed + '，FAIL ' + failed);
    console.log('='.repeat(66));
    process.exit(1);
  }
  for (const k of ['threads', 'kpis', 'stages', 'gates', 'kbAssets', 'terms']) {
    check('读模型含 ' + k, wb[k] !== undefined);
  }
  check('读模型不伪造 quality（评测指标非实时）',
        Array.isArray(wb.quality) && wb.quality.length === 0);

  // ---- 4. 真发询盘（四个演示场景）——走后端全链路 ----
  let created = [];
  for (const sc of api.scenarios) {
    try {
      const r = await api.inbound(sc.payload);
      created.push(r);
      check('场景 ' + sc.id + ' 真实发起询盘', !!r.thread_id, r.status + ' / ' + r.thread_id);
    } catch (e) {
      check('场景 ' + sc.id + ' 真实发起询盘', false, e.message);
    }
  }

  // ---- 5. 真签核（带闸门令牌）与真发送 ----
  const target = created.find(r => r.draft && r.draft.body);
  if (!target) {
    check('找到一个可签核的草稿', false, '没有产出草稿的线程');
  } else {
    check('找到一个可签核的草稿', true, target.thread_id);
    try {
      const r = await api.signoff(target.thread_id, target.draft_id, 'approve');
      check('签核通过（闸门令牌实际生效）', r.ok === true, JSON.stringify(r).slice(0, 90));
    } catch (e) {
      check('签核通过（闸门令牌实际生效）', false, e.message);
    }
    try {
      const r = await api.send(target.thread_id, target.draft_id);
      check('发送（第二张一次性闸门令牌）', r.ok === true, JSON.stringify(r).slice(0, 90));
    } catch (e) {
      check('发送（第二张一次性闸门令牌）', false, e.message);
    }
    // 一次性闸门令牌：同一张令牌不能用两次。
    // 注意：signoff() 每次都会**重新取一张令牌**，所以调两次 signoff 并不能验证一次性。
    // 要验这一点，必须自己取一张令牌连用两次。
    const oneTok = await api.gateToken(target.thread_id, target.draft_id, 'sign_off');
    const signPath = '/threads/' + encodeURIComponent(target.thread_id) + '/signoff';
    const signBody = { draft_id: target.draft_id, action: 'approve' };
    let firstOk = false;
    try {
      await api.req(signPath, { method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Gate-Token': oneTok },
        body: signBody });
      firstOk = true;
    } catch (e) {
      console.log('    第一次用令牌失败：' + e.message);
    }
    check('第一次使用闸门令牌成功', firstOk);
    let secondStatus = 0;
    try {
      await api.req(signPath, { method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Gate-Token': oneTok },
        body: signBody });
    } catch (e) {
      secondStatus = e.status || 0;
    }
    check('一次性闸门令牌（同令牌不能用二次）',
      secondStatus === 403, '第二次返回 HTTP ' + secondStatus + '（应为 403）');
  }

  // ---- 6. 静态接线检查：app.js 里真数据分支存在 ----
  const appSrc = fs.readFileSync(path.join(ROOT, 'app.js'), 'utf8');
  for (const needle of ['loadSource', 'reloadLive', 'onActionApi', 'currentSource']) {
    check('app.js 含 ' + needle + '（真数据分支）', appSrc.indexOf(needle) >= 0);
  }

  console.log('='.repeat(66));
  console.log('验证结果：通过 ' + passed + '，FAIL ' + failed + '，共 ' + (passed + failed) + ' 项');
  console.log('='.repeat(66));
  process.exit(failed ? 1 : 0);
}

main().catch(e => { console.error(e); process.exit(1); });
