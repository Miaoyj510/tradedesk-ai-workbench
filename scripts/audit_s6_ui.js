/* S6 独立审核 —— 前端**渲染层**（审核 agent 用）
 *
 * 为什么要有这个文件
 * ------------------
 * 实现方的 `scripts/verify_s6_ui.js` 只验了**数据层**（api.js 能不能拿到数据、
 * 令牌一次性能不能拒）。**渲染层**（app.js 把数据画成界面）一次都没被跑过 ——
 * 而本项目栽过的跟头里，有一半是"数据对了、界面说错了"：
 *   · mock 时代的固定译文，在真数据模式下挂在**别的**买家消息下面（R20-03）；
 *   · 名单命中面板把条目 ID **写死**成演示用的 CN-2211（R20-02）；
 *   · 时间线文本**不转义**（R20-01 存储型 XSS）。
 * 所以这里用**极简 DOM 桩**把 app.js 真跑一遍，拿它生成的内联 HTML 做断言。
 *
 * 怎么跑的
 * --------
 *   1. 装进 Node 的 vm 沙箱：config.js + api.js + data.js + app.js（都是真文件）；
 *   2. DOM 桩：够 app.js 跑完 initWorkbench / initDashboard / initKnowledge，
 *      并把每个容器的 innerHTML 留下来；
 *   3. 真后端：真发询盘、真签核、真转交（带 XSS 载荷）、真读模型；
 *   4. 断言：渲染出来的 HTML 里该有的有、不该有的没有。
 *
 * 用法
 *   node scripts/audit_s6_ui.js <apiBase>
 *   例如：node scripts/audit_s6_ui.js http://127.0.0.1:8099
 *
 * 退出码：0 = 无 FAIL；1 = 有 FAIL。
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.join(__dirname, '..', 'ui-prototype', 'assets');
const apiBase = process.argv[2] || 'http://127.0.0.1:8099';

let passed = 0;
let failed = 0;
const details = [];

function check(clause, name, ok, detail) {
  if (ok) { passed++; console.log('  [PASS] ' + name + (detail ? ' — ' + detail : '')); }
  else {
    failed++;
    details.push({ clause: clause, name: name, detail: detail || '' });
    console.log('  [FAIL] ' + name + (detail ? ' — ' + detail : ''));
  }
}

/* ------------------------------------------------------------------ DOM 桩 */
function makeEl(tag) {
  const el = {
    tagName: tag || 'div',
    innerHTML: '', textContent: '', value: '', className: '', disabled: false,
    style: {}, dataset: {}, children: [], attrs: {}, handlers: {},
    classList: { add() {}, remove() {}, contains() { return false; }, toggle() {} },
    setAttribute(k, v) { this.attrs[k] = v; this[k] = v; },
    getAttribute(k) { return this.attrs[k]; },
    addEventListener(type, fn) { (this.handlers[type] = this.handlers[type] || []).push(fn); },
    removeEventListener() {},
    appendChild(c) { this.children.push(c); return c; },
    remove() {}, focus() {}, click() {},
    closest() { return null; },
    parentElement: null,
    querySelector(sel) { return el(sel); },
    querySelectorAll(sel) { return sel === '.thread' ? threadButtons : []; },
  };
  return el;
}

/* 线程按钮：app.js 渲染完列表后会给每个 .thread 绑 click；
 * 桩里我们按读模型里的线程顺序造按钮，好让审核方能"点开"任意一条线程。 */
let threadButtons = [];
function makeThreadButtons(ids) {
  return ids.map(id => {
    const b = makeEl('button');
    b.dataset.id = id;
    b.click = function () { (this.handlers['click'] || []).forEach(fn => fn()); };
    return b;
  });
}

const registry = {};
function el(sel) {
  if (!registry[sel]) registry[sel] = makeEl('div');
  return registry[sel];
}

const document = {
  querySelector(sel) { return el(sel); },
  querySelectorAll(sel) { return sel === '.thread' ? threadButtons : []; },
  createElement(tag) { return makeEl(tag); },
  addEventListener(type, fn) { if (type === 'DOMContentLoaded') domReady = fn; },
  body: makeEl('body'),
};
let domReady = null;

/* ------------------------------------------------------------------ 主流程 */
async function main() {
  console.log('='.repeat(70));
  console.log('S6 独立审核 —— 前端渲染层（API：' + apiBase + '）');
  console.log('='.repeat(70));

  const sandbox = {
    window: { location: { search: '' }, prompt: () => null },
    document: document,
    console: console,
    fetch: fetch,
    URLSearchParams: URLSearchParams,
    setTimeout: () => 0,
    clearTimeout: () => 0,
  };
  vm.createContext(sandbox);

  const run = (file) => vm.runInContext(fs.readFileSync(path.join(ROOT, file), 'utf8'), sandbox, { filename: file });
  run('config.js');
  // apiBase 必须在 api.js 加载**之前**注入（api.js 在 IIFE 里就持有 cfg 了）。
  sandbox.window.TD_CONFIG = Object.assign({}, sandbox.window.TD_CONFIG, { apiBase: apiBase, source: 'api' });
  run('api.js');
  run('data.js');

  const api = sandbox.window.TDApi;
  if (!api) { console.log('  [FAIL] api.js 导出 TDApi'); process.exit(1); }

  /* ---- 1. 造数据：一条普通询盘、一条名单命中、一条用来转交（带 XSS 载荷） ---- */
  const XSS = '<img src=x onerror="window.__xss=1">';

  const normal = await api.inbound({
    buyer_id: 'BY-AUDIT-1', language: 'en', company: 'Northwind Instruments Inc.', country: 'US',
    message: 'Please quote 12000 pcs of M-77 CIF Vancouver and confirm the ISO 9001 certificate.'
  });

  // 名单命中：用演示名单里的**第二条**（CN-2307），不是第一条（CN-2211）。
  const blocked = await api.inbound({
    buyer_id: 'BY-AUDIT-2', language: 'ar', company: 'Demo Metal Trading SARL', country: 'SA',
    message: 'مرحبا، نطلب التعامل عن الموديل M-77'
  });

  // 转交 + 注入载荷（转交原因会进事件流 → 时间线 → 界面）
  let transferred = false;
  try {
    const tok = await api.gateToken(normal.thread_id, normal.draft_id, 'sign_off');
    await api.req('/threads/' + encodeURIComponent(normal.thread_id) + '/signoff', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Gate-Token': tok },
      body: { draft_id: normal.draft_id, action: 'transfer', reason: XSS }
    });
    transferred = true;
  } catch (e) {
    console.log('    转交失败：' + e.message);
  }
  check('S6-B1', '造数据：普通询盘 / 名单命中 / 带载荷的转交',
    !!(normal && normal.thread_id && blocked && blocked.thread_id && transferred),
    'normal=' + (normal && normal.thread_id) + ' blocked=' + (blocked && blocked.thread_id));

  /* ---- 2. 读模型 ---- */
  const live = await api.loadWorkbench();
  const liveThreadIds = (live.threads || []).map(t => t.id);
  threadButtons = makeThreadButtons(liveThreadIds);

  /* ---- 3. 真跑 app.js ---- */
  run('app.js');
  if (!domReady) { console.log('  [FAIL] app.js 没注册 DOMContentLoaded'); process.exit(1); }
  await domReady();

  const conv = el('#conversation').innerHTML;
  const review = el('#review').innerHTML;
  const list = el('#thread-list').innerHTML;
  const kb = el('#kb-results').innerHTML;
  const pipeline = el('#pipeline-steps').innerHTML;
  const quality = el('#quality-table').innerHTML;
  const termTable = el('#term-table').innerHTML;

  /* ---- 4. 断言 ---- */

  // S6-B2 · 名单命中面板的条目 ID 必须来自后端，不能写死
  const blockedThread = (live.threads || []).find(t => t.id === blocked.thread_id);
  const evidenceIds = ((blockedThread && blockedThread.evidence) || [])
    .map(e => e.v).filter(v => /^CN-\d+$/.test(v || ''));
  const btn = threadButtons.find(b => b.dataset.id === blocked.thread_id);
  if (btn) btn.click();
  const blockedConv = el('#conversation').innerHTML;
  const realId = evidenceIds[0] || '';
  check('S6-B2', '名单命中面板显示的条目 ID 与证据链一致（不得写死演示条目）',
    !!(realId && blockedConv.indexOf(realId) >= 0 && blockedConv.indexOf('CN-2211') < 0),
    '证据链条目=' + (realId || '（未找到）') + '；面板含 CN-2211=' + (blockedConv.indexOf('CN-2211') >= 0));

  // S6-B3 · 时间线文本必须转义（存储型 XSS）
  const convBtn = threadButtons.find(b => b.dataset.id === normal.thread_id);
  if (convBtn) convBtn.click();
  const reviewX = el('#review').innerHTML;
  check('S6-B3', '时间线文本必须转义（注入载荷不得原样进 innerHTML）',
    reviewX.indexOf('<img src=x onerror=') < 0,
    reviewX.indexOf('<img src=x onerror=') >= 0 ? '原样出现在审计时间线里' : '已转义');

  // S6-B4 · 真数据模式不得显示与本次消息无关的固定译文
  const FIXED_ES = '我们是一家巴西经销商';
  const FIXED_EN = '你好，能告诉我 M-77 夹具的材质和安全工作载荷吗';
  const FIXED_AR = '你好，我们是从阿联酋进口的';
  const convNormal = el('#conversation').innerHTML;
  const fixedLeak = [FIXED_ES, FIXED_EN, FIXED_AR].filter(x => convNormal.indexOf(x) >= 0);
  check('S6-B4', '真数据模式不得显示与本次买家消息无关的固定译文',
    fixedLeak.length === 0,
    fixedLeak.length ? '命中固定译文：' + fixedLeak.join(' / ') : '');

  // S6-B5 · api 模式下不得渲染 mock 静态内容（后端没给的字段不许用 mock 顶上）
  check('S6-B5', 'api 模式不得残留 mock 静态内容（后端未提供的字段不应被渲染）',
    !(live.pipeline === undefined && pipeline && pipeline.indexOf('step') >= 0),
    live.pipeline === undefined ? '读模型没有 pipeline，但界面渲染了 7 步静态管线' : '');

  // S6-B6 · 口径说明要落到界面上（否则"诚实披露"只在 JSON 里）
  const notes = live.notes || [];
  const panels = conv + review + kb + quality + el('#data-source').textContent;
  const shownAnywhere = notes.length > 0 && notes.some(n => panels.indexOf(n.slice(0, 12)) >= 0);
  check('S6-B6', '读模型的口径说明（notes）必须显示在界面上',
    shownAnywhere,
    shownAnywhere ? '' : (notes.length ? 'notes 一条都没出现在任何面板里（诚实披露只停在 JSON）' : '读模型没有 notes'));

  // S6-B7 · 评测类指标为空时界面必须给说明（不能是一张空表）
  const qualityEmpty = Array.isArray(live.quality) && live.quality.length === 0;
  const qualityExplained = quality.indexOf('评测') >= 0 || quality.indexOf('暂无') >= 0;
  check('S6-B7', '分语种质量为空时界面要说明原因（不能是一张空表）',
    !(qualityEmpty && !qualityExplained),
    qualityEmpty && !qualityExplained ? 'quality 为空数组，界面只有表头' : '');

  // S6-B8 · 库版本号必须来自读模型
  const appSrc = fs.readFileSync(path.join(ROOT, 'app.js'), 'utf8');
  const hardVer = (appSrc.match(/kb-\d{4}\.\d{2}-[a-z]/) || [])[0];
  check('S6-B8', '知识库页的库版本号必须来自读模型（不得写死）',
    !hardVer,
    hardVer ? 'app.js 里写死了 ' + hardVer + '（读模型实际是 ' + live.kb_version + '）' : '');

  // S6-B9 · 术语表必须是真数据（后端术语块）
  check('S6-B9', '术语表来自后端（真数据模式下不是 mock 的 5 条）',
    termTable.indexOf('最小起订量') >= 0 || termTable.indexOf('MOQ') >= 0,
    '术语表长度=' + termTable.length);

  // S6-B10 · 线程列表必须渲染
  check('S6-B10', '线程列表渲染出真实线程', list.indexOf('thread-title') >= 0 || list.length > 0,
    '列表长度=' + list.length);

  // S6-B11 · 看板顶栏的 KPI 不得是写死的 mock 数字
  // （dashboard.html 的四个 KPI 没有 id，app.js 也从不写它们 ——
  //   真数据模式下"今日已发送 41 / 草稿可用率 78% / 首响中位数 / ¥0.046"全是 mock）
  const dashHtml = fs.readFileSync(path.join(ROOT, '..', 'dashboard.html'), 'utf8');
  const dashKpis = (dashHtml.match(/<div class="kpi">[\s\S]*?<\/div>\s*<\/div>/g) || []);
  const dashStatic = dashKpis.filter(k => !/id="/.test(k));
  check('S6-B11', '看板顶栏 KPI 必须是真数据（不得是写死且无人更新的 mock 数字）',
    dashStatic.length === 0,
    'dashboard.html 顶栏 ' + dashStatic.length + ' 个 KPI 没有 id，app.js 也从不写它们 → '
    + '真数据模式下仍显示 mock 的 41 / 78% / 3 分 12 秒 / ¥0.046');

  // S6-B12 · 知识库页顶栏的库版本号
  const kbHtml = fs.readFileSync(path.join(ROOT, '..', 'knowledge.html'), 'utf8');
  const kbVerStatic = (kbHtml.match(/kb-\d{4}\.\d{2}-[a-z]/g) || []);
  check('S6-B12', '知识库页顶栏的库版本号来自读模型（不得写死）',
    kbVerStatic.length === 0,
    'knowledge.html 写死 ' + kbVerStatic.join(',') + '（读模型实际是 ' + live.kb_version + '）');

  // S6-B13 · PRD FR-HUM-06：四种签核动作在真数据模式下都要能执行
  // （前端 onActionApi 只实现了 approve / modify / reject，
  //   transfer / block / escalate 落到"暂未开放"分支）
  const appSrc2 = fs.readFileSync(path.join(ROOT, 'app.js'), 'utf8');
  const apiBranch = appSrc2.slice(appSrc2.indexOf('async function onActionApi'));
  const hasTransfer = /'transfer'/.test(apiBranch);
  check('S6-B13', 'FR-HUM-06 四种签核动作在真数据模式下都可执行（含转交他人）',
    hasTransfer,
    hasTransfer ? '' : 'onActionApi 里没有 transfer 分支 → 转交在真数据模式下走"暂未开放"');

  // S6-B14 · 业务员在界面上改了草稿，通过时必须把改后的正文发出去
  // （追问 chip 会改 DOM 里的 .draft-body，但 approve 分支不带 body
  //   → 改的内容被静默丢弃，用户以为发的是改过的）
  const approveBranch = apiBranch.slice(0, apiBranch.indexOf("act === 'modify'"));
  const approveSendsBody = /signoff\([^)]*body/.test(approveBranch.replace(/\s+/g, ' '));
  check('S6-B14', '真数据模式下业务员改过的草稿必须随签核发出（不得静默丢弃修改）',
    approveSendsBody,
    approveSendsBody ? '' : 'approve 分支调 signoff 时不带 body → 界面上的编辑被丢掉，但 toast 说"已签核并发送"');

  // S6-B15 · "上线门槛（Go / No-Go）"卡片不得是写死的达标结论
  const gateCard = (dashHtml.match(/上线门槛[\s\S]*?<\/section>/) || [''])[0];
  const hardcodedPass = (gateCard.match(/\d+(?:\.\d+)?%|¥\d+(?:\.\d+)?/g) || []);
  check('S6-B15', '上线门槛卡片必须是真数据（不得写死“已达标”的结论）',
    hardcodedPass.length === 0,
    '门槛卡片里写死了 ' + hardcodedPass.join(' / ') + ' —— 真数据模式下仍宣称引用正确率/名单召回/单位成本已达标');

  console.log('='.repeat(70));
  console.log('渲染层审核结果：通过 ' + passed + '，FAIL ' + failed + '，共 ' + (passed + failed) + ' 项');
  console.log('='.repeat(70));
  // 不要 process.exit()：Windows 上会在 fetch 的连接还在收尾时崩（uv 断言），
  // 崩出来的退出码会把"断言结果"盖掉。用 exitCode 让进程自然退出。
  process.exitCode = failed ? 1 : 0;
}

main().catch(e => { console.error(e); process.exit(1); });
