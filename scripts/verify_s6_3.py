# -*- coding: utf-8 -*-
"""S6-3 验证脚本 —— 灰度与回滚（发布单元四类独立回滚 / 按企业分桶 / 观察窗 / 双索引）。

用法
    python scripts/verify_s6_3.py                     # SQLite
    python scripts/verify_s6_3.py --backend both      # SQLite + PostgreSQL
    python scripts/verify_s6_3.py --dsn <DSN>

本轮验证什么
    1. 发布清单校验（审批必填 / 证据要求 / 单灰度 / 无变化不建发布）
    2. 按企业分桶与语种灰度（PILOT 白名单、同一企业结果一致、空公司不进灰度）
    3. 推进档位（观察窗不许跳、一次一档、P20 比例精确、P100 切四类别名）
    4. 分类别回滚（回基线 / 回上一版 / 其他类别不受影响 / 审计留痕）
    5. 落库与审计（发布清单、别名、重启后仍在、审计链完整）
    6. 双索引：建新版本**不切 active**，灰度企业读到新价、稳定企业仍读旧价，回滚不删数据
    7. HTTP 层（发布台账 / 建发布 / 推进 / 回滚的状态码）

本轮不验证什么
    真实云端嵌入与真实模型出稿（S4 / S5 已各自验证）；CI、镜像扫描、三环境发布流水线
    属于**生产化项**（PRD 13.4.11），demo 阶段不实现。
    名单库数据仍是结构化演示数据，不是真实名单。
    向量用**假行 fixture**（只验存储与切换行为，不是真嵌入能力）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _setup_stdout() -> None:
    """自适应输出编码：tty 用控制台原生编码，非 tty 用 UTF-8，缺字用 replace 兜底。"""
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

from tradedesk.data.kb_seed import build_demo_chunks, load_kb          # noqa: E402
from tradedesk.domain.enums import Language                             # noqa: E402
from tradedesk.release.bucketing import enterprise_bucket, in_canary, normalize_company  # noqa: E402
from tradedesk.release.manifest import baseline_components              # noqa: E402
from tradedesk.release.service import ReleaseError, ReleaseService      # noqa: E402
from tradedesk.store.postgres_store import PostgresStateStore, DEFAULT_DSN  # noqa: E402
from tradedesk.store.sqlite_store import SqliteStateStore               # noqa: E402

RESULTS = []

KB_BASE = "kb-2026.11-c"
KB_V2 = "kb-2026.11-c-v2"
LIST_BASE = "sanctions-2026.11-a"
T0 = datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc)


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append({"name": name, "ok": bool(ok), "detail": detail})
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (f" — {detail}" if detail else ""))
    return bool(ok)


def section(title: str) -> None:
    print("\n" + title)
    print("-" * max(len(title), 60))


def _svc(store, kb=KB_BASE, observe=24) -> ReleaseService:
    return ReleaseService(store, baseline_components(kb, LIST_BASE), observe_hours=observe)


def _expect_code(svc: ReleaseService, code: str, **kw) -> str:
    """调用 create 并返回错误码（期望以 ReleaseError 结束）。"""
    args = dict(components={"kb_version": "kb-2026.11-d"},
                approver="张三", change_notes="知识库换版本",
                rollback_plan="回滚 knowledge 类别",
                pilot_companies=["Pilot Corp"])
    args.update(kw)
    try:
        svc.create(**args)
    except ReleaseError as exc:
        return exc.code
    return "no_error"


# ====================================================================== 1
def scenario_1_validation(store):
    section("1) 发布清单校验（审批 / 证据 / 单灰度 / 无变化）")
    svc = _svc(store)
    check("缺审批人被拒", _expect_code(svc, "validation", approver="") == "validation")
    check("缺变更说明被拒", _expect_code(svc, "validation", change_notes="") == "validation")
    check("缺回滚预案被拒", _expect_code(svc, "validation", rollback_plan="") == "validation")
    check("灰度白名单为空被拒",
          _expect_code(svc, "validation", pilot_companies=[]) == "validation")
    check("不支持的语种被拒",
          _expect_code(svc, "validation", langs=["fr"]) == "validation")
    check("未知组件被拒",
          _expect_code(svc, "validation", components={"mystery": "1"}) == "validation")
    check("组件版本号为空被拒",
          _expect_code(svc, "validation", components={"kb_version": "  "}) == "validation")
    check("与稳定版无差异不建发布",
          _expect_code(svc, "no_change", components={"kb_version": KB_BASE}) == "no_change")
    check("Agent 变更缺回归证据被拒",
          _expect_code(svc, "regression_required",
                       components={"prompt_version": "1.1.0"}) == "regression_required")
    check("名单库变更缺 L-6 证据被拒",
          _expect_code(svc, "risk_evidence_required",
                       components={"list_version": "sanctions-2026.12-a"}) == "risk_evidence_required")

    m = svc.create(components={"kb_version": "kb-2026.11-d"}, approver="张三",
                   change_notes="知识库换版本", rollback_plan="回滚 knowledge 类别",
                   pilot_companies=["Pilot Corp"], langs=["en", "es"],
                   release_id="REL-DUP", now=T0)
    check("发布创建成功并进入 PILOT 灰度",
          m.stage == "PILOT" and m.status == "canary" and m.changed_categories == ["knowledge"],
          "changed=%s" % m.changed_categories)
    check("换知识库时索引版本同步跟随",
          m.components.get("index_version") == "kb-2026.11-d",
          "index=%s" % m.components.get("index_version"))
    check("发布号重复被拒",
          _expect_code(svc, "duplicate_release", release_id="REL-DUP") == "duplicate_release")
    check("同时只允许一个灰度",
          _expect_code(svc, "canary_in_flight") == "canary_in_flight")
    return svc, m


# ====================================================================== 2
def scenario_2_bucketing(store, svc, m):
    section("2) 按企业分桶与语种灰度")
    check("公司名规范化：大小写/全角/空白一致",
          normalize_company("  ＡＣＭＥ   Trading ") == normalize_company("acme trading"))
    b1 = enterprise_bucket("Pilot Corp")
    check("分桶确定性且落在 0-99",
          b1 == enterprise_bucket("Pilot Corp") and 0 <= b1 <= 99,
          "bucket=%d" % b1)
    eff = svc.resolve("Pilot Corp", "en")
    check("PILOT 白名单企业命中灰度", eff.source == "canary",
          "source=%s kb=%s" % (eff.source, eff.components.get("kb_version")))
    check("白名单外企业走基线", svc.resolve("Other Ltd", "en").source == "baseline")
    check("公司名大小写/空格不同仍命中同一版本",
          svc.resolve(" pilot  corp ", "en").components.get("kb_version")
          == eff.components.get("kb_version"))
    check("限定语种之外不进灰度", svc.resolve("Pilot Corp", "ar").source == "baseline")
    check("语种未知不进灰度（保守）", svc.resolve("Pilot Corp", "").source == "baseline")
    check("公司名缺失不进灰度", svc.resolve("", "en").source == "baseline")
    again = svc.resolve("Pilot Corp", "en")
    check("同一企业两次分流结果一致",
          again.components.get("kb_version") == eff.components.get("kb_version"))
    check("in_canary 与白名单判定一致",
          in_canary(m, "Pilot Corp", "en") is True and in_canary(m, "Other Ltd", "en") is False)


# ====================================================================== 3
def scenario_3_advance(store, svc, m):
    section("3) 推进档位（观察窗 / 不许跳档 / P20 比例 / P100 切别名）")
    try:
        svc.advance(m.release_id, now=T0 + timedelta(hours=23))
        check("观察窗未满拒绝推进", False, "本该拒绝却通过了")
    except ReleaseError as exc:
        check("观察窗未满拒绝推进", exc.code == "observe_window", exc.code)

    m = svc.advance(m.release_id, now=T0 + timedelta(hours=25))
    check("一次只进一档（PILOT → P20）", m.stage == "P20", "stage=%s" % m.stage)
    check("进档后重新计观察窗",
          svc.advance.__name__ == "advance" and m.stage_entered_at == T0 + timedelta(hours=25))

    names = ["Demo Import GmbH %03d" % i for i in range(200)]
    selected = [n for n in names if in_canary(m, n, "en")]
    expected = [n for n in names if enterprise_bucket(n) < 20]
    check("P20 命中集合与桶号 <20 完全一致", selected == expected,
          "命中 %d / 200" % len(selected))
    check("P20 命中比例落在合理区间", 10 <= len(selected) <= 50,
          "命中 %d / 200" % len(selected))
    check("P20 档白名单企业仍在灰度内（不被静默踢出）",
          in_canary(m, "Pilot Corp", "en") is True
          and svc.resolve("Pilot Corp", "en").source == "canary")
    check("P20 档非白名单企业按桶分流",
          in_canary(m, "Demo Import GmbH 001", "en")
          == (enterprise_bucket("Demo Import GmbH 001") < 20))

    m = svc.advance(m.release_id, now=T0 + timedelta(hours=50))
    check("第二档推进到 P50", m.stage == "P50", "stage=%s" % m.stage)
    m = svc.advance(m.release_id, now=T0 + timedelta(hours=75))
    check("第三档推进到 P100 且状态转全量",
          m.stage == "P100" and m.status == "full", "stage=%s status=%s" % (m.stage, m.status))
    aliases = store.list_release_aliases()
    check("P100 切换四类别名",
          sorted(aliases) == ["agent", "app", "knowledge", "sanctions"],
          "别名=%s" % sorted(aliases))
    from tradedesk.release.manifest import COMPONENT_CATEGORY, COMPONENT_KEYS
    check("索引版本是独立登记的组件（PRD 13.4.8 回滚行）",
          "index_version" in COMPONENT_KEYS
          and COMPONENT_CATEGORY["index_version"] == "knowledge",
          "组件=%s" % "、".join(COMPONENT_KEYS))
    check("换知识库时索引版本同步跟随（块与索引同版本）",
          svc.stable_components().get("index_version") == "kb-2026.11-d",
          "index=%s" % svc.stable_components().get("index_version"))
    check("全量后分流走稳定版", svc.resolve("Pilot Corp", "en").source == "stable")
    check("全量后稳定版已是新知识库",
          svc.resolve("Pilot Corp", "en").components.get("kb_version") == "kb-2026.11-d")
    try:
        svc.advance(m.release_id, now=T0 + timedelta(hours=100))
        check("全量发布不能再推进", False, "本该拒绝却通过了")
    except ReleaseError as exc:
        check("全量发布不能再推进", exc.code == "not_canary", exc.code)
    return m


# ====================================================================== 4
def scenario_4_rollback(store, svc, m):
    section("4) 分类别回滚（回基线 / 回上一版 / 类别独立 / 审计留痕）")
    try:
        svc.rollback(m.release_id, "knowledge_v2")
        check("非法回滚类别被拒", False, "本该拒绝却通过了")
    except ReleaseError as exc:
        check("非法回滚类别被拒", exc.code == "bad_category", exc.code)
    try:
        svc.rollback("REL-NOT-EXIST", "knowledge")
        check("回滚不存在的发布被拒", False, "本该拒绝却通过了")
    except ReleaseError as exc:
        check("回滚不存在的发布被拒", exc.code == "not_found", exc.code)
    try:
        svc.advance("REL-NOT-EXIST")
        check("推进不存在的发布被拒", False, "本该拒绝却通过了")
    except ReleaseError as exc:
        check("推进不存在的发布被拒", exc.code == "not_found", exc.code)

    m2 = svc.rollback(m.release_id, "knowledge", now=T0 + timedelta(hours=80))
    check("回滚后状态转为已回滚", m2.status == "rolled_back", "status=%s" % m2.status)
    alias = store.get_release_alias("knowledge")
    check("knowledge 别名回到基线版本",
          alias is not None and alias["components"]["kb_version"] == KB_BASE,
          "kb=%s" % (alias or {}).get("components", {}).get("kb_version"))
    check("其他类别别名不受影响",
          store.get_release_alias("app") is not None
          and store.get_release_alias("sanctions") is not None)
    check("回滚后分流回到基线价格版本",
          svc.resolve("Pilot Corp", "en").components.get("kb_version") == KB_BASE)

    # 第二个发布 → 回滚时应当回到**上一个**发布版本（而不是基线）
    m3 = svc.create(components={"kb_version": "kb-2026.11-e"}, approver="李四",
                    change_notes="再换一版知识库", rollback_plan="回滚 knowledge 类别",
                    pilot_companies=["Pilot Corp"], now=T0 + timedelta(hours=90))
    svc.rollback(m3.release_id, "knowledge", now=T0 + timedelta(hours=91))
    alias = store.get_release_alias("knowledge")
    check("回滚到上一个发布版本（不是基线）",
          alias is not None and alias["release_id"] == m.release_id
          and alias["components"]["kb_version"] == "kb-2026.11-d",
          "prev=%s" % (alias or {}).get("release_id"))
    # 再回滚一次，回到基线
    svc.rollback(m.release_id, "knowledge", now=T0 + timedelta(hours=92))
    alias = store.get_release_alias("knowledge")
    check("继续回滚回到基线", alias is not None
          and alias["components"]["kb_version"] == KB_BASE
          and alias["release_id"] == "")


# ====================================================================== 5
def scenario_5_storage_audit(store, svc, db_path, backend, dsn):
    section("5) 落库、审计与重启持久化")
    releases = store.list_releases()
    check("发布清单已落库", len(releases) >= 2, "共 %d 条" % len(releases))
    check("发布清单字段完整",
          all(r.approver and r.rollback_plan and r.changed_categories for r in releases))
    aliases = store.list_release_aliases()
    check("版本别名已落库", isinstance(aliases, dict) and len(aliases) >= 1,
          "别名=%s" % sorted(aliases))
    ok, broken = store.verify_audit_chain()
    check("审计链完整", ok is True and broken is None, "broken=%s" % broken)
    actions = [a.action for a in store.list_audit()]
    check("审计含发布相关动作",
          "release.create" in actions and "release.advance" in actions
          and "release.rollback" in actions, "动作=%s" % sorted(set(actions)))
    payload = [a for a in store.list_audit() if a.action == "release.rollback"]
    check("回滚审计含预案与前后版本",
          bool(payload) and "rollback_plan" in payload[0].payload
          and "from_release" in payload[0].payload)

    if backend == "sqlite":
        reopened = SqliteStateStore(str(db_path))
    else:
        reopened = PostgresStateStore(dsn)
    try:
        check("重启后发布清单仍在", len(reopened.list_releases()) == len(releases))
        check("重启后别名仍在", reopened.get_release_alias("knowledge") is not None)
    finally:
        try:
            reopened.close()
        except Exception:
            pass

# ====================================================================== 6
def scenario_6_dual_index(store):
    section("6) 双索引：新版本不切 active，灰度企业读新价，稳定企业仍读旧价")
    # 重新灌种子，保证两种后端起点一致
    store.clear_kb()
    try:
        store.clear_kb_embeddings()
    except Exception:
        pass
    load_kb(store, KB_BASE)
    v1_chunks = store.list_kb_chunks(KB_BASE)
    check("v1 知识库就绪", len(v1_chunks) > 0, "块数=%d" % len(v1_chunks))

    # v2：chunk_id 加版本后缀。
    # 原因（真实约束，不是讲究）：kb_chunks 的主键是**单列 chunk_id**，
    # 同名会直接覆盖 v1 —— 双索引要共存，就不能让两个版本用同一个主键。
    # 生产化应改为 (chunk_id, kb_version) 复合主键（已列入生产化项）。
    v2_chunks = build_demo_chunks(KB_V2)
    for c in v2_chunks:
        c.chunk_id = c.chunk_id + "-v2"
        if c.doc_type == "产品规格" and c.identifiers.get("sku") == "X-200":
            extra = dict(c.extra or {})
            extra["price_low"] = 999.0
            extra["price_high"] = 999.0
            c.extra = extra
        store.upsert_kb_chunk(c)
    check("v2 与 v1 块共存（未互相覆盖）",
          store.get_kb_chunk("KB-A-SPEC-X-200") is not None
          and store.get_kb_chunk("KB-A-SPEC-X-200-v2") is not None)

    # 假向量 fixture：只验"向量按版本存、切换时选对应那套"，**不是真嵌入能力**
    # 维度必须与 pgvector 列一致（1536），否则 PostgreSQL 后端直接拒绝写入
    fake = [0.001 * ((i % 97) + 1) for i in range(1536)]
    for c in store.list_kb_chunks(KB_BASE):
        store.upsert_kb_embedding(c.chunk_id, KB_BASE, "fixture/no-embed", fake,
                                  "fp-" + c.chunk_id)
    for c in store.list_kb_chunks(KB_V2):
        store.upsert_kb_embedding(c.chunk_id, KB_V2, "fixture/no-embed", fake,
                                  "fp-" + c.chunk_id)
    check("两套向量按版本并存",
          len(store.list_kb_embeddings(KB_BASE)) > 0
          and len(store.list_kb_embeddings(KB_V2)) > 0,
          "v1=%d v2=%d" % (len(store.list_kb_embeddings(KB_BASE)),
                           len(store.list_kb_embeddings(KB_V2))))

    svc = _svc(store)
    rel = svc.create(components={"kb_version": KB_V2}, approver="张三",
                     change_notes="新知识库灰度", rollback_plan="回滚 knowledge 类别",
                     pilot_companies=["Pilot Corp"], now=T0 + timedelta(hours=100))
    check("建了指向 v2 的灰度发布", rel.components.get("kb_version") == KB_V2)

    from tradedesk.api.deps import build_registry
    from tradedesk.retrieval.store_retriever import StoreRetriever
    from tradedesk.runtime.supervisor import Supervisor

    def factory(kb_version: str) -> StoreRetriever:
        return StoreRetriever(store, kb_version)

    registry = build_registry(retriever=StoreRetriever(store, KB_BASE),
                               retriever_factory=factory)
    sup = Supervisor(store=store, registry=registry, release_service=svc)
    msg = "Quote 5000 pcs of X-200 FOB Shenzhen."
    r1 = sup.ingest(buyer_id="B-GRAY", message=msg, language=Language.EN,
                    company="Pilot Corp", country="US")
    r2 = sup.ingest(buyer_id="B-STABLE", message=msg, language=Language.EN,
                    company="Other Ltd", country="US")
    body1 = r1.draft.body if r1.draft else ""
    body2 = r2.draft.body if r2.draft else ""
    check("灰度企业读到新版价格 999.0", "999.0" in body1, body1[:70])
    check("灰度企业草稿不含旧价", "4.85" not in body1)
    check("稳定企业仍读到旧价 4.85", "4.85" in body2, body2[:70])
    check("稳定企业未被动到新版本",
          svc.resolve("Other Ltd", "en").components.get("kb_version") == KB_BASE)
    events = store.list_events(r1.thread_id)
    check("灰度分流留痕（release.assigned 事件）",
          any(e.event_type == "release.assigned" for e in events),
          "事件=%s" % [e.event_type for e in events])
    audits = [a for a in store.list_audit() if a.action == "release.assign"]
    check("灰度分流留痕（审计）", len(audits) >= 1, "审计条数=%d" % len(audits))

    svc.rollback(rel.release_id, "knowledge", now=T0 + timedelta(hours=101))
    check("回滚不删数据：旧块仍在", store.get_kb_chunk("KB-A-SPEC-X-200") is not None)
    check("回滚不删数据：旧向量仍在", len(store.list_kb_embeddings(KB_BASE)) > 0)
    check("回滚不删数据：新版块也仍在", len(store.list_kb_chunks(KB_V2)) > 0)


# ====================================================================== 7
def scenario_7_http(store):
    section("7) HTTP 层（发布台账 / 建发布 / 推进 / 回滚）")
    from fastapi.testclient import TestClient

    from tradedesk.api.app import create_app
    from tradedesk.api.deps import AppContext, offline_config

    # 强制关掉云端对话与嵌入：本轮不联网、不计费
    cfg = offline_config()
    cfg.embedder_enabled = False
    cfg.llm_draft_enabled = False
    ctx = AppContext(store=store, config=cfg)
    client = TestClient(create_app(ctx))

    resp = client.get("/release/status")
    check("GET /release/status 返回 200", resp.status_code == 200, str(resp.status_code))
    body = resp.json() if resp.status_code == 200 else {}
    check("发布台账含默认组件 / 发布列表 / 别名",
          all(k in body for k in ("defaults", "releases", "aliases")))

    payload = {"components": {"app_image": "tradedesk/app:http-canary"},
               "change_notes": "HTTP 建发布", "rollback_plan": "回滚 app 类别",
               "pilot_companies": ["HTTP Pilot Ltd"]}
    resp = client.post("/release", json=payload)
    check("缺审批人的建发布请求返回 422", resp.status_code == 422, str(resp.status_code))

    payload["approver"] = "王五"
    resp = client.post("/release", json=payload)
    check("建发布成功返回 200", resp.status_code == 200, resp.text[:120])
    rid = resp.json().get("release_id", "") if resp.status_code == 200 else ""

    resp = client.get("/release/resolve", params={"company": "HTTP Pilot Ltd", "lang": "en"})
    check("HTTP 分流命中灰度版本",
          resp.status_code == 200 and resp.json().get("source") == "canary",
          resp.text[:120])

    resp = client.post("/release/%s/advance" % rid)
    check("观察窗内推进返回 409", resp.status_code == 409, str(resp.status_code))

    resp = client.post("/release/rollback", json={"release_id": rid, "category": "app"})
    check("按类别回滚返回 200", resp.status_code == 200, resp.text[:120])

    resp = client.post("/release/rollback",
                       json={"release_id": "REL-NOPE", "category": "app"})
    check("回滚不存在的发布返回 404", resp.status_code == 404, str(resp.status_code))

    # ---- 越权路径（鉴权开启时）：审批人只能取自会话，角色要有发布权限 ----
    import os
    from tradedesk.config import reset_settings
    os.environ["TRADEDESK_AUTH_REQUIRED"] = "1"
    reset_settings()
    try:
        resp = client.post("/release", json={"components": {"app_image": "tradedesk/app:authz"},
                                             "approver": "某人", "change_notes": "x",
                                             "rollback_plan": "y",
                                             "pilot_companies": ["Authz Ltd"]})
        check("鉴权开启后无会话建发布返回 401", resp.status_code == 401, str(resp.status_code))

        st = client.post("/auth/login", json={"username": "sales", "password": "demo1234"})
        sales = (st.json() or {}).get("token", "")
        st = client.post("/auth/login", json={"username": "manager", "password": "demo1234"})
        manager = (st.json() or {}).get("token", "")
        check("演示账号可登录拿会话", bool(sales) and bool(manager))

        resp = client.post("/release", json={"components": {"app_image": "tradedesk/app:authz"},
                                             "approver": "业务员自己填", "change_notes": "x",
                                             "rollback_plan": "y",
                                             "pilot_companies": ["Authz Ltd"]},
                           headers={"X-Session-Token": sales})
        check("业务员建发布返回 403（无发布权限）", resp.status_code == 403, str(resp.status_code))

        resp = client.post("/release", json={"components": {"app_image": "tradedesk/app:authz"},
                                             "approver": "某人自己填", "change_notes": "x",
                                             "rollback_plan": "y",
                                             "pilot_companies": ["Authz Ltd"]},
                           headers={"X-Session-Token": manager})
        check("经理建发布返回 200", resp.status_code == 200, resp.text[:100])
        approver = resp.json().get("approver", "") if resp.status_code == 200 else ""
        check("审批人取自会话用户（不认请求体自称）",
              approver == "manager" and approver != "某人自己填", "approver=%s" % approver)

        resp = client.post("/release/rollback",
                           json={"release_id": resp.json().get("release_id", ""),
                                 "category": "app"},
                           headers={"X-Session-Token": sales})
        check("业务员回滚返回 403（无发布权限）", resp.status_code == 403, str(resp.status_code))
    finally:
        os.environ["TRADEDESK_AUTH_REQUIRED"] = "0"
        reset_settings()


# --------------------------------------------------------------- 收尾自检
def scenario_8_final_audit(store):
    section("8) 收尾：审计链与状态自检")
    ok, broken = store.verify_audit_chain()
    check("全部操作后审计链仍然完整", ok is True and broken is None, "broken=%s" % broken)
    actions = sorted({a.action for a in store.list_audit()})
    check("审计动作覆盖发布全流程",
          {"release.create", "release.advance", "release.rollback"} <= set(actions),
          "动作=%s" % actions)


def purge_artifacts(db_path: Path, postgres: bool = False, dsn: str = DEFAULT_DSN) -> list:
    """清理本次验证产生的全部临时物，保证项目目录回到干净状态。"""
    removed = []
    candidates = [db_path, db_path.with_name(db_path.name + "-wal"),
                  db_path.with_name(db_path.name + "-shm")]
    tmp_root = ROOT / "data" / "tmp"
    if tmp_root.exists():
        for p in tmp_root.iterdir():
            candidates.append(p)
    for p in candidates:
        try:
            if p.is_dir():
                import shutil
                shutil.rmtree(p, ignore_errors=True)
                removed.append(str(p.relative_to(ROOT)))
            elif p.exists():
                p.unlink()
                removed.append(str(p.relative_to(ROOT)))
        except Exception:
            pass
    if postgres:
        try:
            st = PostgresStateStore(dsn)
            st.reset()
            st.close()
            removed.append("postgres: %s (表已清空)" % dsn)
        except Exception:
            pass
    return removed


def _run_backend(backend: str, args, all_results) -> int:
    global RESULTS
    RESULTS = []
    db_path = Path(args.db)
    if backend == "sqlite":
        if db_path.exists():
            db_path.unlink()
        for suffix in ("-wal", "-shm"):
            p = db_path.with_name(db_path.name + suffix)
            if p.exists():
                p.unlink()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        print("\n" + "=" * 70)
        print("后端：SQLite（零依赖，本地文件）")
        print("数据库：%s" % db_path)
        store = SqliteStateStore(str(db_path))
    else:
        print("\n" + "=" * 70)
        print("后端：PostgreSQL 16（Docker 容器，本地真实依赖）")
        print("DSN：%s" % args.dsn)
        try:
            store = PostgresStateStore(args.dsn)
        except Exception as e:
            print("  [SKIP] PostgreSQL 不可用，跳过该后端：%s: %s"
                  % (type(e).__name__, str(e)[:110]))
            print("         启动依赖：& $dev up（需要 Docker Desktop 在运行）")
            store = None
        if store is not None:
            store.reset()

    if store is None:
        passed = sum(1 for r in RESULTS if r["ok"])
        total = len(RESULTS)
        print("\n" + "-" * 66)
        print("[sqlite] 结果：%d/%d 项通过（PostgreSQL 后端已跳过：容器未启动）"
              % (passed, total))
        print("-" * 66)
        return 0 if passed == total else 1

    try:
        svc, m = scenario_1_validation(store)
        scenario_2_bucketing(store, svc, m)
        m = scenario_3_advance(store, svc, m)
        scenario_4_rollback(store, svc, m)
        scenario_5_storage_audit(store, svc, db_path, backend, args.dsn)
        scenario_6_dual_index(store)
        scenario_7_http(store)
        scenario_8_final_audit(store)
    finally:
        try:
            store.close()
        except Exception:
            pass

    passed = sum(1 for r in RESULTS if r["ok"])
    total = len(RESULTS)
    print("\n" + "-" * 66)
    print("[%s] 结果：%d/%d 项通过" % (backend, passed, total))
    if passed < total:
        print("失败项：")
        for r in RESULTS:
            if not r["ok"]:
                print("  - %s %s" % (r["name"], r["detail"]))
    print("本轮未覆盖：CI 与镜像扫描、三环境发布流水线、提示词热切换（均为生产化项）。")
    print("名单数据为结构化演示数据，非真实名单；向量为假行 fixture，非真嵌入。")
    print("-" * 66)
    for r in RESULTS:
        r["backend"] = backend
    all_results.extend(RESULTS)

    if not args.keep:
        removed = purge_artifacts(db_path, postgres=(backend == "postgres"), dsn=args.dsn)
        print("\n[清理] 已移除 %d 项验证临时物：" % len(removed))
        for r in removed:
            print("  - %s" % r)
    else:
        print("\n[清理] 已跳过（--keep）")

    return 0 if passed == total else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="S6-3 灰度与回滚验证")
    ap.add_argument("--backend", choices=["sqlite", "postgres", "both"], default="sqlite",
                    help="跑哪个后端（默认 sqlite）")
    ap.add_argument("--db", default=str(ROOT / "data" / "verify_s6_3.db"), help="SQLite 文件路径")
    ap.add_argument("--dsn", default=DEFAULT_DSN, help="PostgreSQL DSN")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--keep", action="store_true", help="保留临时物（默认跑完清理）")
    args = ap.parse_args()

    backends = ["sqlite", "postgres"] if args.backend == "both" else [args.backend]
    all_results = []
    rc = 0
    for backend in backends:
        rc |= _run_backend(backend, args, all_results)
    if args.json:
        print(json.dumps({"results": all_results}, ensure_ascii=False, indent=2))
    return rc


if __name__ == "__main__":
    sys.exit(main())