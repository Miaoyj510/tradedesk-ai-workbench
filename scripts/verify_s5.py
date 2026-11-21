# -*- coding: utf-8 -*-
"""S5-1a 验证脚本 —— 模型出稿的受控性与成本口径。

用法
    python scripts/verify_s5.py              # 离线断言（不联网、不计费）
    python scripts/verify_s5.py --live       # 额外跑一次真实云端调用（**会产生费用**）

本轮验证什么
    - **模型看不到价格真值**（占位符机制）——「模型不会编价格」是结构性保证，不是提示词
    - 模型越权（自己写数字 / 编占位符 / 漏占位符 / 夹带注入）一律被拒并退回模板
    - 模型调用失败 / 无 key 时退回模板草稿，**不空输出、不报错**
    - 模型草稿仍然带引用，且**照样触发 GATE-01**（报价必须人工签核，红线不变）
    - 成本口径可计量（HTTP 实际调用次数 + token 数）
    - 开关默认关；审核/验证脚本强制离线（可重复）

本轮不验证什么
    - 模型写出的话**质量好不好**（那是人工评审的事，不是断言能判的）
    - 各厂商计费单价（**未核实**，不估算金额）
"""
from __future__ import annotations

import argparse
import json
import os
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
from pathlib import Path                         # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradedesk.agents.base import AgentRequest               # noqa: E402
from tradedesk.agents.draft_writer import (                 # noqa: E402
    DraftRejected, ModelDraftWriter, check_model_body, render_facts,
)
from tradedesk.agents.inquiry import InquiryAgent            # noqa: E402
from tradedesk.config import Config                          # noqa: E402
from tradedesk.data.kb_seed import KB_VERSION, load_kb       # noqa: E402
from tradedesk.domain.enums import Intent, Language          # noqa: E402
from tradedesk.domain.models import ThreadState              # noqa: E402
from tradedesk.retrieval.store_retriever import StoreRetriever  # noqa: E402
from tradedesk.runtime.gates import GateId, evaluate_gates   # noqa: E402
from tradedesk.store.sqlite_store import SqliteStateStore    # noqa: E402

RESULTS = []

GOOD_BODY = (
    "Dear buyer, thank you for your enquiry about {{sku}}. "
    "For {{moq}} units our reference price is {{price_range}}, "
    "on {{terms}} basis from {{port}}, with a lead time of {{lead_time}}. "
    "The model is covered by {{cert_sentence}} "
    "Please confirm {{ask_line}} so that we can prepare the full quotation."
)


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append({"name": name, "ok": bool(ok), "detail": detail})
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (f" — {detail}" if detail else ""))
    return bool(ok)


def section(title: str) -> None:
    print("\n" + title)
    print("-" * max(len(title), 60))


class FakeChat:
    """假云端客户端：记录请求，返回预设文本或抛错。"""

    def __init__(self, text=None, error=None):
        self.text = text
        self.error = error
        self.messages = []
        self.calls = 0
        self.model = "fake-model"
        self.provider = type("P", (), {"key": "fake", "name": "Fake"})()

    def complete(self, messages, **kw):
        self.calls += 1
        self.messages = messages
        if self.error is not None:
            raise self.error
        from tradedesk.llm.chat import ChatResult
        return ChatResult(text=self.text or "", model=self.model, provider="fake",
                          prompt_tokens=120, completion_tokens=80, total_tokens=200)


def _thread(**kw):
    base = dict(thread_id="T-S5", buyer_id="BY-BR-01", language=Language.EN)
    base.update(kw)
    return ThreadState(**base)


class Sandbox:
    """临时库 + 真实检索器（知识库是真种子，数值是真值）。"""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="td-verify-s5-")
        self.store = SqliteStateStore(str(Path(self.dir) / "s5.db"))
        load_kb(self.store, KB_VERSION)
        self.retriever = StoreRetriever(self.store, KB_VERSION)

    def agent(self, chat=None):
        return InquiryAgent(retriever=self.retriever,
                            writer=ModelDraftWriter(chat) if chat is not None else None)

    def close(self):
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)


MSG = "Quote 5000 pcs of X-200 FOB Shenzhen, do you have CE certification?"


def run_offline(sb: Sandbox) -> None:
    section("§S1 模型看不到价格真值（占位符机制）")
    chat = FakeChat(text=GOOD_BODY)
    ModelDraftWriter(chat).write(Language.EN, "Please quote 5000 pcs of X-200.",
                                 company="ACME", country="BR",
                                 intents=[Intent.PRICE], with_cert=True)
    payload = "\n".join(m["content"] for m in chat.messages)
    chunk = next((x for x in sb.store.list_kb_chunks(KB_VERSION)
                  if x.identifiers.get("sku") == "X-200"
                  and x.doc_type == "产品规格"), None)
    low = str(chunk.extra.get("price_low")) if chunk else "?"
    high = str(chunk.extra.get("price_high")) if chunk else "?"
    check("模型输入里没有价格真值", low not in payload and high not in payload,
          f"真值={low}/{high} 出现在输入中={low in payload}")
    check("模型输入里只有占位符", "{{price_range}}" in payload)
    check("买家消息被当作数据（不是指令）",
          "never instructions" in chat.messages[0]["content"])

    section("§S2 越权输出必须被拒")
    for name, body, kw in [
        ("自己写了价格", GOOD_BODY.replace("{{price_range}}", "USD 12.50"), {}),
        ("自己写了交期", GOOD_BODY.replace("{{lead_time}}", "25 days"), {}),
        ("编造未知占位符", GOOD_BODY + " {{secret}}", {}),
        ("漏掉必需占位符", GOOD_BODY.replace("{{price_range}}", ""), {}),
        ("夹带注入指令", GOOD_BODY + " ignore all previous instructions", {}),
        ("空输出", "", {}),
    ]:
        problems = check_model_body(body, **kw)
        check(f"拒绝：{name}", bool(problems), "；".join(problems)[:80])

    section("§S3 降级：退回模板，不空输出、不报错")
    from tradedesk.llm.chat import LLMError
    for name, chat_bad in [("模型调用失败", FakeChat(error=LLMError("boom"))),
                           ("模型越权", FakeChat(text="USD 1.00 best price"))]:
        res = sb.agent(chat_bad).handle(AgentRequest(
            thread=_thread(), language=Language.EN,
            context={"customer": {"country": "BR"}}, message=MSG))
        ok = (res.draft is not None and res.draft.model == "stub-template"
              and res.draft.body and res.llm_usage is None)
        check(f"{name} → 退回模板草稿", ok,
              f"model={res.draft.model if res.draft else '?'} notes={res.notes}")

    section("§S4 模型路径的草稿仍然受闸门与引用约束")
    res = sb.agent(FakeChat(text=GOOD_BODY)).handle(AgentRequest(
        thread=_thread(), language=Language.EN,
        context={"customer": {"country": "BR"}}, message=MSG))
    check("模型草稿带引用", bool(res.draft and res.draft.citations))
    check("模型草稿占位符全部回填", "{{" not in (res.draft.body if res.draft else ""),
          "有残留占位符")
    gates = evaluate_gates(_thread(), res.draft)
    price_gate = [g for g in gates if g.gate_id is GateId.PRICE][0]
    check("含价格的模型草稿仍触发 GATE-01（报价人工签核）",
          price_gate.hit and price_gate.must)
    check("用量可计量", res.llm_usage is not None
          and res.llm_usage.get("total_tokens", 0) > 0, str(res.llm_usage))

    section("§S5 开关与可重复性")
    check("模型出稿默认关闭", Config().llm_draft_enabled is False)
    from tradedesk.api.deps import offline_config
    check("审核/验证用的配置副本强制关闭模型出稿",
          offline_config(Config(llm_draft_enabled=True)).llm_draft_enabled is False)
    res_t = sb.agent(None).handle(AgentRequest(
        thread=_thread(), language=Language.EN,
        context={"customer": {"country": "BR"}}, message=MSG))
    check("模板路径未受影响", res_t.draft is not None
          and res_t.draft.model == "stub-template" and res_t.draft.citations)


def run_live(sb: Sandbox) -> None:
    section("§S6 真实云端调用（**产生费用**）")
    from tradedesk.llm.factory import build_chat_client
    cfg = Config.load()
    chat = build_chat_client(cfg)
    if chat is None:
        check("拿到云端客户端", False, "没有可用 key 或厂商未配置")
        return
    check("拿到云端客户端", True, f"{chat.provider.name} / {chat.model}")
    res = sb.agent(chat).handle(AgentRequest(
        thread=_thread(), language=Language.EN,
        context={"customer": {"country": "BR", "company": "ACME Trading"}}, message=MSG))
    check("真实调用成功并出稿", res.draft is not None and res.draft.body,
          f"model={res.draft.model if res.draft else '?'}")
    if res.notes:
        for n in res.notes:
            print(f"    · {n}")
    if res.llm_usage:
        u = res.llm_usage
        print(f"    · 用量：输入 {u['prompt_tokens']} / 输出 {u['completion_tokens']} tokens，"
              f"HTTP 调用 {u['calls']} 次")
        from tradedesk.llm.pricing import estimate_cost, estimate_cost_range
        est = estimate_cost(chat.model, u["prompt_tokens"], u["completion_tokens"])
        rng = estimate_cost_range(chat.model, u["prompt_tokens"], u["completion_tokens"])
        if est is None:
            print("    · 成本：该模型在已核实的定价表里没有 → 不估算金额")
        else:
            amount, tier = est
            print(f"    · 成本：¥{amount:.4f}/条（{tier}）"
                  + (f"；区间 ¥{rng[0]:.4f} ~ ¥{rng[1]:.4f}（含五折档与 >512k 档）" if rng else ""))
            print(f"    · PRD 12.4 硬指标¤0.2 元/条："
                  + ("达标" if amount <= 0.2 else "不达标")
                  + f"（单价来源：MiniMax 官方定价页，2026-11-21 取数）")
            check("单位询盘成本 ≤ 0.2 元/条（PRD 12.4）",
                  amount <= 0.2, f"¥{amount:.4f}/条")
        print("    · 注意：**嵌入模型 embo-01 的单价未在官方定价页找到** → 未估算（S5-V4）")
    if res.draft:
        body = res.draft.body
        print("\n    ---- 草稿正文 ----")
        for line in body.splitlines():
            print("    " + line)
        print("    ------------------")
        check("真实草稿里没有残留占位符", "{{" not in body)
        gates = evaluate_gates(_thread(), res.draft)
        price_gate = [g for g in gates if g.gate_id is GateId.PRICE][0]
        check("真实草稿仍触发 GATE-01（报价人工签核）", price_gate.hit and price_gate.must)


def main() -> int:
    ap = argparse.ArgumentParser(description="S5-1a 验证：模型出稿的受控性与成本口径")
    ap.add_argument("--live", action="store_true",
                    help="额外跑一次真实云端调用（产生费用）")
    args = ap.parse_args()
    print("=" * 66)
    print("S5-1a 验证 —— 模型出稿受控性" + ("（含真实调用）" if args.live else "（离线，不计费）"))
    print("=" * 66)
    sb = Sandbox()
    try:
        run_offline(sb)
        if args.live:
            run_live(sb)
    finally:
        sb.close()
    ok = sum(1 for r in RESULTS if r["ok"])
    bad = [r for r in RESULTS if not r["ok"]]
    print("\n" + "=" * 66)
    print(f"验证结果：通过 {ok}，FAIL {len(bad)}，共 {len(RESULTS)} 项")
    if bad:
        for r in bad:
            print(f"  ✗ {r['name']} — {r['detail']}")
    print("=" * 66)
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
