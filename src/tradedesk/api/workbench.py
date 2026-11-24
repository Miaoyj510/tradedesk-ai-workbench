# -*- coding: utf-8 -*-
"""工作台读模型（BFF，S6-1a）。

为什么要有这一层
----------------
前端原型（ui-prototype）原本只吃一份 `assets/data.js` 里的 mock。接真数据时若让前端
自己去拼 N+1 个接口（每线程一次 events、一次 signoff-view），有两个问题：

- **闸门判定会被前端重算** —— 而闸门是后端的领域逻辑（`runtime/gates.py`），
  前端重算就等于出现第二份真相（这正是 R17 教训的形态）；
- N+1 请求，一屏几十个线程会把本地服务打爆。

所以这里给一个**聚合读模型**：后端把工作台需要的全部数据组装成一次响应，
且**形状与 mock 同构**（前端渲染逻辑几乎不用改）。

口径声明（哪些是真数据、哪些不是）
--------------------------------
| 字段 | 来源 | 是不是真数据 |
| --- | --- | --- |
| `threads` | 存储层 + 闸门评估 + 事件流 | ✅ |
| `kpis` / `stages` | 线程聚合 | ✅ |
| `costByLang` / `costTrend` | `llm_usage` 表（S5-1b） | ✅（单价未核实的行不计入） |
| `gates` | 闸门规则表 + 实际命中计数 | ✅ |
| `riskEvents` | 线程状态标记与事件流 | ✅ |
| `kbAssets` / `terms` | 知识库块 | ✅ |
| `quality` | **评测脚本的产物**（`dev.ps1 eval`） | ❌ **不是实时数据** |

`quality`（分语种检索质量）**不在实时接口里给**：它来自离线评测，不是一次查询的结果。
给一个"看起来像实时"的质量分比不给更糟 —— 所以这里返回空数组 + `notes`，
由前端显式显示"需运行评测脚本后回填"。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from ..domain.enums import EventType, Intent, Stage
from ..runtime.gates import gate_catalog

if TYPE_CHECKING:  # pragma: no cover
    from .deps import AppContext

STAGE_LABEL = {
    "INQUIRY": "询盘", "SAMPLING": "样品", "QUOTING": "报价", "ORDERED": "订单",
    "DOCS": "单证", "PAYMENT": "收款", "CLOSED": "已关闭", "LOST": "已流失",
}
STAGE_COLOR = {
    "INQUIRY": "#2563EB", "SAMPLING": "#0891B2", "QUOTING": "#7C3AED",
    "ORDERED": "#059669", "DOCS": "#B45309", "PAYMENT": "#475569",
}
INTENT_LABEL = {
    "price": "询价", "sample": "打样", "spec": "规格",
    "logistics": "物流", "complaint": "投诉",
}
INTENT_ORDER = ["price", "sample", "spec", "logistics", "complaint"]

# 风险事件（从线程状态标记与事件流里提取）
RISK_RULES = [
    ("sanction_hit", "名单命中", "high"),
    ("injection_detected", "注入拦截", "high"),
    ("regulation_conflict", "合规降级", "mid"),
    ("awaiting_escalation", "超时升级", "mid"),
    ("llm_degraded", "模型降级", "low"),
]
RISK_TEXT = {
    "sanction_hit": "命中受控名单，已抑制对外输出",
    "injection_detected": "买家消息含指令型文本，已阻断转人工",
    "regulation_conflict": "目的国法规时效冲突，相关话术转人工",
    "awaiting_escalation": "线程超时，已升级至经理",
    "llm_degraded": "模型输出被拒，已降级到模板草稿重生成",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _risk_of(thread, gate_hits: Dict[str, bool]) -> str:
    """风险等级由**闸门命中**推导，不用别的口径。

    为什么不直接看 `status_flags` 里的 `risk_*`：那个标记只在非投诉路径上才会被写入，
    投诉/注入拦截这两条路径上根本没有它 —— 那时就会把高风险显示成低风险。
    """
    if gate_hits.get("GATE-03") or gate_hits.get("GATE-07"):
        return "high"
    if gate_hits.get("GATE-09") or gate_hits.get("GATE-11") or gate_hits.get("GATE-04"):
        return "mid"
    flags = list(getattr(thread, "status_flags", []) or [])
    if "risk_high" in flags or "sanction_hit" in flags:
        return "high"
    if "risk_mid" in flags or "regulation_conflict" in flags:
        return "mid"
    return "low"


def _state_of(thread, events=()) -> str:
    """线程状态（与前端 STATE_LABEL 的键对应）。

    **为什么要看事件流**：投诉路径上的“转人工”是用 `human.takeover` **事件**记的，
    线程标志里只有 `complaint` 而没有 `human_takeover` —— 只看标志会把“已交给人工”显示成“草稿待确认”。
    """
    flags = list(getattr(thread, "status_flags", []) or [])
    if "sanction_hit" in flags:
        return "blocked"
    if "rejected" in flags:
        return "needs_confirmation"
    if "human_takeover" in flags or any(
            e.event_type == EventType.HUMAN_TAKEOVER for e in events):
        return "human_takeover"
    if "awaiting_signoff" in flags:
        return "needs_signoff"
    if getattr(thread, "sent_at", None) is not None:
        return "sent"
    if "needs_human_confirmation" in flags:
        return "needs_confirmation"
    return "ready"


def _remain(due_at, now: datetime) -> Dict[str, str]:
    """时限的剩余文案与级别（前端用 level='over' 表过期）。"""
    if due_at is None:
        return {"remain": "—", "level": "ok"}
    delta = (due_at - now).total_seconds() / 60.0
    if delta < 0:
        return {"remain": "已超时 %d 分钟" % int(-delta), "level": "over"}
    if delta < 60:
        return {"remain": "%d 分钟" % int(delta), "level": "warn"}
    return {"remain": "%d 小时" % int(delta // 60), "level": "ok"}


def _buyer_message(events) -> str:
    """买家原话（S6-1a 补的留痕事件 MESSAGE_RECEIVED）。"""
    for e in reversed(list(events)):
        if e.event_type == EventType.MESSAGE_RECEIVED:
            return str(e.payload.get("message") or "")
    return ""


# 事件 → 时间线文案（前端展示用）
def _timeline(events, now: datetime) -> List[Dict[str, Any]]:
    out = []
    for e in events:
        p = e.payload or {}
        text = None
        accent = False
        if e.event_type == EventType.THREAD_CREATED:
            text = "线程已建立（阶段 %s）" % p.get("stage", "")
        elif e.event_type == EventType.MESSAGE_RECEIVED:
            text = "买家来信（%s）" % (p.get("language") or "")
        elif e.event_type == EventType.GATE_EVALUATED:
            text = "Sentinel 核验 · 风险级别 %s" % (p.get("risk_level") or "")
        elif e.event_type == EventType.DRAFT_GENERATED:
            if p.get("suppressed"):
                text = "风控命中，抑制输出"
                accent = True
            else:
                text = "生成草稿 · 引用 %d 条" % int(p.get("citations") or 0)
        elif e.event_type == EventType.SIGNED_OFF:
            text = "人工签核：%s" % (p.get("action") or "")
            accent = True
        elif e.event_type == EventType.SENT:
            text = "已发送"
            accent = True
        elif e.event_type == EventType.HUMAN_TAKEOVER:
            text = "转人工：%s" % (p.get("reason") or "")
            accent = True
        elif e.event_type == EventType.STAGE_CHANGED:
            text = "阶段推进 → %s" % (p.get("to") or p.get("stage") or "")
        elif e.event_type == EventType.WRITE_REJECTED:
            text = "写操作被拒：%s" % (p.get("reason") or "")
            accent = True
        elif e.event_type == EventType.TRANSITION_REJECTED:
            text = "阶段推进被拒：%s" % (p.get("reason") or "")
        if text is None:
            continue
        out.append({"time": _fmt_when(e.ts, now), "text": text, "accent": accent})
    return out


def _fmt_when(ts: datetime, now: datetime) -> str:
    """相对日期的时间标签。

    **不能恒为"今天"**（R21-12c）：本项目的核心卖点是跨天跨周的询盘线程，
    会话栏与审计时间线都丢掉日期，正好打在卖点上。
    """
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=now.tzinfo)
    d = (ts.date() - now.date()).days
    if d == 0:
        return "今天 " + ts.strftime("%H:%M")
    if d == 1:
        return "昨天 " + ts.strftime("%H:%M")
    return ts.strftime("%m-%d %H:%M")


def _messages(events, company: str, now: datetime) -> List[Dict[str, Any]]:
    """买家原话（前端会话栏）。"""
    out = []
    for e in events:
        if e.event_type != EventType.MESSAGE_RECEIVED:
            continue
        p = e.payload or {}
        out.append({
            "who": "buyer",
            "name": p.get("company") or company,
            "time": _fmt_when(e.ts, now),
            "lang": p.get("language") or "",
            "text": p.get("message") or "",
            "attach": "",
        })
    return out


def _evidence(events) -> List[Dict[str, str]]:
    """Sentinel 证据链（从 GATE_EVALUATED 事件里取）。

    后端的证据条目是 `{\"条目 ID\": ..., \"主体\": ...}` 这种字关字典，
    而前端的证据行是 `{k, v}` —— 这里展平为一个条目一行。
    """
    out = []
    for e in events:
        if e.event_type != EventType.GATE_EVALUATED:
            continue
        for item in (e.payload or {}).get("evidence") or []:
            if isinstance(item, dict):
                for k, v in item.items():
                    out.append({"k": str(k), "v": str(v)})
            else:
                out.append({"k": "证据", "v": str(item)})
    return out


def _thread_payload(ctx, thread, now: datetime) -> Dict[str, Any]:
    store = ctx.store
    sup = ctx.supervisor
    draft = sup.get_draft(thread.thread_id)
    events = store.list_events(thread.thread_id)
    gates = gate_catalog(sup.low_confidence_threshold, sup.amount_threshold)
    from ..runtime.gates import evaluate_gates
    live = evaluate_gates(thread, draft, sup.low_confidence_threshold, sup.amount_threshold)
    hit_map = {g.gate_id.value: g.hit for g in live}

    customer = store.get_customer(thread.buyer_id)
    company = (customer.company if customer else "") or thread.buyer_id
    country = (customer.country if customer else "") or ""
    deadlines = []
    for d in thread.deadlines:
        item = {"label": d.label}
        item.update(_remain(d.due_at, now))
        deadlines.append(item)
    deadlines.sort(key=lambda x: x["label"])

    soon = min([x for x in (_mins_left(d.due_at, now) for d in thread.deadlines)
                if x is not None] or [None])
    wait = int((now - thread.last_activity_at).total_seconds() / 60)
    if wait < 0:
        wait = 0

    draft_view = {
        "lang": draft.lang.value if draft else thread.language.value,
        "body": draft.body if draft else "",
        "intent": [INTENT_LABEL.get(i.value, i.value) for i in (draft.intent_labels if draft else [])],
        "confidence": round(draft.confidence, 2) if draft else 0.0,
        "next": draft.next_action if draft else "",
    }
    citations = []
    for c in (draft.citations if draft else []):
        citations.append({"id": c.citation_id, "type": c.type, "note": c.note})

    from ..runtime.supervisor import draft_id as _draft_id

    return {
        "id": thread.thread_id,
        # draft_id 必须由后端给（签核与发送都要用它签发闸门令牌）——
        # 前端不得自己拼这个规则，规则一变就会拿错令牌。
        "draft_id": _draft_id(thread.thread_id),
        "company": company,
        "country": country,
        "flag": country[:2].upper(),
        "lang": thread.language.value,
        "stage": thread.stage.value,
        "risk": _risk_of(thread, hit_map),
        "state": _state_of(thread, events),
        "sla": soon,
        "wait": wait,
        "intent": list(thread.intent_labels),
        "title": _buyer_message(events)[:120] or thread.thread_id,
        "buyer_message": _buyer_message(events),
        "draft": draft_view,
        "citations": citations,
        "commitments": ["%s %s" % (c.kind, c.value) for c in thread.committed_items],
        "open": [q.text for q in thread.open_questions],
        "deadlines": deadlines,
        "gates": [{"id": g.gate_id.value, "title": g.title, "desc": g.desc,
                    "hit": bool(hit_map.get(g.gate_id.value))} for g in gates],
        "events": [{"seq": e.seq, "type": e.event_type.value, "actor": e.actor.value,
                    "ts": e.ts.isoformat(), "payload": e.payload} for e in events],
        "messages": _messages(events, company, now),
        "timeline": _timeline(events, now),
        "evidence": _evidence(events),
        "signed_off": bool(thread.price_signed_off),
        "sent_at": thread.sent_at.isoformat() if thread.sent_at else None,
    }


def _mins_left(due_at, now: datetime) -> Optional[int]:
    if due_at is None:
        return None
    return int((due_at - now).total_seconds() / 60)


def _kpis(ctx, threads, now: datetime) -> Dict[str, Any]:
    pending = sum(1 for t in threads if "awaiting_signoff" in (t.status_flags or []))
    sla_soon = 0
    for t in threads:
        lefts = [_mins_left(d.due_at, now) for d in t.deadlines]
        lefts = [x for x in lefts if x is not None]
        if lefts and min(lefts) <= 60:
            sla_soon += 1
    sent_count = sum(1 for t in threads if t.sent_at is not None)
    usage = ctx.store.llm_usage_summary()
    cost_today = 0.0
    today = now.date().isoformat()
    day = (usage.get("by_day") or {}).get(today)
    if day:
        cost_today = float(day.get("cost_cny") or 0.0)
    # 首响时长：从事件流算（首个草稿生成 − 收到消息）。
    # 不用 `last_activity_at`：那是“最后一次活动”，不是首响时刻——用它算出来的是“0 分 0 秒”。
    deltas = []
    for t in threads:
        evs = ctx.store.list_events(t.thread_id)
        recv = next((e for e in evs if e.event_type == EventType.MESSAGE_RECEIVED), None)
        draft_ev = next((e for e in evs if e.event_type == EventType.DRAFT_GENERATED), None)
        if recv is not None and draft_ev is not None:
            deltas.append((draft_ev.ts - recv.ts).total_seconds())
    avg_first = "—"
    if deltas:
        avg = int(sum(deltas) / len(deltas))
        avg_first = "%d 分 %d 秒" % (avg // 60, avg % 60)
    # 含人工签核与发送的真实首响：没发出过就没有数字
    sent_deltas = []
    for t in threads:
        evs = ctx.store.list_events(t.thread_id)
        recv = next((e for e in evs if e.event_type == EventType.MESSAGE_RECEIVED), None)
        # 注意变量名（R21-07）：原来这里也叫 `sent`，把上面统计线程数的 `sent` 覆盖了，
        # 于是 kpis.sent 返回的是 None 或一个事件对象，而不是数字。
        sent_ev = next((e for e in evs if e.event_type == EventType.SENT), None)
        if recv is not None and sent_ev is not None:
            sent_deltas.append((sent_ev.ts - recv.ts).total_seconds())
    avg_first_signed = "—"
    if sent_deltas:
        avg = int(sum(sent_deltas) / len(sent_deltas))
        avg_first_signed = "%d 分 %d 秒" % (avg // 60, avg % 60)
    # draftReady = **真正有草稿正文**的线程数（R21-06）。
    # 原来写的是"线程数 − 待签核数"，那会把名单阻断（不生成任何回复）
    # 和已发送的线程都算成"草稿就绪"，口径既不等于"有草稿的线程数"，
    # 也不等于界面上的"草稿可用率 %"。
    draft_ready = 0
    for t in threads:
        d = ctx.supervisor.get_draft(t.thread_id)
        if d is not None and (d.body or "").strip():
            draft_ready += 1
    return {
        "pending": pending,
        "slaSoon": sla_soon,
        "costToday": round(cost_today, 4),
        # 不给"预算"这个无出处的常量（R21-08）：mock 里的 2.4 是"当日预算 ¥2.40"，
        # 与 PRD 12.4 的"≤0.2 元/条"根本不是一个口径，差 12 倍。
        # 改成能自证的写法：当日成本 + PRD 的硬指标本身。
        "costHardCapPerInquiry": 0.2,
        "costUnit": "元/条询盘",
        "costThreadCount": usage.get("thread_count", 0),
        "sent": sent_count,
        "avgFirstResponse": avg_first,
        "avgFirstResponseSigned": avg_first_signed,
        "draftReady": draft_ready,
        "pricedCount": usage.get("priced_count", 0),
        "usageCount": usage.get("count", 0),
    }


def _cost_series(usage: Dict[str, Any]) -> Dict[str, Any]:
    """成本序列 —— **分母是询盘条数，不是调用次数**（R21-05）。

    PRD 12.4 的硬指标是"单位询盘 Token 成本 ≤ 0.2 元/条"，分母就是**询盘条数**。
    一条询盘要走对话 + 嵌入 + 可能的重排，用调用次数当分母会把数字摊薄到 1/3~1/4
    —— 那是"元/次模型调用"，不是"元/条询盘"，两者不能混用。
    调用次数单独给一列（`calls`），不混进单位成本。
    """
    by_lang = []
    for lang, b in sorted((usage.get("by_lang") or {}).items()):
        inquiries = int(b.get("thread_count") or 0)
        calls = int(b.get("count") or 0)
        if inquiries <= 0:
            continue
        by_lang.append({"lang": lang, "n": inquiries, "calls": calls,
                         "y": round(float(b.get("cost_cny") or 0.0) / inquiries, 6)})
    trend = []
    for day in sorted((usage.get("by_day") or {}).keys()):
        b = usage["by_day"][day]
        inquiries = int(b.get("thread_count") or 0)
        calls = int(b.get("count") or 0)
        if inquiries <= 0:
            continue
        trend.append({"d": day[5:], "n": inquiries, "calls": calls,
                      "v": round(float(b.get("cost_cny") or 0.0) / inquiries, 6)})
    return {"costByLang": by_lang, "costTrend": trend}


def _risk_events(ctx, threads, now: datetime) -> List[Dict[str, Any]]:
    out = []
    for t in threads:
        flags = list(t.status_flags or [])
        for flag, label, level in RISK_RULES:
            if flag not in flags:
                continue
            out.append({
                "time": t.last_activity_at.strftime("%H:%M:%S"),
                "type": label,
                "level": level,
                "text": "%s %s" % (t.thread_id, RISK_TEXT.get(flag, flag)),
            })
    out.sort(key=lambda e: e["time"], reverse=True)
    return out[:20]


def _kb_assets(ctx) -> List[Dict[str, Any]]:
    """知识库证据块（一次真实检索的结果）。"""
    from ..retrieval.base import SearchQuery
    query = SearchQuery(text="X-200 价格 交期 证书", lang="zh",
                        buyer_country="", today=_now().date(), top_k=12)
    assets = []
    for h in ctx.retriever.search(query):
        c = h.chunk
        valid = "长期"
        if h.expired:
            valid = "已废止"
        elif h.not_yet_effective:
            valid = "未生效"
        elif c.valid_until is not None:
            valid = "至 %s" % c.valid_until.date().isoformat()
        assets.append({
            "id": c.identifiers.get("doc_ref") or c.identifiers.get("sku") or c.doc_id,
            "type": c.doc_type,
            "lang": c.lang,
            "score": round(float(h.score), 3),
            "valid": valid,
            "tags": list(c.tags or [])[:4],
            "body": c.text[:220],
            "expired": bool(h.expired),
            "channel": h.channel,
        })
    return assets


def _terms(ctx) -> List[Dict[str, Any]]:
    """术语表（四语对照）。"""
    out = []
    for c in ctx.store.list_kb_chunks(ctx.kb_version):
        if c.doc_type != "术语表":
            continue
        parts = [p.strip() for p in (c.title or "").split("/")]
        parts = (parts + ["", "", "", ""])[:4]
        forbidden = (c.extra or {}).get("forbidden") or []
        out.append({
            "zh": parts[1] if len(parts) > 1 else "",
            "en": parts[0] if parts else "",
            "es": parts[2] if len(parts) > 2 else "",
            "ar": parts[3] if len(parts) > 3 else "",
            "forbid": forbidden[0] if forbidden else "",
        })
    return out


def build_workbench(ctx, limit: int = 200) -> Dict[str, Any]:
    """组装工作台读模型。"""
    now = _now()
    threads = list(ctx.store.list_threads())
    threads.sort(key=lambda t: t.last_activity_at, reverse=True)
    threads = threads[:limit]
    payloads = [_thread_payload(ctx, t, now) for t in threads]

    stage_counts: Dict[str, int] = {}
    for t in threads:
        stage_counts[t.stage.value] = stage_counts.get(t.stage.value, 0) + 1
    stages = [{"key": k, "name": STAGE_LABEL.get(k, k), "n": stage_counts.get(k, 0),
               "color": STAGE_COLOR.get(k, "#475569")}
              for k in ("INQUIRY", "SAMPLING", "QUOTING", "ORDERED", "DOCS", "PAYMENT")]

    usage = ctx.store.llm_usage_summary()
    series = _cost_series(usage)

    gate_hits: Dict[str, int] = {}
    for p in payloads:
        for g in p["gates"]:
            if g["hit"]:
                gate_hits[g["id"]] = gate_hits.get(g["id"], 0) + 1
    gate_defs = gate_catalog(ctx.supervisor.low_confidence_threshold,
                             ctx.supervisor.amount_threshold)
    gates = [{"name": "%s %s" % (g.gate_id.value, g.title), "desc": g.desc,
              "must": g.must,
              "state": ("触发 %d 条" % gate_hits.get(g.gate_id.value, 0))
              if gate_hits.get(g.gate_id.value) else "未触发"}
             for g in gate_defs]

    return {
        "source": "live",
        "generated_at": now.isoformat(),
        "kb_version": ctx.kb_version,
        "STAGE": {k: STAGE_LABEL.get(k, k) for k in STAGE_LABEL},
        "INTENT": dict(INTENT_LABEL),
        "threads": payloads,
        "kpis": _kpis(ctx, threads, now),
        "stages": stages,
        "quality": [],
        "costByLang": series["costByLang"],
        "costTrend": series["costTrend"],
        "riskEvents": _risk_events(ctx, threads, now),
        "gates": gates,
        "kbAssets": _kb_assets(ctx),
        "terms": _terms(ctx),
        "notes": [
            "分语种检索质量（quality）不是实时数据，来自评测脚本产物（dev.ps1 eval），"
            "这里不给（给一个“看起来像实时”的质量分比给不给更坏）。",
            "成本只累加**单价已核实**的行（priced_count），未核实单价的行不计入。",
            "kpis.avgFirstResponse 是**收到消息 → 生成草稿**的时延（自动应答时延）；"
            "含人工签核与发送的真实首响在 avgFirstResponseSigned（没发出过就没有数字）。",
        ],
    }
