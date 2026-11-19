# -*- coding: utf-8 -*-
"""S1 验证脚本 —— 时间维度骨架（状态机 / 存储 / 闸门令牌 / 审计哈希链）。

用法
    python scripts/verify_s1.py            # 跑全部场景，打印可读报告
    python scripts/verify_s1.py --json     # 机器可读输出（便于接入 CI）
    python scripts/verify_s1.py --db path  # 指定数据库文件

本轮验证什么
    跨天跨周长事务的状态推进、非法迁移拦截、消息幂等、闸门令牌一次性与绑定校验、
    断点续传（重启后状态完整）、审计链验伪、乐观锁并发冲突拦截。
本轮不验证什么
    真实检索（检索层在 S4）、真实大模型（S5）、HTTP 接口（S2）、名单真实核验（S3）。
    名单数据当前为**结构化演示数据**，不是真实名单。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

def _setup_stdout() -> None:
    """自适应输出编码：
    - 交互式控制台（tty）：沿用控制台原生编码（中文 Windows 上是 GBK/CP936），
      这样在用户终端里中文显示正常；
    - 重定向 / 被工具捕获（非 tty）：改用 UTF-8，避免落盘或抓取时乱码。
    - 若外部已设置 PYTHONIOENCODING，则以环境变量为准。
    """
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradedesk.config import Config                                  # noqa: E402
from tradedesk.data.seed import seed_demo                           # noqa: E402
from tradedesk.domain.enums import Actor, EventType, GateId, Intent, Language, Stage  # noqa: E402
from tradedesk.domain.models import Draft, ThreadEvent, ThreadState  # noqa: E402
from tradedesk.domain.state_machine import (                        # noqa: E402
    TransitionRejected,
    apply_transition,
    build_thread,
    check_transition,
    is_duplicate_message,
)
from tradedesk.runtime.gates import (                               # noqa: E402
    GateTokenService,
    blocking_gates,
    evaluate_gates,
)
from tradedesk.store.sqlite_store import SqliteStateStore           # noqa: E402
from tradedesk.store.postgres_store import PostgresStateStore, DEFAULT_DSN  # noqa: E402

RESULTS = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append({"name": name, "ok": bool(ok), "detail": detail})
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (f" — {detail}" if detail else ""))
    return bool(ok)


def purge_artifacts(db_path: Path, postgres: bool = False, dsn: str = DEFAULT_DSN) -> list:
    """清理本次验证产生的全部临时物，保证项目目录回到干净状态。

    `dsn` 必须是**本次实际写入的那个库**：两个 agent 各用各的库
    （tradedesk_a / tradedesk_b），清理时必须清自己那个，
    否则会误清对方的库，破坏 WORKFLOW.md 第 4 节的数据库隔离。
    """
    removed = []
    candidates = [db_path,
                  db_path.with_name(db_path.name + "-wal"),
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
    # PostgreSQL 侧：清空本次验证写入的数据（只清本 agent 自己的库）
    if postgres:
        try:
            from tradedesk.store.postgres_store import PostgresStateStore
            st = PostgresStateStore(dsn)
            st.reset()
            st.close()
            removed.append(f"postgres: {dsn} (表已清空)")
        except Exception:
            pass
    return removed


def section(title: str) -> None:
    print("\n" + title)
    print("-" * max(len(title), 60))


def now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
def scenario_1_seed(store: SqliteStateStore) -> None:
    section("场景 1 · 建档与初始状态（询盘 → 样品 → 报价 → 订单 的时间轴起点）")
    seed_demo(store)
    threads = store.list_threads()
    customers = [store.get_customer(c) for c in ("BY-BR-01", "BY-AE-01", "BY-UK-01")]
    check("演示线程入库 3 条", len(threads) == 3, f"实际 {len(threads)}")
    check("客户档案入库 3 条", all(c is not None for c in customers))
    t42 = store.get_thread("T-1042")
    check("T-1042 阶段为 QUOTING", t42 is not None and t42.stage.value == "QUOTING")
    check("T-1042 承诺台账保留 2 条", t42 is not None and len(t42.committed_items) == 2)
    check("T-1042 未决问题保留 2 条", t42 is not None and len(t42.open_questions) == 2)
    check("T-1043 带名单命中标记", store.get_thread("T-1043") is not None
          and "sanction_hit" in store.get_thread("T-1043").status_flags)


def scenario_2_transitions(store: SqliteStateStore) -> None:
    section("场景 2 · 合法迁移链与前置条件")
    th = build_thread("T-9001", "BY-BR-01", Language.ES, message_hash="sha-9001")
    store.create_thread(th)
    store.append_event(ThreadEvent(thread_id=th.thread_id, seq=0, event_type=EventType.THREAD_CREATED,
                                   actor=Actor.SYSTEM, payload={"stage": "INQUIRY"}))
    for target in ("SAMPLING", "QUOTING"):
        cur = store.get_thread("T-9001")
        new = apply_transition(cur, target)
        store.save_thread(new)
        store.append_event(ThreadEvent(thread_id="T-9001", seq=0, event_type=EventType.STAGE_CHANGED,
                                       actor=Actor.HUMAN, payload={"to": target}))
    cur = store.get_thread("T-9001")
    check("合法迁移 INQUIRY→SAMPLING→QUOTING 完成", cur.stage.value == "QUOTING")
    check("阶段迁移写入事件日志", len(store.list_events("T-9001")) == 3,
          f"事件数 {len(store.list_events('T-9001'))}")

    # 阶段进入时间：必须是"迁移发生的那一刻"，不是抄线程的最后活动时间。
    # 阶段停留时长、SLA 守护、看板的"等待人审时长"都建在这个字段上。
    stamp = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    probe = build_thread("T-9001B", "BY-BR-01", Language.ES, message_hash="sha-9001b")
    probe.last_activity_at = stamp - timedelta(days=7)
    store.create_thread(probe)
    moved = apply_transition(store.get_thread("T-9001B"), "SAMPLING", at=stamp)
    store.save_thread(moved)
    got = store.get_thread("T-9001B")
    check("阶段进入时间 = 迁移时点", got.stage_entered_at == stamp, str(got.stage_entered_at))
    check("最后活动时间随迁移推进", got.last_activity_at == stamp, str(got.last_activity_at))

    ok, reason = check_transition(cur, "ORDERED")
    check("缺合同版本时 QUOTING→ORDERED 被拦截", ok is False, reason)

    cur = cur.model_copy(deep=True)
    cur.contract_version = "PI-2026-1187"
    ok2, reason2 = check_transition(cur, "ORDERED")
    check("补齐 contract_version 后允许进入 ORDERED", ok2 is True, reason2)
    cur2 = apply_transition(cur, "ORDERED")
    store.save_thread(cur2)
    check("订单状态已落库", store.get_thread("T-9001").stage.value == "ORDERED")


def scenario_3_reject(store: SqliteStateStore) -> None:
    section("场景 3 · 非法迁移被拒绝并留痕")
    th = build_thread("T-9002", "BY-UK-01", Language.EN, message_hash="sha-9002")
    th.stage = Stage.QUOTING
    store.create_thread(th)
    before = len(store.list_events("T-9002"))
    try:
        apply_transition(store.get_thread("T-9002"), "ORDERED")
        check("非法迁移抛出 TransitionRejected", False, "未抛出异常")
    except TransitionRejected as e:
        check("非法迁移抛出 TransitionRejected", True, e.message)
        store.append_event(ThreadEvent(thread_id="T-9002", seq=0, event_type=EventType.TRANSITION_REJECTED,
                                       actor=Actor.SYSTEM, payload={"to": "ORDERED", "reason": e.message}))
        after = len(store.list_events("T-9002"))
        check("拒绝事件已留痕", after == before + 1, f"事件数 {before} → {after}")
    check("线程阶段未被非法迁移改写", store.get_thread("T-9002").stage.value == "QUOTING")


def scenario_4_idempotent(store: SqliteStateStore) -> None:
    section("场景 4 · 消息幂等（重复投递不重复推进）")
    th = build_thread("T-9003", "BY-BR-01", Language.ES, message_hash="sha-abc")
    store.create_thread(th)
    n1 = len(store.list_events("T-9003"))
    dup = is_duplicate_message(store.get_thread("T-9003"), "sha-abc")
    check("相同 message_hash 判定为重复", dup is True)
    check("重复投递不产生新事件", len(store.list_events("T-9003")) == n1)
    check("不同 message_hash 不判重复", is_duplicate_message(store.get_thread("T-9003"), "sha-xyz") is False)


def scenario_5_gates(store: SqliteStateStore) -> None:
    section("场景 5 · 11 条人审闸门评估")
    thread = store.get_thread("T-1042")
    draft = Draft(
        thread_id="T-1042", lang=Language.ES,
        body=("Hola Nuria: 参考价 USD 4.85–5.20 por unidad FOB Shenzhen, "
              "el plazo de entrega es de 25–30 días. El modelo tiene certificación CE "
              "certificado CERT-CE-2041."),
        intent_labels=[Intent.PRICE, Intent.SPEC], confidence=0.86,
        citations=[{"citation_id": "CERT-CE-2041", "type": "证书", "note": "有效期至 2027-03-31"}],
    )
    gates = evaluate_gates(thread, draft)
    hit_ids = {g.gate_id.value for g in blocking_gates(gates)}
    check("闸门规则共 11 条", len(gates) == 11, f"实际 {len(gates)}")
    check("含价格命中 GATE-01", "GATE-01" in hit_ids)
    check("含交期命中 GATE-02", "GATE-02" in hit_ids)
    check("含认证声明命中 GATE-05", "GATE-05" in hit_ids)
    check("高置信不命中 GATE-06", "GATE-06" not in hit_ids)
    check("未命中 GATE-03/04/09", not {"GATE-03", "GATE-04", "GATE-09"} & hit_ids)

    blocked_thread = store.get_thread("T-1043")
    gates2 = evaluate_gates(blocked_thread, None)
    hit2 = {g.gate_id.value for g in blocking_gates(gates2)}
    check("名单命中线程触发 GATE-03", "GATE-03" in hit2)

    complaint = store.get_thread("T-1046").model_copy(deep=True)
    complaint.intent_labels = [Intent.COMPLAINT]
    gates3 = evaluate_gates(complaint, None)
    check("投诉意图触发 GATE-09", "GATE-09" in {g.gate_id.value for g in blocking_gates(gates3)})

    low = Draft(thread_id="T-1046", lang=Language.EN, body="Hello", confidence=0.41)
    gates4 = evaluate_gates(store.get_thread("T-1046"), low)
    check("低置信 0.41 触发 GATE-06", "GATE-06" in {g.gate_id.value for g in blocking_gates(gates4)})

    inj = store.get_thread("T-1046").model_copy(deep=True)
    inj.status_flags = ["injection_detected"]
    gates5 = evaluate_gates(inj, None)
    check("注入特征触发 GATE-07", "GATE-07" in {g.gate_id.value for g in blocking_gates(gates5)})


def scenario_6_tokens(store: SqliteStateStore) -> None:
    section("场景 6 · 闸门令牌（写操作凭证：一次性、绑定、可过期）")
    svc = GateTokenService(store, ttl_seconds=900)
    draft_id = "DRAFT-9004"
    token = svc.issue("T-1042", draft_id, [GateId.PRICE, GateId.DELIVERY])
    check("令牌已签发", bool(token.token))
    check("令牌绑定 thread 与 draft", token.thread_id == "T-1042" and token.draft_id == draft_id)

    check("无令牌时写操作被拒", svc.consume("TOKEN-NOT-EXIST", "T-1042", draft_id) is False)
    check("令牌可消费一次", svc.consume(token.token, "T-1042", draft_id) is True)
    check("同一令牌二次消费失败", svc.consume(token.token, "T-1042", draft_id) is False)

    t2 = svc.issue("T-1042", "DRAFT-OTHER", [GateId.PRICE])
    check("令牌与草稿不匹配时被拒", svc.consume(t2.token, "T-1042", draft_id) is False)

    t3 = svc.issue("T-1042", draft_id, [GateId.PRICE])
    check("令牌绑定其他线程时被拒", svc.consume(t3.token, "T-9999", draft_id) is False)

    expired = svc.issue("T-1042", draft_id, [GateId.PRICE])
    store.expire_gate_token(expired.token, now() - timedelta(seconds=1))
    check("过期令牌被拒", svc.consume(expired.token, "T-1042", draft_id) is False)


def scenario_7_resume(store: SqliteStateStore, db_path: Path) -> None:
    section("场景 7 · 跨天断点续传（进程重启后状态不丢）")
    th = store.get_thread("T-1042")
    th.last_activity_at = now() - timedelta(days=3)   # 模拟 3 天前停在这里
    th.version += 1                                   # 乐观锁：版本号必须前进
    store.save_thread(th)
    before = store.get_thread("T-1042")
    events_before = [e.model_dump(mode="json") for e in store.list_events("T-1042")]
    audit_before = len(store.list_audit())
    dsn = getattr(store, "dsn", None)
    store.close()

    store2 = type(store)(str(db_path)) if isinstance(store, SqliteStateStore) else type(store)(dsn)
    after = store2.get_thread("T-1042")
    check("重启后线程仍存在", after is not None)
    check("阶段未丢失", after.stage.value == before.stage.value, f"{before.stage.value} → {after.stage.value}")
    check("承诺台账完整", len(after.committed_items) == len(before.committed_items) == 2)
    check("未决问题完整", len(after.open_questions) == len(before.open_questions) == 2)
    check("时限完整", len(after.deadlines) == len(before.deadlines) == 2)
    check("事件序列一致", [e.model_dump(mode="json") for e in store2.list_events("T-1042")] == events_before)
    check("审计条数一致", len(store2.list_audit()) == audit_before)
    check("跨天状态可读回（3 天前）", (now() - after.last_activity_at).days >= 3)
    store2.close()


def scenario_8_audit(store: SqliteStateStore) -> None:
    section("场景 8 · 审计哈希链（可验伪）")
    store.append_audit("agent", "draft.generated", "T-1042", {"draft_id": "D-1", "citations": 4})
    store.append_audit("human", "signed_off", "T-1042", {"gates": ["GATE-01"]})
    ok, bad_seq = store.verify_audit_chain()
    check("审计链完整", ok is True, f"异常位置 {bad_seq}")

    rows = store.list_audit()
    target = rows[-1]
    store.tamper_audit_payload(target.seq, {"gates": ["GATE-04"], "tampered": True})
    ok2, bad2 = store.verify_audit_chain()
    check("篡改后审计链校验失败", ok2 is False, f"定位到 seq={bad2}")
    check("定位到的正是被篡改条目", bad2 == target.seq, f"期望 {target.seq}，实际 {bad2}")
    # 还原
    store.tamper_audit_payload(target.seq, target.payload)
    ok3, _ = store.verify_audit_chain()
    check("还原后审计链恢复完整", ok3 is True)


def scenario_9_optimistic_lock(store: SqliteStateStore) -> None:
    section("场景 9 · 乐观锁（并发覆盖被拦截）")
    th = store.get_thread("T-1046")
    stale = th.model_copy(deep=True)            # 版本号相同 = 陈旧副本
    fresh = th.model_copy(deep=True)
    fresh.version = th.version + 1
    store.save_thread(fresh)                    # 正常提交
    try:
        store.save_thread(stale)                # 陈旧副本再提交 → 应被拒
        check("陈旧版本提交被拒绝", False, "未抛出冲突")
    except RuntimeError as e:
        check("陈旧版本提交被拒绝", True, str(e)[:70])


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="S1 验证脚本")
    ap.add_argument("--backend", choices=["sqlite", "postgres", "both"], default="sqlite",
                    help="存储后端：sqlite（默认）/ postgres / both")
    ap.add_argument("--db", default=str(ROOT / "data" / "verify_s1.db"), help="SQLite 文件路径")
    ap.add_argument("--dsn", default=DEFAULT_DSN, help="PostgreSQL DSN")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--keep", action="store_true",
                    help="保留本次验证产生的临时文件（默认结束即清理）")
    ap.add_argument("--self-delete", action="store_true",
                    help="验证结束后删除本脚本本身（用完即焚）")
    args = ap.parse_args()

    backends = ["sqlite", "postgres"] if args.backend == "both" else [args.backend]
    all_results = []
    rc = 0
    for backend in backends:
        rc |= _run_backend(backend, args, all_results)
    if args.json:
        print(json.dumps({"results": all_results}, ensure_ascii=False, indent=2))
    return rc


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
        print(f"数据库：{db_path}")
        store = SqliteStateStore(str(db_path))
    else:
        print("\n" + "=" * 70)
        print("后端：PostgreSQL 16（Docker 容器，本地真实依赖）")
        print(f"DSN：{args.dsn}")
        store = PostgresStateStore(args.dsn)
        store.reset()

    try:
        scenario_1_seed(store)
        scenario_2_transitions(store)
        scenario_3_reject(store)
        scenario_4_idempotent(store)
        scenario_5_gates(store)
        scenario_6_tokens(store)
        scenario_7_resume(store, db_path)
        store = SqliteStateStore(str(db_path)) if backend == "sqlite" else PostgresStateStore(args.dsn)
        scenario_8_audit(store)
        scenario_9_optimistic_lock(store)
    finally:
        try:
            store.close()
        except Exception:
            pass

    passed = sum(1 for r in RESULTS if r["ok"])
    total = len(RESULTS)
    print("\n" + "-" * 66)
    print(f"[{backend}] 结果：{passed}/{total} 项通过")
    if passed < total:
        print("失败项：")
        for r in RESULTS:
            if not r["ok"]:
                print(f"  - {r['name']} {r['detail']}")
    print("本轮未覆盖：真实检索（S4）、真实大模型（S5）、HTTP 接口（S2）、名单真实核验（S3）。")
    print("名单数据为结构化演示数据，非真实名单。")
    print("-" * 66)
    for r in RESULTS:
        r["backend"] = backend
    all_results.extend(RESULTS)

    if not args.keep:
        removed = purge_artifacts(db_path, postgres=(backend == "postgres"), dsn=args.dsn)
        print(f"\n[清理] 已移除 {len(removed)} 项验证临时物：")
        for r in removed:
            print(f"  - {r}")
    else:
        print("\n[清理] 已跳过（--keep）")

    return 0 if passed == total else 1


if __name__ == "__main__":
    _rc = main()
    if "--self-delete" in sys.argv:
        import os
        try:
            os.remove(__file__)
            print(f"\n[清理] 验证脚本已按 --self-delete 删除：{__file__}")
        except Exception as e:
            print(f"\n[清理] 脚本自删除失败：{e}")
    sys.exit(_rc)