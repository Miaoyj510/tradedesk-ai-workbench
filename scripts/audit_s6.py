# -*- coding: utf-8 -*-
"""S6 独立审核脚本（审核 agent 用）。

与 `scripts/verify_s6.py` / `scripts/verify_s6_ui.js` 的区别
---------------------------------------------------------
那两个是实现 agent 交卷时的**自证**（24 + 27 项）。本脚本是**审核方自己按 PRD
重新写**的断言，重点盯六件事 —— 都是这个项目栽过的跟头的翻版：

1. **渲染层根本没被验过**：自证脚本只跑了 `api.js`（数据层），
   `app.js` 怎么把数据画成界面，一次都没跑。于是"数据对、界面说错"没人发现
   （名单条目写死、mock 译文挂在真消息下面、mock 静态内容残留）。
2. **不转义就是存储型 XSS**：本项目里"前端重算闸门"是 R17 的教训，
   "界面不转义"是它的孪生兄弟。转交原因可以经 HTTP 接口任意写进事件流。
3. **口径不许错位**：看板写"元 / 条询盘"，算的却是"元 / 次模型调用"；
   PRD 12.4 的硬指标是**每条询盘**的单位成本 —— 口径错位等于把硬指标报错了。
4. **不许有假的数字**：`quality` 空着是对的（评测产物不是实时数据），
   但空着就得在界面上说清楚；`budget: 2.4` 这种没出处的常量不该出现在真数据里。
5. **前端不许重算业务规则**：闸门、风险等级、状态一律后端说了算。
6. **文档说的用法必须能用**：`config.js` 说"跨地址调试需要后端允许 CORS"，
   而后端没有 CORS 中间件 —— 文档指的用法直接不可用。

审核轮次
--------
- **R20：S6-1a 工作台读模型（BFF）+ S6-1b 前端接真数据**
  结论见 `docs/S6-AUDIT-R20.md`。

用法
    python scripts/audit_s6.py
    python scripts/audit_s6.py --dsn "postgresql://...:5433/tradedesk_audit_b"
    python scripts/audit_s6.py --json

退出码：0 = 无 FAIL；1 = 有 FAIL。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _setup_stdout() -> None:
    """自适应输出编码（控制台是 GBK，非 tty 强制 UTF-8）。"""
    if os.environ.get("PYTHONIOENCODING"):
        return
    try:
        if sys.stdout.isatty():
            sys.stdout.reconfigure(errors="replace")
        else:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


_setup_stdout()

RESULTS = []
CURRENT_CLAUSE = "?"
_PG_DBS = []
_TMPDIRS = []


# --------------------------------------------------------------------------
# 断言工具
# --------------------------------------------------------------------------
def section(title: str) -> None:
    print("\n" + title)
    print("-" * max(len(title), 70))


def clause(cid: str) -> None:
    global CURRENT_CLAUSE
    CURRENT_CLAUSE = cid
    print("\n[%s]" % cid)


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append({"clause": CURRENT_CLAUSE, "name": name,
                    "level": "PASS" if ok else "FAIL", "detail": detail})
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (f" — {detail}" if detail else ""))
    return bool(ok)


def note(name: str, detail: str = "") -> None:
    """记账项：不违约，但要记。"""
    RESULTS.append({"clause": CURRENT_CLAUSE, "name": name, "level": "NOTE", "detail": detail})
    print("  [NOTE] " + name + (f" — {detail}" if detail else ""))


def http_json(url: str, method: str = "GET", body=None, headers=None):
    data = None
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw or "{}")
        except Exception:
            return e.code, {"raw": raw[:200]}


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_server(port: int, db_path: str):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    env["TRADEDESK_DB_PATH"] = db_path
    # 审核不调云端（不计费）：本轮验的是前后端契约与渲染，不是模型能力
    env["TRADEDESK_LLM_DRAFT_ENABLED"] = "0"
    env["TRADEDESK_EMBEDDER_ENABLED"] = "0"
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "tradedesk.api.app:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    for _ in range(60):
        if proc.poll() is not None:
            out = proc.stdout.read().decode("utf-8", "replace") if proc.stdout else ""
            raise RuntimeError("后端启动失败：\n" + out[-1500:])
        try:
            with urllib.request.urlopen(base + "/health", timeout=2):
                return proc, base
        except Exception:
            time.sleep(0.5)
    proc.terminate()
    raise RuntimeError("后端启动超时")


def pg_split(dsn: str):
    head, _, _db = dsn.rpartition("/")
    return head + "/"


def pg_exec(db: str, sql: str) -> None:
    import psycopg
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(sql)


# --------------------------------------------------------------------------
# §S6-A 读模型契约
# --------------------------------------------------------------------------
def audit_contract(base: str, wb: dict) -> None:
    clause("S6-A")
    for k in ("threads", "kpis", "stages", "gates", "kbAssets", "terms",
              "costByLang", "costTrend", "riskEvents", "quality", "notes", "kb_version"):
        check(f"读模型含 {k}", k in wb)
    check("quality 为空数组（分语种质量是评测产物，不是实时数据，不伪造）",
          wb.get("quality") == [])
    check("读模型带口径说明 notes", bool(wb.get("notes")))
    check("notes 说明了 quality 的来源",
          "评测" in " ".join(wb.get("notes") or []))
    check("读模型带 kb_version（前端不能写死版本号）", bool(wb.get("kb_version")))

    # 后端给的必须是**真条目 ID**（前端若写死 CN-2211 就是前端的锅）
    blocked = [t for t in wb.get("threads", [])
               if t.get("state") == "blocked"]
    check("名单命中的线程在读模型里 state=blocked", bool(blocked))
    if blocked:
        t = blocked[0]
        ids = [e["v"] for e in t.get("evidence", []) if str(e.get("v", "")).startswith("CN-")]
        check("名单命中线程带真实条目 ID（后端口径）", bool(ids), ",".join(ids))
        check("名单命中线程 risk=high（GATE-03）", t.get("risk") == "high", str(t.get("risk")))
        check("名单命中线程没有草稿（不生成任何回复，FR-EVA-07）",
              not t.get("draft") or not (t.get("draft") or {}).get("body"))


# --------------------------------------------------------------------------
# §S6-B 写操作与闸门（越权路径）
# --------------------------------------------------------------------------
def audit_writes(base: str) -> None:
    clause("S6-B")
    st, ing = http_json(base + "/inbound", "POST", {
        "buyer_id": "BY-AUDIT-W", "language": "en", "company": "Audit Buyer Ltd", "country": "GB",
        "message": "Quote 8000 pcs of X-200 FOB Shenzhen, do you have CE certification?"})
    check("POST /inbound 产生线程", st == 200 and bool(ing.get("thread_id")), str(st))
    tid = ing.get("thread_id")
    did = ing.get("draft_id")

    # 1) 无令牌 → 403
    st, _ = http_json(base + f"/threads/{tid}/signoff", "POST",
                       {"draft_id": did, "action": "approve"})
    check("写操作缺闸门令牌 → 403", st == 403, str(st))

    # 2) 一次性：同一张令牌不能用两次
    st, tok = http_json(base + f"/threads/{tid}/gate-token", "POST",
                        {"draft_id": did, "action": "sign_off"})
    check("闸门令牌可签发", st == 200 and bool(tok.get("token")), str(st))
    token = tok.get("token")
    st1, _ = http_json(base + f"/threads/{tid}/signoff", "POST",
                       {"draft_id": did, "action": "approve"},
                       headers={"X-Gate-Token": token})
    st2, _ = http_json(base + f"/threads/{tid}/signoff", "POST",
                       {"draft_id": did, "action": "approve"},
                       headers={"X-Gate-Token": token})
    check("闸门令牌一次性（第二次 403）", st1 == 200 and st2 == 403, f"{st1}/{st2}")

    # 3) 动作绑定：sign_off 的令牌不能拿去 send_draft
    st, tok2 = http_json(base + f"/threads/{tid}/gate-token", "POST",
                         {"draft_id": did, "action": "sign_off"})
    st3, _ = http_json(base + f"/threads/{tid}/send", "POST", {"draft_id": did},
                       headers={"X-Gate-Token": tok2.get("token")})
    check("闸门令牌绑定动作（sign_off 令牌不能发草稿）", st3 == 403, str(st3))

    # 4) 未签核不得发送（要用一条**还没签核过**的线程）
    st, ing_ns = http_json(base + "/inbound", "POST", {
        "buyer_id": "BY-AUDIT-NS", "language": "en", "company": "Audit Buyer Ltd",
        "country": "GB", "message": "Quote 300 pcs of X-200 FOB Shenzhen."})
    ns_tid, ns_did = ing_ns.get("thread_id"), ing_ns.get("draft_id")
    st, tok3 = http_json(base + f"/threads/{ns_tid}/gate-token", "POST",
                         {"draft_id": ns_did, "action": "send_draft"})
    st4, _ = http_json(base + f"/threads/{ns_tid}/send", "POST", {"draft_id": ns_did},
                       headers={"X-Gate-Token": tok3.get("token")})
    check("未签核不得外发（FR-EVA-06 / FR-HUM-01）", st4 == 403, str(st4))

    # 5) 驳回必须带原因
    st, tok4 = http_json(base + f"/threads/{tid}/gate-token", "POST",
                         {"draft_id": did, "action": "sign_off"})
    st5, _ = http_json(base + f"/threads/{tid}/signoff", "POST",
                       {"draft_id": did, "action": "reject"},
                       headers={"X-Gate-Token": tok4.get("token")})
    check("驳回不带原因 → 拒绝（FR-HUM-06）", st5 in (403, 422), str(st5))

    # 6) 签核留痕要含 5 要素（FR-HUM-01）
    st, ing2 = http_json(base + "/inbound", "POST", {
        "buyer_id": "BY-AUDIT-T", "language": "en", "company": "Audit Buyer Ltd", "country": "GB",
        "message": "Quote 8000 pcs of X-200 FOB Shenzhen, do you have CE certification?"})
    tid2, did2 = ing2.get("thread_id"), ing2.get("draft_id")
    st, tok5 = http_json(base + f"/threads/{tid2}/gate-token", "POST",
                         {"draft_id": did2, "action": "sign_off"})
    st6, _ = http_json(base + f"/threads/{tid2}/signoff", "POST",
                       {"draft_id": did2, "action": "approve"},
                       headers={"X-Gate-Token": tok5.get("token")})
    check("签核成功", st6 == 200, str(st6))
    st, evs = http_json(base + f"/threads/{tid2}/events")
    signed = [e for e in evs if e.get("event_type") == "signed_off"]
    payload = (signed[0].get("payload") if signed else {}) or {}
    need = ("unit_price", "currency", "terms", "valid_until", "tier", "clause_version")
    missing = [k for k in need if k not in payload]
    check("签核留痕含价格要素 5 件套 + 条款版本（FR-HUM-01 / GATE-01）",
          not missing, "缺：" + ",".join(missing) if missing else "")


# --------------------------------------------------------------------------
# §S6-C 时间线与 XSS
# --------------------------------------------------------------------------
XSS = '<img src=x onerror="window.__xss=1">'


def audit_timeline(base: str) -> None:
    clause("S6-C")
    st, ing = http_json(base + "/inbound", "POST", {
        "buyer_id": "BY-AUDIT-X", "language": "en", "company": "Audit Buyer Ltd", "country": "GB",
        "message": "Quote 100 pcs of X-200 FOB Shenzhen."})
    tid, did = ing.get("thread_id"), ing.get("draft_id")
    st, tok = http_json(base + f"/threads/{tid}/gate-token", "POST",
                        {"draft_id": did, "action": "sign_off"})
    st, _ = http_json(base + f"/threads/{tid}/signoff", "POST",
                      {"draft_id": did, "action": "transfer", "reason": XSS},
                      headers={"X-Gate-Token": tok.get("token")})
    check("转交接口接受任意 reason（写操作可达）", st == 200, str(st))
    st, wb = http_json(base + "/workbench")
    t = next((x for x in wb.get("threads", []) if x.get("id") == tid), None)
    texts = [x.get("text", "") for x in (t or {}).get("timeline", [])]
    hit = any(XSS in s for s in texts)
    check("时间线文本不做转义（载荷原样出现 → 渲染层必须自己转义）",
          hit, "命中" if hit else "未命中")
    note("后端不转义是**可接受的**（后端只管存），但前端渲染时必须转义 —— 见 §S6-D")


# --------------------------------------------------------------------------
# §S6-D 口径（成本 / KPI）
# --------------------------------------------------------------------------
def audit_cost(base: str, db_path: str) -> None:
    clause("S6-D")
    from datetime import datetime, timezone
    from tradedesk.domain.models import LLMUsage
    from tradedesk.store.sqlite_store import SqliteStateStore

    st = SqliteStateStore(db_path)
    now = datetime.now(timezone.utc)
    # 一条询盘：一次对话（0.015 元）+ 一次嵌入（0.005 元）= 这条询盘 0.02 元
    st.record_llm_usage(LLMUsage(id="audit-call-1", thread_id="T-AUDIT", draft_id="D",
                                 lang="en", provider="minimax", model="MiniMax-M3",
                                 prompt_tokens=100, completion_tokens=50, total_tokens=150,
                                 http_calls=1, cost_cny=0.015, ts=now))
    st.record_llm_usage(LLMUsage(id="audit-call-2", thread_id="T-AUDIT", draft_id="D",
                                 lang="en", provider="minimax", model="embo-01",
                                 prompt_tokens=10, completion_tokens=0, total_tokens=10,
                                 http_calls=1, cost_cny=0.005, ts=now))
    # 一条**单价未核实**的用量（cost_cny=None）：不得计入成本，但计数要在
    st.record_llm_usage(LLMUsage(id="audit-call-3", thread_id="T-AUDIT", draft_id="D",
                                 lang="en", provider="minimax", model="embo-01",
                                 prompt_tokens=10, completion_tokens=0, total_tokens=10,
                                 http_calls=1, cost_cny=None, ts=now))
    st.close()

    st, wb = http_json(base + "/workbench")
    kpis = wb.get("kpis", {})
    check("今日成本只累加单价已核实的行（未核实不计入）",
          abs(float(kpis.get("costToday") or 0) - 0.02) < 1e-6,
          "costToday=%s（应为 0.02）" % kpis.get("costToday"))
    check("已计价覆盖率可见（priced_count / usageCount）",
          kpis.get("pricedCount") == 2 and kpis.get("usageCount") == 3,
          "priced=%s usage=%s" % (kpis.get("pricedCount"), kpis.get("usageCount")))

    by_lang = {c["lang"]: c for c in wb.get("costByLang", [])}
    en = by_lang.get("en", {})
    y = float(en.get("y") or 0)
    check("成本看板的“单位成本”口径是**每条询盘**（PRD 12.4：≤0.2 元/条）",
          abs(y - 0.02) < 1e-6,
          "这条询盘共 3 次调用、0.02 元：按“每条询盘”应是 0.02，"
          "读模型给的是 %s（= 0.02 / 3 次调用）—— 看板标的是“元 / 条询盘”" % y)

    # draftReady 的口径
    threads = wb.get("threads", [])
    with_draft = sum(1 for t in threads if (t.get("draft") or {}).get("body"))
    check("kpis.draftReady = 真正有草稿的线程数",
          int(kpis.get("draftReady") or 0) == with_draft,
          "draftReady=%s，实际有草稿=%s（= 线程数 %s - 待签核 %s：把名单阻断/已发送的线程也算成了“草稿就绪”）"
          % (kpis.get("draftReady"), with_draft, len(threads), kpis.get("pending")))

    # sent 必须是数字（不是 None / 不是事件对象）
    check("kpis.sent 是数字（已发送线程数）",
          isinstance(kpis.get("sent"), int),
          "sent=%r（类型 %s）—— 读模型的 sent 被后面的循环变量覆盖了"
          % (kpis.get("sent"), type(kpis.get("sent")).__name__))


# --------------------------------------------------------------------------
# §S6-E 前端渲染层（Node 真跑 app.js）
# --------------------------------------------------------------------------
def audit_render(base: str) -> None:
    clause("S6-E")
    node = shutil.which("node")
    if not node:
        note("本机没有 node，跳过渲染层审核", "装了 node 再跑一次")
        return
    proc = subprocess.run([node, str(ROOT / "scripts" / "audit_s6_ui.js"), base],
                          cwd=str(ROOT), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    print(proc.stdout.rstrip())
    if proc.stderr.strip():
        print(proc.stderr.rstrip()[:1500])
    check("渲染层审核脚本跑通", proc.returncode in (0, 1), "exit=%d" % proc.returncode)


# --------------------------------------------------------------------------
# §S6-F 静态与流程
# --------------------------------------------------------------------------
def audit_static() -> None:
    clause("S6-F")
    app = (ROOT / "ui-prototype" / "assets" / "app.js").read_text(encoding="utf-8")
    cfg = (ROOT / "ui-prototype" / "assets" / "config.js").read_text(encoding="utf-8")
    wb_py = (ROOT / "src" / "tradedesk" / "api" / "workbench.py").read_text(encoding="utf-8")

    # F1 库版本写死
    import re
    hard = re.findall(r"kb-\d{4}\.\d{2}-[a-z]", app)
    check("知识库页的库版本号来自读模型（不得在代码里写死）",
          not hard, "app.js 写死 " + ",".join(sorted(set(hard))) if hard else "")

    # F2 渲染层转义：时间线与草稿正文
    check("审计时间线文本必须转义（x.text 直接进 innerHTML）",
          "esc(x.text)" in app or "esc(x?.text)" in app,
          "app.js 里时间线文本没有走 esc()")
    check("草稿正文必须转义（d.body 直接进 innerHTML）",
          "esc(d.body)" in app, "app.js 里草稿正文没有走 esc()")

    # F3 FR-ORC-10 的四类筛选（注意：不能用子串 "wait" —— await 里也有）
    uses_wait = re.search(r"\.wait", app) is not None
    check("线程看板支持第 4 类筛选：等待人审时长（FR-ORC-10）",
          uses_wait, "app.js 里没有任何按 t.wait（等待人审时长）筛选或排序的逻辑")

    # F4 CORS：config.js 说了，后端有没有？
    has_cors = "CORSMiddleware" in "".join(
        p.read_text(encoding="utf-8") for p in (ROOT / "src" / "tradedesk" / "api").glob("*.py"))
    check("config.js 文档说的跨地址调试可用（后端需允许 CORS）",
          has_cors or "CORS" not in cfg,
          "config.js 写着“跨地址调试…后端需允许 CORS”，但后端没有 CORS 中间件 —— 跨地址一律被浏览器拦")

    # F5 无出处的常量
    budget = re.findall(r'"budget"\s*:\s*([0-9.]+)', wb_py)
    check("看板预算（budget）有出处，不是写死的常量",
          not budget, "workbench.py 写死 budget=%s 元/条；PRD 12.4 的硬指标是 ≤0.2 元/条" % budget[0]
          if budget else "")

    # F6 验收基线文档
    doc = ROOT / "docs" / "S6-ACCEPTANCE.md"
    check("有 S6 的验收基线文档（条款号 = 验收判据）",
          doc.exists(), "缺 docs/S6-ACCEPTANCE.md —— 判据只散在脚本里，没法逐条对账")

    # F7 跨天时间戳
    check("跨天消息的时间戳不恒为“今天”",
          'strftime("今天' not in wb_py,
          "workbench.py 里消息时间写死成 strftime(\"今天 %H:%M\") —— "
          "跨天的来信在会话栏里也显示成“今天”")


# --------------------------------------------------------------------------
# §S6-G 跨后端一致性（build_workbench 在两个后端上跑同一场景）
# --------------------------------------------------------------------------
def _digest(store) -> dict:
    """在给定后端上跑同一组场景，返回可比对的摘要。"""
    from tradedesk.api.deps import AppContext, offline_config
    from tradedesk.api.workbench import build_workbench
    from tradedesk.config import get_settings
    from tradedesk.domain.enums import Language

    cfg = offline_config(get_settings())
    ctx = AppContext(store=store, config=cfg)
    sup = ctx.supervisor
    sup.ingest(buyer_id="BY-D1", message="Quote 5000 pcs of X-200 FOB Shenzhen, CE?",
               language=Language.EN, company="Brasil Distribuidora Ltda", country="BR")
    sup.ingest(buyer_id="BY-D2", message="مرحبا، نطلب التعامل عن الموديل M-77",
               language=Language.AR, company="Demo Metal Trading SARL", country="SA")
    sup.ingest(buyer_id="BY-D3", message="Send me your best price.",
               language=Language.EN, company="Unknown Buyer", country="")
    wb = build_workbench(ctx)
    return {
        "threads": sorted(
            (t.get("id"), t.get("state"), t.get("risk"), t.get("stage"),
             tuple(sorted(e.get("v") for e in t.get("evidence", [])
                          if str(e.get("v", "")).startswith("CN-"))))
            for t in wb.get("threads", [])),
        "pending": wb.get("kpis", {}).get("pending"),
        "draftReady": wb.get("kpis", {}).get("draftReady"),
        "sent": wb.get("kpis", {}).get("sent"),
        "gates": sorted((g.get("name"), g.get("state")) for g in wb.get("gates", [])),
    }


def audit_cross_backend(dsn: str, db_path: str) -> None:
    clause("S6-G")
    from tradedesk.store.sqlite_store import SqliteStateStore
    # **两侧都用干净的库**：否则 SQLite 侧会带上前面章节造的线程，比的就不是同一场景了。
    fresh = tempfile.mkdtemp(prefix="td-audit-s6-x-")
    _TMPDIRS.append(fresh)
    lite = _digest(SqliteStateStore(os.path.join(fresh, "x.db")))
    note("SQLite 摘要", json.dumps(lite["threads"], ensure_ascii=False))
    if not dsn:
        note("本次未跑 PostgreSQL", "加 --dsn <DSN> 再跑一次（要指向可自建库的 DSN）")
        return
    try:
        from tradedesk.store.postgres_store import PostgresStateStore
        head = pg_split(dsn)
        name = "audit_s6_tmp"
        try:
            pg_exec(head + "postgres", f'DROP DATABASE IF EXISTS "{name}"')
        except Exception:
            pass
        pg_exec(head + "postgres", f'CREATE DATABASE "{name}"')
        _PG_DBS.append((head, name))
        pg = _digest(PostgresStateStore(head + name, vector_dim=4))
        check("SQLite 与 PostgreSQL 的工作台读模型一致",
              lite["threads"] == pg["threads"] and lite["pending"] == pg["pending"]
              and lite["draftReady"] == pg["draftReady"]
              and lite["gates"] == pg["gates"],
              "SQLite=%s / PG=%s" % (json.dumps(lite["threads"], ensure_ascii=False),
                                     json.dumps(pg["threads"], ensure_ascii=False)))
    except Exception as exc:  # noqa: BLE001
        note("PostgreSQL 审核跑不起来", str(exc)[:200])


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="S6 独立审核：前端联调 + 读模型")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--dsn", default=None, help="PostgreSQL DSN（跨后端一致性）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    print("=" * 70)
    print("S6 独立审核 —— 工作台读模型 + 前端接真数据")
    print("=" * 70)

    tmp = tempfile.mkdtemp(prefix="td-audit-s6-")
    _TMPDIRS.append(tmp)
    db_path = os.path.join(tmp, "s6.db")
    port = args.port or free_port()
    proc = None
    try:
        proc, base = start_server(port, db_path)
        print("后端：%s（临时库，不调云端、不计费）" % base)

        st, wb = http_json(base + "/workbench")
        check("GET /workbench 返回 200", st == 200, str(st))
        # 先造出三类线程（正常 / 名单命中 / 信息不足），读模型的判据才有东西可看
        http_json(base + "/inbound", "POST", {
            "buyer_id": "BY-S6-A", "language": "en", "company": "Brasil Distribuidora Ltda",
            "country": "BR",
            "message": "Quote 5000 pcs of X-200 FOB Shenzhen, do you have CE certification?"})
        http_json(base + "/inbound", "POST", {
            "buyer_id": "BY-S6-C", "language": "ar", "company": "Demo Metal Trading SARL",
            "country": "SA", "message": "مرحبا، نطلب التعامل عن الموديل M-77"})
        http_json(base + "/inbound", "POST", {
            "buyer_id": "BY-S6-D", "language": "en", "company": "Unknown Buyer",
            "country": "", "message": "Send me your best price for the manual hydraulic hand truck."})
        st, wb = http_json(base + "/workbench")
        if st == 200:
            audit_contract(base, wb)
        audit_writes(base)
        audit_timeline(base)
        audit_cost(base, db_path)
        audit_render(base)
        audit_static()
        audit_cross_backend(args.dsn, db_path)
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
        for head, name in _PG_DBS:
            try:
                pg_exec(head + "postgres", f'DROP DATABASE IF EXISTS "{name}"')
            except Exception:
                pass
        for d in _TMPDIRS:
            shutil.rmtree(d, ignore_errors=True)

    ok = sum(1 for r in RESULTS if r["level"] == "PASS")
    bad = [r for r in RESULTS if r["level"] == "FAIL"]
    notes = [r for r in RESULTS if r["level"] == "NOTE"]
    print("\n" + "=" * 70)
    print("审核结果：通过 %d，FAIL %d，记账 %d，共 %d 项"
          % (ok, len(bad), len(notes), len(RESULTS)))
    for r in bad:
        print("  ✗ [%s] %s — %s" % (r["clause"], r["name"], r["detail"]))
    print("=" * 70)

    if args.json:
        print(json.dumps(RESULTS, ensure_ascii=False, indent=2))
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
