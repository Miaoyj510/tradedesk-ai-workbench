# -*- coding: utf-8 -*-
"""S6-3 独立审核 —— 发布 / 灰度 / 回滚（审核 agent 用）。

判据来源：`docs/S6-3-AUDIT-CRITERIA.md`（**写于实现交付之前**，避免被实现反向塑形），
逐条对应 PRD 13.4.8（发布、灰度与回滚）。

范围边界（很重要）
------------------
PRD 第 22 行 + 13.4.12 差距清单第 6 条：13.4 是**生产环境**章节，本期交付的是
"产品机制可验证的原型"，"三环境 + 灰度 + 回滚"在差距清单里标为**未做 / 顺延 v1.1**。
所以本脚本要验的是**机制可验证**，不是生产环境达标 —— 但**口径不许虚高**：
文档若声称覆盖 PRD 13.4.8 的回滚行（镜像 + Prompt/AgentSpec + **索引**版本回滚），
索引那一条就必须算数；否则必须**明确声明未做**。

用法
----
    python scripts/audit_s6_release.py
    python scripts/audit_s6_release.py --dsn "postgresql://...:5433/postgres"
    python scripts/audit_s6_release.py --json

退出码：0 = 无 FAIL；1 = 有 FAIL。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(os.environ.get("TRADEDESK_AUDIT_ROOT") or
            Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "src"))


def _setup_stdout() -> None:
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
    RESULTS.append({"clause": CURRENT_CLAUSE, "name": name, "level": "NOTE", "detail": detail})
    print("  [NOTE] " + name + (f" — {detail}" if detail else ""))


# --------------------------------------------------------------------------
def pg_split(dsn: str):
    head, _, _db = dsn.rpartition("/")
    return head + "/"


def pg_exec(db: str, sql: str) -> None:
    import psycopg
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(sql)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# --------------------------------------------------------------------------
# §S6-H 发布单元与版本化
# --------------------------------------------------------------------------
def audit_units(svc, defaults) -> None:
    clause("S6-H")
    from tradedesk.release import manifest as M

    check("四个回滚类别齐（app / agent / knowledge / sanctions）",
          set(M.CATEGORIES) == {"app", "agent", "knowledge", "sanctions"},
          "实际：" + "、".join(M.CATEGORIES))
    check("每个组件都有归属类别",
          all(M.COMPONENT_CATEGORY.get(k) for k in M.COMPONENT_KEYS),
          "组件：" + "、".join(M.COMPONENT_KEYS))

    # 只改知识库 → 回滚 knowledge 时，另外三类不许动（PRD：四类独立回滚）
    rel = svc.create(components={"kb_version": "kb-test-2"}, approver="审核员",
                     change_notes="只改知识库", rollback_plan="切回 kb-test-1",
                     pilot_companies=["ACME Trading"])
    before = svc.stable_components()
    svc.rollback(rel.release_id, "knowledge")
    after = svc.stable_components()
    others = [k for k in M.COMPONENT_KEYS
              if M.COMPONENT_CATEGORY[k] != "knowledge"]
    moved = [k for k in others if before.get(k) != after.get(k)]
    check("四类独立回滚：回滚 knowledge 不动另外三类", not moved,
          "被连带改动的组件：" + "、".join(moved) if moved else "")

    # 索引版本：PRD 13.4.8 回滚行明写"索引版本回滚（双索引 + 别名切换）"
    has_index = any("index" in k.lower() for k in M.COMPONENT_KEYS)
    declared_gap = _index_gap_declared()
    check("索引版本回滚：要么 knowledge 类含索引版本组件，"
          "要么文档明确声明未做（PRD 13.4.8 回滚行）",
          has_index or declared_gap,
          "组件里没有索引版本：" + "、".join(M.COMPONENT_KEYS)
          + "；文档也未声明该缺口" if not (has_index or declared_gap)
          else ("组件含索引版本" if has_index else "文档已声明为缺口"))

    # 组件版本是数据：改版本号不必改代码
    rel2 = svc.create(components={"prompt_version": "prompt-任意版本号"},
                      approver="审核员", change_notes="换提示词",
                      rollback_plan="切回", regression_evidence="L-1..L-7 回归报告 R-001",
                      pilot_companies=["ACME Trading"])
    check("组件版本是数据（任意版本字符串可生效，不必改代码）",
          rel2.components.get("prompt_version") == "prompt-任意版本号",
          str(rel2.components.get("prompt_version")))


def _index_gap_declared() -> bool:
    """文档里是否明确声明"索引双索引 + 别名切换未做"。"""
    import re
    for rel in ("docs/S6-ACCEPTANCE.md", "docs/NEXT.md", "CLAUDE.md"):
        p = ROOT / rel
        if not p.exists():
            continue
        txt = p.read_text(encoding="utf-8")
        for line in txt.splitlines():
            if ("索引" in line and ("未做" in line or "未实现" in line
                                    or "差距" in line or "v1.1" in line)):
                if re.search(r"索引|双索引", line):
                    return True
    return False


# --------------------------------------------------------------------------
# §S6-I 灰度分流
# --------------------------------------------------------------------------
def _cleanup_canary(svc) -> None:
    """把在灰度中的发布全部回滚掉（让下一段断言从干净状态开始）。

    为什么要清场：业务规则规定"同时只允许一个灰度"，各段断言各自建灰度会互相撞。
    这不是绕过规则 —— 是审核脚本自己按规则收拾干净。
    """
    rel = svc.canary()
    if rel is None:
        return
    for cat in list(rel.changed_categories):
        try:
            svc.rollback(rel.release_id, cat)
        except Exception:
            pass
    for cat in ("app", "agent", "knowledge", "sanctions"):
        if cat not in rel.rolled_back_categories:
            try:
                svc.rollback(rel.release_id, cat)
            except Exception:
                pass


def _new_canary(svc, **kw):
    """先清掉在灰度中的发布，再建一个新的（业务规则：同时只允许一个灰度）。"""
    _cleanup_canary(svc)
    return svc.create(pilot_companies=kw.pop("pilot_companies", ["ACME Trading"]), **kw)


def audit_gray(svc) -> None:
    clause("S6-I")
    _cleanup_canary(svc)
    from tradedesk.release.bucketing import enterprise_bucket, in_canary, normalize_company

    # G1 同企业不同买家同版本
    rel = _new_canary(svc, components={"prompt_version": "p-g1"}, approver="审核员",
                   change_notes="灰度分流测试", rollback_plan="切回",
                   regression_evidence="回归 R-002")
    a = svc.resolve(company="ACME Trading", lang="en")
    b = svc.resolve(company="ACME Trading", lang="es")
    check("G1 同企业不同买家/不同线程拿到同一版本",
          a.components == b.components and a.source == "canary",
          "en=%s / es=%s / source=%s%s" % (
              json.dumps(a.components, ensure_ascii=False),
              json.dumps(b.components, ensure_ascii=False), a.source,
              "（反空断言失败：拿到的不是灰度版本，这条断言就没证明力）"
              if a.source != "canary" else ""))

    # G2 同一买家跨时间稳定（分桶是纯函数）
    first = svc.resolve(company="ACME Trading", lang="en")
    svc.advance(rel.release_id, now=datetime.now(timezone.utc) + timedelta(days=3))
    second = svc.resolve(company="ACME Trading", lang="en")
    check("G2 同一企业跨时间（推进档位后）仍拿到同一版本",
          first.components == second.components and first.source == "canary",
          "推进前=%s / 推进后=%s" % (json.dumps(first.components, ensure_ascii=False),
                                     json.dumps(second.components, ensure_ascii=False)))

    # G3 分桶确定性
    b1 = enterprise_bucket("ACME Trading")
    b2 = enterprise_bucket("ACME Trading")
    check("G3 分桶确定性（同输入同结果，可复现可审计）", b1 == b2,
          "%s vs %s" % (b1, b2))
    check("G3 分桶对大小写/空白不敏感（同一家公司同一桶）",
          enterprise_bucket("  ACME  Trading ") == enterprise_bucket("acme trading"),
          "%s vs %s" % (enterprise_bucket("  ACME  Trading "),
                        enterprise_bucket("acme trading")))

    # G5 观察窗：没到不许进档
    from tradedesk.release.service import ReleaseError
    rel2 = _new_canary(svc, components={"prompt_version": "p-g5"}, approver="审核员",
                    change_notes="观察窗测试", rollback_plan="切回",
                    regression_evidence="回归 R-003")
    blocked = False
    try:
        svc.advance(rel2.release_id)
    except ReleaseError:
        blocked = True
    check("G5 观察窗未满不许进档（每档 ≥1 个工作日）", blocked,
          "立刻推进被拒" if blocked else "观察窗没满竟然放行了")

    # G5b 不许跳档
    one_step = None
    try:
        svc.advance(rel2.release_id, now=datetime.now(timezone.utc) + timedelta(days=3))
        one_step = svc.get(rel2.release_id).stage
    except ReleaseError as exc:
        one_step = "REJECTED: %s" % exc.code
    check("G5b 不许跳档（一次 advance 只进一档：PILOT → P20）",
          one_step == "P20",
          "一次推进后档位=%s（期望 P20；跳到 P50 就是跳过了中间观察期）" % one_step)

    # G6 语种限定
    rel3 = _new_canary(svc, components={"prompt_version": "p-g6"}, approver="审核员",
                    change_notes="语种灰度测试", rollback_plan="切回",
                    regression_evidence="回归 R-004", langs=["es"])
    es = svc.resolve(company="ACME Trading", lang="es")
    en = svc.resolve(company="ACME Trading", lang="en")
    unknown = svc.resolve(company="ACME Trading", lang="")
    check("G6 语种限定生效（限定 es 时 en 不进灰度）",
          es.source == "canary" and en.source != "canary",
          "es.source=%s / en.source=%s" % (es.source, en.source))
    check("G6 语种未知不进灰度（保守：宁可少灰度不可错发版本）",
          unknown.source != "canary", "unknown.source=%s" % unknown.source)

    # 空白名单 / 空公司不进灰度
    empty_co = svc.resolve(company="", lang="en")
    check("拿不到公司名不进灰度（走稳定版）",
          empty_co.source != "canary", "source=%s" % empty_co.source)


# --------------------------------------------------------------------------
# §S6-J 回滚
# --------------------------------------------------------------------------
def audit_rollback(svc, store) -> None:
    clause("S6-J")
    rel = _new_canary(svc, components={"kb_version": "kb-rollback-test",
                                 "prompt_version": "p-rollback"},
                   approver="审核员", change_notes="回滚测试", rollback_plan="切回",
                   regression_evidence="回归 R-005")
    svc.advance(rel.release_id, now=datetime.now(timezone.utc) + timedelta(days=3))
    svc.advance(rel.release_id, now=datetime.now(timezone.utc) + timedelta(days=6))
    svc.advance(rel.release_id, now=datetime.now(timezone.utc) + timedelta(days=9))

    # B2 回滚不丢数据
    try:
        threads_before = len(store.list_threads())
    except Exception:
        threads_before = None
    audits_before = _audit_count(store)
    svc.rollback(rel.release_id, "knowledge")
    svc.rollback(rel.release_id, "agent")
    audits_after = _audit_count(store)
    threads_after = len(store.list_threads()) if threads_before is not None else None
    check("B2 回滚不删业务数据与审计日志（不 truncate）",
          (threads_before is None or threads_before == threads_after)
          and audits_after >= audits_before,
          "线程 %s→%s / 审计 %s→%s" % (threads_before, threads_after,
                                       audits_before, audits_after))

    # B3 回滚有留痕
    events = _audit_events(store, "release.rollback")
    ok = False
    detail = "没找到 release.rollback 审计事件"
    for ev in events:
        payload = _payload_of(ev)
        need = ("category", "from_release", "to_release", "rollback_plan")
        if all(k in payload for k in need):
            ok = True
            detail = "留痕字段：" + "、".join(need)
            break
    check("B3 回滚留痕（含类别 / 从哪到哪 / 回滚预案）", ok, detail)


def _audit_count(store) -> int:
    try:
        return len(store.list_audit())
    except Exception:
        return -1


def _audit_events(store, action: str):
    try:
        rows = store.list_audit()
    except Exception:
        return []
    out = []
    for r in rows:
        action_name = r.get("action") if isinstance(r, dict) else getattr(r, "action", None)
        if action_name == action:
            out.append(r)
    return out


def _payload_of(entry):
    if isinstance(entry, dict):
        return entry.get("payload") or {}
    return getattr(entry, "payload", None) or {}


# --------------------------------------------------------------------------
# §S6-K 变更冻结与发布留痕
# --------------------------------------------------------------------------
def audit_freeze(svc) -> None:
    clause("S6-K")
    from tradedesk.release.service import ReleaseError

    # K1 名单库变更必须附 L-6 证据
    blocked = False
    try:
        svc.create(components={"list_version": "sanctions-新版本"}, approver="审核员",
                   change_notes="换名单库", rollback_plan="切回",
                   pilot_companies=["ACME Trading"])
    except ReleaseError as exc:
        blocked = "risk" in exc.code or "evidence" in exc.code
    check("K1 名单库变更必须附 L-6 风控回归证据（否则建不了）", blocked,
          "无证据被拒" if blocked else "无证据竟然建成了")

    # K2 Agent/提示词变更必须附回归证据
    blocked2 = False
    try:
        svc.create(components={"prompt_version": "p-no-evidence"}, approver="审核员",
                   change_notes="换提示词", rollback_plan="切回",
                   pilot_companies=["ACME Trading"])
    except ReleaseError as exc:
        blocked2 = "regression" in exc.code or "evidence" in exc.code
    check("K2 Agent/提示词变更必须附回归证据（不允许跳过回归直接改）", blocked2,
          "无证据被拒" if blocked2 else "无证据竟然建成了")

    # K3 发布记录 5 要素
    rel = _new_canary(svc, components={"prompt_version": "p-audit"}, approver="审批人张三",
                   change_notes="把提示词升到 p-audit", rollback_plan="切回 prompt-上一版",
                   regression_evidence="L-1..L-7 回归报告 R-006", langs=["en"])
    events = _audit_events(svc.store, "release.create")
    need = ("components", "approver", "change_notes", "rollback_plan",
            "pilot_companies", "langs")
    missing = []
    if events:
        payload = _payload_of(events[-1])
        missing = [k for k in need if k not in payload]
    check("K3 发布留痕含 5 要素（版本号/变更/审批人/灰度范围/回滚预案）",
          bool(events) and not missing,
          "缺：" + "、".join(missing) if missing
          else ("审计事件 %d 条，要素齐" % len(events) if events else "没找到 release.create 审计事件"))

    # K4 同时只允许一个灰度
    blocked3 = False
    try:
        svc.create(components={"prompt_version": "p-second-canary"}, approver="审核员",
                   change_notes="第二个灰度", rollback_plan="切回",
                   regression_evidence="回归 R-007", pilot_companies=["ACME Trading"])
    except ReleaseError as exc:
        blocked3 = "canary" in exc.code
    check("K4 同时只允许一个灰度（否则说不清哪家企业在哪版）", blocked3,
          "第二个灰度被拒" if blocked3 else "第二个灰度竟然建成了")


# --------------------------------------------------------------------------
# §S6-L 口径守卫（不许声称生产达标）
# --------------------------------------------------------------------------
def _cn_num(text: str):
    """中文数字 → 整数（状态卡里步数写的是"十一步"）。"""
    text = str(text).strip()
    if text.isdigit():
        return int(text)
    digits = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}
    if "十" in text:
        left, _, right = text.partition("十")
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        return tens * 10 + ones
    total = 0
    for ch in text:
        if ch in digits:
            total = total * 10 + digits[ch]
    return total


def audit_wording() -> None:
    clause("S6-L")
    import re

    acc = (ROOT / "docs" / "S6-ACCEPTANCE.md")
    txt = acc.read_text(encoding="utf-8") if acc.exists() else ""

    # Z1 不得出现生产口径的达标声明
    bad = [ln.strip() for ln in txt.splitlines()
           if re.search(r"(已|可)?(支持|具备|实现|提供)(生产级)?(灰度发布|灰度回滚|一键回滚|生产回滚|自动回滚)", ln)
           and not re.search(r"(不得|不能|未做|未实现|不准|禁止|顺延|v1\.1|未完成|差距|机制)", ln)]
    check("Z1 验收基线不得出现生产口径的达标声明（本期是机制可验证）",
          not bad, "命中：" + " / ".join(bad[:2]) if bad else "")

    # Z2 PRD 13.4.12 差距清单第 6 条不得被改写为"已做"
    prd = (ROOT / "PRD-外贸业务员的AI数字员工工作台-v1.0.md")
    if prd.exists():
        p = prd.read_text(encoding="utf-8")
        row = [ln for ln in p.splitlines() if "发布与回滚" in ln and "三环境" in ln]
        ok = bool(row) and not re.search(r"\|[^|]*\|\s*已做\s*\|", row[0])
        check("Z2 PRD 13.4.12 差距清单「发布与回滚」仍标为未做/顺延 v1.1",
              ok, row[0].strip()[:120] if row else "没找到该行")

    # Z3 PRD 12.5 门槛 10 仍顺延 v1.1
    if prd.exists():
        p = prd.read_text(encoding="utf-8")
        gate = [ln for ln in p.splitlines()
                if "灰度对照" in ln and "回复率相对提升" in ln and "询盘→报价" in ln]
        ok = bool(gate) and ("v1.1" in gate[0] or "顺延" in gate[0] or "无真实买家流量" in gate[0])
        check("Z3 PRD 12.5 门槛 10（灰度对照）仍顺延 v1.1（无真实买家流量）",
              ok, gate[0].strip()[:120] if gate else "没找到门槛 10")

    # M1 验收基线里 S6-3 要有条款号判据
    has_s63 = "S6-3" in txt
    check("M1 验收基线里 S6-3 有条款号判据", has_s63,
          "docs/S6-ACCEPTANCE.md 里没有 S6-3 章节" if not has_s63 else "")

    # M2 新待验证项编号不得撞 PRD 14.3（现到 V-23）
    if prd.exists():
        p = prd.read_text(encoding="utf-8")
        vnums = sorted({int(m) for m in re.findall(r"\|\s*V-(\d{2})\s*\|", p)})
        top = max(vnums) if vnums else 0
        docs_v = set()
        for rel in ("docs/S6-ACCEPTANCE.md", "docs/NEXT.md"):
            f = ROOT / rel
            if f.exists():
                docs_v |= {int(m) for m in re.findall(r"\bV-(\d{2})\b", f.read_text(encoding="utf-8"))}
        clash = sorted(v for v in docs_v if v <= top and v not in vnums)
        check("M2 新增待验证项编号不得撞 PRD 14.3（现到 V-%02d）" % top,
              not clash, "撞号：" + "、".join("V-%02d" % v for v in clash) if clash else "")

    # M4 门禁步数与状态卡一致
    dev = (ROOT / "dev.ps1")
    nxt = (ROOT / "docs" / "NEXT.md")
    if dev.exists() and nxt.exists():
        d = dev.read_text(encoding="utf-8", errors="replace")
        n = nxt.read_text(encoding="utf-8")
        steps = re.findall(r"check\s+(\d+)/(\d+)", d)
        total = max((int(b) for _a, b in steps), default=0)
        m = re.search(r"check`?\s*(?:现在是|是|现在)?\s*\*\*([0-9]+|[一二三四五六七八九十]+)\s*步", n)
        declared = _cn_num(m.group(1)) if m else None
        check("M4 门禁步数与状态卡写的一致",
              declared is not None and declared == total,
              "dev.ps1 是 %s 步，状态卡写 %s 步" % (total, declared)
              if declared is not None else "状态卡里没解析出 check 的步数（正则没匹配上）")


# --------------------------------------------------------------------------
def _make_service(store, kb_version="kb-test", list_version="sanctions-test"):
    from tradedesk.release.manifest import baseline_components
    from tradedesk.release.service import ReleaseService
    defaults = baseline_components(kb_version, list_version)
    return ReleaseService(store, defaults)


def run_on(store, tag: str) -> None:
    svc = _make_service(store)
    print("\n—— 后端：%s ——" % tag)
    audit_units(svc, svc.defaults)
    audit_gray(svc)
    audit_rollback(svc, store)
    audit_freeze(svc)


def main() -> int:
    ap = argparse.ArgumentParser(description="S6-3 独立审核：发布 / 灰度 / 回滚")
    ap.add_argument("--dsn", default=None, help="PostgreSQL DSN（跨后端一致性）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    print("=" * 70)
    print("S6-3 独立审核 —— 发布 / 灰度 / 回滚（PRD 13.4.8）")
    print("=" * 70)

    tmp = tempfile.mkdtemp(prefix="td-audit-s6r-")
    try:
        from tradedesk.store.sqlite_store import SqliteStateStore
        run_on(SqliteStateStore(os.path.join(tmp, "s6r.db")), "SQLite")
    except Exception as exc:  # noqa: BLE001
        check("审核脚本能在 SQLite 上跑起来", False, "%s: %s" % (type(exc).__name__, exc))

    if args.dsn:
        try:
            from tradedesk.store.postgres_store import PostgresStateStore
            head = pg_split(args.dsn)
            name = "audit_s6r_tmp"
            try:
                pg_exec(head + "postgres", 'DROP DATABASE IF EXISTS "%s"' % name)
            except Exception:
                pass
            pg_exec(head + "postgres", 'CREATE DATABASE "%s"' % name)
            run_on(PostgresStateStore(head + name, vector_dim=4), "PostgreSQL")
            try:
                pg_exec(head + "postgres", 'DROP DATABASE IF EXISTS "%s"' % name)
            except Exception:
                pass
        except Exception as exc:  # noqa: BLE001
            check("审核脚本能在 PostgreSQL 上跑起来", False,
                  "%s: %s" % (type(exc).__name__, exc))

    audit_wording()

    shutil.rmtree(tmp, ignore_errors=True)

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
