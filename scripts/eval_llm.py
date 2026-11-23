# -*- coding: utf-8 -*-
"""S5-1c 评测脚本 —— 模型层（按语种分层）。

用法
    python scripts/eval_llm.py                          # 离线层：评测器自检（不联网、不计费）
    python scripts/eval_llm.py --live                   # 真实层：真模型跑 N 条
    python scripts/eval_llm.py --live --per-lang 20     # 每语种 20 条
    python scripts/eval_llm.py --live --langs en,es,ar  # 指定语种

评什么（与 S4-4c 的检索层评测是两个东西）
-------------------------------------
S4-4c 评的是**检索层**（命中率 / 召回）；本片评的是**模型层**（出稿 / 越权 / 成本）。
两层的指标**不混在一起算**（PRD 12.3 的分层规则）。

核心指标：事实保真度（硬指标，必须 100%）
------------------------------------------
**正文里出现的每一个数字，必须落在白名单里**：
  ① 知识库真值（价格 / 交期 / MOQ）；② 买家原话里的数字（模型复述买家的数量不算编造）。
白名单外的数字 → 立即判为**编造**。

为什么这条指标不是“模型谨话就不会乱写”：数值是代码回填的，模型**根本看不到真值**，
所以这条指标是“设计是否真的成立”的**证明**，而不是考题。
正因为如此，评测必须能拿出一个**恶意写数字**的假模型来证明评测器自己有效。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys


def _setup_stdout() -> None:
    """自适应输出编码：tty 用控制台原生编码，非 tty 用 UTF-8，无法编码的字符用 replace 兜底。"""
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
import shutil                                    # noqa: E402
import tempfile                                  # noqa: E402
from datetime import date                        # noqa: E402
from pathlib import Path                         # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradedesk.agents.base import AgentRequest               # noqa: E402
from tradedesk.agents.draft_writer import ModelDraftWriter  # noqa: E402
from tradedesk.agents.inquiry import InquiryAgent            # noqa: E402
from tradedesk.data.inquiry_corpus import CORPUS_VERSION, build_corpus   # noqa: E402
from tradedesk.data.kb_seed import KB_VERSION, load_kb       # noqa: E402
from tradedesk.domain.enums import Language                  # noqa: E402
from tradedesk.domain.models import ThreadState              # noqa: E402
from tradedesk.retrieval.store_retriever import StoreRetriever  # noqa: E402
from tradedesk.store.sqlite_store import SqliteStateStore    # noqa: E402

# —— 指标门槛（**自建目标值**，不是公开对标值）——
GATE_FACT_FIDELITY = 1.0      # 事实保真度：硬指标，必须 100%
GATE_DRAFT_OK = 0.90          # 出稿成功率
GATE_REJECT_MAX = 0.05        # 越权率上限
GATE_COST_MAX = 0.2           # 单位成本（PRD 12.4 硬指标）
GATED_LANGS = ["en", "es", "ar"]   # 设指标门槛的语种（zh 作内部对照，PRD 12.3）

CERT_RE = re.compile(r"[A-Z]{2,}(?:-[A-Z0-9]+)+")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
SKU_RE = re.compile(r"\b([XM]-\d{2,4})\b")
NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def numbers_in(text: str) -> set:
    """文本里的数字集合（证书编号与日期先挑掉）。

    证书编号（CERT-CE-2041）与有效期（2027-03-31）是**标识符**而不是**数值**，
    它们里的数字不能拿来做“数值是否编造”的判定，所以先挑掉。
    """
    if not text:
        return set()
    s = CERT_RE.sub(" ", text)
    s = DATE_RE.sub(" ", s)
    out = set()
    for m in NUM_RE.findall(s):
        try:
            out.add(float(m))
        except ValueError:
            pass
    return out


def kb_truth_numbers(store, kb_version: str, body: str, citations=None) -> set:
    """知识库真值数字集（不信任被评溪对象自己给出的结果）。

    **白名单定义：正文里能出现的数字，必须来自**
      ① 被该草稿**引用**的知识块（标题 / 结构化字段 / 标识 / 有效期），或
      ② 买家原话里的数字。
    白名单外的数字 → 判为**编造**。

    为什么要把证书的**类型**也算进去：正文里的 "ISO 9001" 里的 9001 是证书标识符的一部分，
    它不是价格、不是交期，但它完全合法地出现在对外内容里 —— 不能被当成编造。
    """
    truth = set()
    cited = {str(c) for c in (citations or [])}
    skus = set(SKU_RE.findall(body or ""))
    for ch in store.list_kb_chunks(kb_version):
        ids = {str(v) for v in (ch.identifiers or {}).values()}
        hit = bool(cited and (ids & cited))
        if not hit and skus:
            if ch.doc_type == "产品规格" and ch.identifiers.get("sku") in skus:
                hit = True
            elif ch.doc_type == "证书":
                covers = [str(x).upper() for x in (ch.extra.get("covers") or [])]
                if not covers or any(c in {x.upper() for x in skus} for c in covers):
                    hit = True
        if not hit:
            continue
        truth |= numbers_in(ch.title)
        for v in (ch.extra or {}).values():
            truth |= numbers_in(str(v))
        for v in ids:
            truth |= numbers_in(v)
        truth |= numbers_in(str(ch.valid_until.date() if ch.valid_until else ""))
    return truth


FRAGMENTS = [
    ("Dear buyer, thank you for your enquiry about {{sku}}. ", "{{sku}}"),
    ("For {{moq}} units ", "{{moq}}"),
    ("our reference price is {{price_range}}, ", "{{price_range}}"),
    ("on {{terms}} basis ", "{{terms}}"),
    ("from {{port}}, ", "{{port}}"),
    ("with a lead time of {{lead_time}}. ", "{{lead_time}}"),
    ("{{cert_sentence}} ", "{{cert_sentence}}"),
    ("Please confirm {{ask_line}} so that we can prepare the full quotation.", "{{ask_line}}"),
]

PLACEHOLDER_IN_MSG = re.compile(r"\{\{[A-Za-z_][A-Za-z0-9_]*\}\}")


def _allowed_from_messages(messages) -> set:
    """从提示词里把本次**允许用的占位符**提取出来。

    假模型必须只输出允许的占位符：否则它会因为“编造未知占位符”被拒，
    而不是因为“写了数字”被拒 —— 那就测错了拒绝的原因。
    """
    payload = "\n".join(m.get("content", "") for m in messages)
    return set(PLACEHOLDER_IN_MSG.findall(payload))


class GoodFakeChat:
    """假模型：始终输出合规占位符（不写任何数字）。"""

    name = "good-fake"

    def __init__(self):
        self.calls = 0
        self.model = "MiniMax-M3"
        self.provider = type("P", (), {"key": "fake", "name": "GoodFake"})()

    def _body(self, messages) -> str:
        allowed = _allowed_from_messages(messages)
        parts = []
        for text, ph in FRAGMENTS:
            if ph in allowed:
                parts.append(text)
        return "".join(parts)

    def complete(self, messages, **kw):
        self.calls += 1
        from tradedesk.llm.chat import ChatResult
        return ChatResult(text=self._body(messages), model=self.model, provider="fake",
                          prompt_tokens=600, completion_tokens=200, total_tokens=800)


class BadFakeChat(GoodFakeChat):
    """假模型：**恶意写价格**——用来证明评测器自己有效。

    它把价格占位符保留着（因此不会因“漏占位符”被拒），
    只多写一个多余的数字 —— 这样被拒的原因就只有一个：编数。
    """

    name = "bad-fake"

    def _body(self, messages) -> str:
        body = super()._body(messages)
        return body.replace("our reference price is {{price_range}}, ",
                            "our reference price is {{price_range}} (special price USD 999.99 per unit), ")


def kb_cert_ok(store, kb_version: str, body: str, sku: str) -> bool:
    """正文里的证书号必须是该型号**有效**证书（未过期、覆盖本型号）。"""
    import re as _re
    mentioned = set(_re.findall(r"CERT-[A-Z0-9-]+", body or ""))
    if not mentioned:
        return True                      # 没写证书号 → 不能判定为错（不伪造失败）
    today = date.today()
    for ch in store.list_kb_chunks(kb_version):
        if ch.doc_type != "证书":
            continue
        no = str(ch.identifiers.get("cert_no", ""))
        if no not in mentioned:
            continue
        covers = [str(x).upper() for x in (ch.extra.get("covers") or [])]
        if covers and sku.upper() not in covers:
            return False
        if ch.valid_until is not None and ch.valid_until.date() < today:
            return False
        mentioned.discard(no)
    return not mentioned


def run_layer(cases, store, kb_version: str, chat, label: str) -> dict:
    """跑一层评测，返回按语种分组的指标。"""
    retriever = StoreRetriever(store, kb_version)
    agent = InquiryAgent(retriever=retriever, writer=ModelDraftWriter(chat))
    stats = {}
    for case in cases:
        lang = Language(case.lang)
        thread = ThreadState(thread_id="EVAL-" + case.case_id, buyer_id="EVAL-BUYER",
                             language=lang)
        res = agent.handle(AgentRequest(
            thread=thread, message=case.text, language=lang,
            context={"customer": {"country": case.expect_country, "company": "Eval Co"}}))
        b = stats.setdefault(case.lang, {
            "cases": 0, "draft_ok": 0, "degraded": 0, "rejected": 0,
            "fabricated": 0, "fidelity_fail": 0, "cert_fail": 0,
            "placeholder_left": 0, "total_tokens": 0, "cost_cny": 0.0,
            "priced": 0, "http_calls": 0, "no_draft": 0,
        })
        b["cases"] += 1
        if res.draft is None:
            b["no_draft"] += 1
            continue
        body = res.draft.body
        usage = res.llm_usage or {}
        b["http_calls"] += int(usage.get("calls") or 0)
        b["total_tokens"] += int(usage.get("total_tokens") or 0)
        if usage.get("cost_cny") is not None:
            b["cost_cny"] += float(usage["cost_cny"])
            b["priced"] += 1
        degraded = bool(usage.get("degraded"))
        if degraded:
            b["degraded"] += 1
        else:
            b["draft_ok"] += 1
        if "{{" in body:
            b["placeholder_left"] += 1
        # 事实保真度：正文数字 ∈ 知识库真值 ∪ 买家原话数字
        body_nums = numbers_in(body)
        cited = [c.citation_id for c in (res.draft.citations or [])]
        truth = kb_truth_numbers(store, kb_version, body, cited) | numbers_in(case.text)
        extra = body_nums - truth
        if extra:
            b["fabricated"] += 1
            b["fidelity_fail"] += 1
        skus = set(SKU_RE.findall(body))
        sku = sorted(skus)[0] if skus else ""
        if sku and not kb_cert_ok(store, kb_version, body, sku):
            b["cert_fail"] += 1
    for lang, b in stats.items():
        n = max(1, b["cases"])
        b["draft_ok_rate"] = b["draft_ok"] / n
        b["degraded_rate"] = b["degraded"] / n
        b["fidelity"] = 1.0 - (b["fidelity_fail"] / n)
        b["avg_tokens"] = b["total_tokens"] / n
        b["avg_cost_cny"] = (b["cost_cny"] / b["priced"]) if b["priced"] else None
    return {"label": label, "by_lang": stats}


def report(result: dict, gated_langs=GATED_LANGS) -> tuple:
    """打印报表并返回是否达标。"""
    rows = result["by_lang"]
    print("\n" + "=" * 78)
    print(f"模型层评测 —— {result['label']}")
    print("=" * 78)
    header = ("语种 | 样本 | 出稿成功 | 越权率 | 事实保真 | "
              "占位符残留 | 证书错 | 平均token | 平均成本 | 判定")
    print(header)
    print("-" * 78)
    ok = True
    for lang in sorted(rows):
        b = rows[lang]
        gated = lang in gated_langs
        c = b["avg_cost_cny"]
        problems = []
        if b["fidelity"] < GATE_FACT_FIDELITY:
            problems.append("事实保真度不达标")
        if b["placeholder_left"]:
            problems.append("有占位符残留")
        if b["cert_fail"]:
            problems.append("证书声明错误")
        if gated:
            if b["draft_ok_rate"] < GATE_DRAFT_OK:
                problems.append("出稿成功率低于门槛")
            if b["degraded_rate"] > GATE_REJECT_MAX:
                problems.append("越权率高于门槛")
            if c is not None and c > GATE_COST_MAX:
                problems.append("单位成本高于 0.2 元/条")
        verdict = "✓" if not problems else "✗ " + "；".join(problems)
        if problems:
            ok = False
        cost_txt = ("¥%.4f" % c) if c is not None else "未计价"
        print("%-5s | %4d | %9.2f%% | %6.2f%% | %8.2f%% | %8d | %6d | %8.0f | %9s | %s" % (
            lang, b["cases"], b["draft_ok_rate"] * 100, b["degraded_rate"] * 100,
            b["fidelity"] * 100, b["placeholder_left"], b["cert_fail"],
            b["avg_tokens"], cost_txt, verdict))
    print("-" * 78)
    print("门槛（自建目标值，非公开对标值）："
          "事实保真度 = 100%（硬指标）；"
          "出稿成功率 ≥ 90%；"
          "越权率 ≤ 5%；"
          "单位成本 ≤ ¥0.2/条（PRD 12.4）。")
    print("设门槛的语种：" + ", ".join(gated_langs) + "（zh 作内部对照，PRD 12.3）。")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="S5-1c 评测：模型层分语种指标")
    ap.add_argument("--live", action="store_true", help="真实云端调用（产生费用）")
    ap.add_argument("--per-lang", type=int, default=10, help="每个语种跑多少条（默认 10）")
    ap.add_argument("--langs", default="", help="只跑这几个语种（逗号分隔）")
    args = ap.parse_args()

    print("=" * 78)
    print("S5-1c 模型层评测" + ("（真实云端，会产生费用）" if args.live
                                            else "（离线，不联网、不计费）"))
    print("=" * 78)

    tmp = tempfile.mkdtemp(prefix="td-eval-llm-")
    store = SqliteStateStore(str(Path(tmp) / "eval.db"))
    load_kb(store, KB_VERSION)
    try:
        cases = build_corpus()
        if args.langs:
            want = {x.strip() for x in args.langs.split(",") if x.strip()}
            cases = [c for c in cases if c.lang in want]
        else:
            cases = [c for c in cases if c.lang != "zh"]      # zh 作内部对照，默认不跑
        picked = []
        per = {}
        for c in cases:
            if per.get(c.lang, 0) >= args.per_lang:
                continue
            per[c.lang] = per.get(c.lang, 0) + 1
            picked.append(c)
        print(f"语料：自建语料 {len(picked)} 条（每语种最多 {args.per_lang} 条）"
              f"，语料版本 {CORPUS_VERSION}")

        ok = True
        if not args.live:
            # 离线层：先证明**评测器自己有效**（能拿出恶意写数字的假模型）
            print("\n--- 离线层 A：合规假模型（应全部通过） ---")
            r_ok = report(run_layer(picked, store, KB_VERSION, GoodFakeChat(), "离线·合规假模型"))
            ok = ok and r_ok
            print("\n--- 离线层 B：恶意写价的假模型（应全部被拒） ---")
            r_bad = run_layer(picked, store, KB_VERSION, BadFakeChat(), "离线·恶意假模型")
            for lang, b in r_bad["by_lang"].items():
                good = (b["degraded_rate"] == 1.0 and b["fidelity"] == 1.0
                        and b["placeholder_left"] == 0)
                print("  [%s] %s：降级率 %.0f%%（应 100%%）、事实保真度 %.0f%%"
                      % ("PASS" if good else "FAIL", lang, b["degraded_rate"] * 100,
                         b["fidelity"] * 100))
                ok = ok and good
            print("离线层结论：评测器能拿出恶意编造，"
                  "且恶意输出**不会流到对外内容**（降级回模板后保真度仍为 100%）。")
        else:
            from tradedesk.config import Config
            from tradedesk.llm.factory import build_chat_client
            chat = build_chat_client(Config.load())
            if chat is None:
                print("没有可用的云端客户端（缺 key 或厂商未配置）")
                return 1
            print(f"云端客户端：{chat.provider.name} / {chat.model}")
            n = len(picked)
            print(f"将发起 {n} 次真实调用（预计金额见报表）……")
            ok = report(run_layer(picked, store, KB_VERSION, chat, "真实·" + chat.model)) and ok
    finally:
        store.close()
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n" + "=" * 78)
    print("结论：" + ("全部达标" if ok else "有未达标项"))
    print("=" * 78)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
