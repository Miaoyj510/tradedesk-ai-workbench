# -*- coding: utf-8 -*-
"""评测框架（S4-4c）—— 分语种、分通道的检索质量评估。

用法
    python scripts/eval_retrieval.py                  # SQLite
    python scripts/eval_retrieval.py --dsn <DSN>     # PostgreSQL
    python scripts/eval_retrieval.py --json out.json # 机器可读

评什么（PRD 12.3 的分层规则）
----------------------
【自建语料层】（本脚本的主体）
  - 检索 Top-5 命中（SKU 级）、同语 Top-1、跨语命中 Top-1
  - 意图 Macro-F1（五类意图）
  - 分通道对比：词法 / 结构化 / 稠密 / 融合
  【公开基准体检层】
  独立成表，**不与自建集合并计算总分**（PRD 12.3 分层规则）。
  本机网络不可达 huggingface.co，基准数据集拿不到 → 记为待验证项（见 docs/S4-ACCEPTANCE.md）。

口径提醒：本语料是**模板合成**，标注准确但句式重复度高—— 数字不能当成真实买家问法的分布。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path


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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradedesk.agents.inquiry import classify_intent          # noqa: E402
from tradedesk.data.inquiry_corpus import (                 # noqa: E402
    CORPUS_VERSION, INTENTS, build_corpus, corpus_summary,
)
from tradedesk.data.kb_seed import KB_VERSION, load_kb       # noqa: E402
from tradedesk.domain.enums import Intent                    # noqa: E402
from tradedesk.retrieval.base import SearchQuery             # noqa: E402
from tradedesk.retrieval.indexer import load_index          # noqa: E402
from tradedesk.retrieval.pipeline import RetrievalPipeline   # noqa: E402
from tradedesk.retrieval.store_retriever import StoreRetriever  # noqa: E402
from tradedesk.store.postgres_store import PostgresStateStore   # noqa: E402
from tradedesk.store.sqlite_store import SqliteStateStore      # noqa: E402

INTENT_MAP = {
    Intent.PRICE: "price",
    Intent.SAMPLE: "sample",
    Intent.SPEC: "spec",
    Intent.LOGISTICS: "logistics",
    Intent.COMPLAINT: "complaint",
}
CHANNELS = ["lexical", "structured", "dense", "fused"]
BUYER_LANGS = ["en", "es", "ar"]      # 买家语种（设指标门槛）
ALL_LANGS = BUYER_LANGS + ["zh"]      # zh 仅作内部对照


def predict_intent(text: str) -> str:
    """用关键词分类器预测意图（单标签取首个命中）。"""
    hits = classify_intent(text)
    for i in hits:
        if i in INTENT_MAP:
            return INTENT_MAP[i]
    return "unknown"


def macro_f1(pairs):
    """宏平均 F1（**PRD 12.3 口径：五类意图的宏平均**）。

    R17-05：旧写法把没有样本的 "unknown" 也算进分母 → 每个语种被系统性压低 1/6，
    报出来的数字与 PRD 门槛不可比。现在只对**五类意图**求宏平均；
    预测出的 "unknown" 计为该类的混、误报（fn），不会无为地拉低分母。
    """
    labels = list(INTENTS)
    total = 0.0
    per = {}
    for lab in labels:
        tp = sum(1 for g, p in pairs if g == lab and p == lab)
        fp = sum(1 for g, p in pairs if g != lab and p == lab)
        fn = sum(1 for g, p in pairs if g == lab and p != lab)
        prec = tp / (tp + fp) if (tp + fp) else 0.0
        rec = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
        per[lab] = round(f1, 4)
        total += f1
    return round(total / len(labels), 4), per


def evaluate(cases, pipeline, retriever, use_dense: bool):
    """返回 {lang: {channel: {...}}} 的指标。"""
    stats = {lang: {ch: {"hit1": 0, "hit3": 0, "hit5": 0, "mrr": 0.0, "n": 0,
                         "same_lang_top1": 0, "cross_lang_top1": 0, "n_same": 0, "n_cross": 0,
                         "prod5": 0, "prod5_same": 0, "n_prod": 0}
                     for ch in CHANNELS}
             for lang in ALL_LANGS}
    intent_pairs = {lang: [] for lang in ALL_LANGS}
    for c in cases:
        q = SearchQuery(text=c.text, lang=c.lang, buyer_country=c.expect_country,
                        today=date.today(), top_k=5)
        dense = retriever.dense_hits(q) if use_dense else []
        by_ch = pipeline.search_by_channel(q, dense_hits=dense)
        by_ch["fused"] = pipeline.search(q, dense_hits=dense)
        intent_pairs[c.lang].append((c.intent, predict_intent(c.text)))
        for ch in CHANNELS:
            hits = by_ch.get(ch, [])
            st = stats[c.lang][ch]
            if c.expect_sku:
                st["n"] += 1
                ranks = [i + 1 for i, h in enumerate(hits)
                         if h.chunk.identifiers.get("sku") == c.expect_sku]
                if ranks:
                    st["hit1"] += 1 if ranks[0] <= 1 else 0
                    st["hit3"] += 1 if ranks[0] <= 3 else 0
                    st["hit5"] += 1 if ranks[0] <= 5 else 0
                    st["mrr"] += 1.0 / ranks[0]
            if hits:
                # 产品块命中（不要求具体型号：买家可能未说型号）
                prod_ranks = [i + 1 for i, h in enumerate(hits) if h.chunk.doc_type == "产品条目"]
                st["prod5"] += 1 if prod_ranks and prod_ranks[0] <= 5 else 0
                st["n_prod"] += 1
                if prod_ranks and prod_ranks[0] <= 5:
                    same_prod = [h.chunk.lang for h in hits[:prod_ranks[0]] if h.chunk.doc_type == "产品条目"]
                    if same_prod and same_prod[0] == c.lang:
                        st["prod5_same"] += 1
                top_lang = hits[0].chunk.lang
                if c.lang != "zh":
                    st["n_same"] += 1
                    st["same_lang_top1"] += 1 if top_lang == c.lang else 0
                else:
                    st["n_cross"] += 1
                    st["cross_lang_top1"] += 1 if top_lang != "zh" else 0
    return stats, intent_pairs


def main() -> int:
    ap = argparse.ArgumentParser(description="分语种分通道检索评测")
    ap.add_argument("--dsn", default="")
    ap.add_argument("--json", default="")
    ap.add_argument("--no-dense", action="store_true",
                    help="关掉稠密通道（不调云端，不产生计费）")
    ap.add_argument("--price-per-call", type=float, default=0.0,
                    help="云端嵌入单价（元/次），用于估算成本；0 = 不估算（单价待核实）")
    args = ap.parse_args()

    print("TradeDesk-AI · S4-4c 检索评测（自建语料层）")
    print(f"语料版本：{CORPUS_VERSION}｜库版本：{KB_VERSION}")

    if args.dsn:
        store = PostgresStateStore(args.dsn)
    else:
        p = ROOT / "data" / "eval_s4.db"
        for suffix in ("", "-wal", "-shm"):
            f = p.with_name(p.name + suffix)
            if f.exists():
                f.unlink()
        store = SqliteStateStore(str(p))
    try:
        load_kb(store, KB_VERSION)
        cases = build_corpus()
        summary = corpus_summary(cases)
        print(f"语料条数：{len(cases)}")
        for lang in ALL_LANGS:
            row = summary.get(lang, {})
            print(f"  {lang}: {sum(row.values())} 条  " + " ".join(f"{k}={v}" for k, v in sorted(row.items())))

        # 稠密通道：需要嵌入器 + 向量索引
        embedder = None
        try:
            from tradedesk.config import get_settings
            from tradedesk.llm.factory import build_embedder
            cfg = get_settings()
            # 走工厂：评测脚本也应该能切换厂商，不能只能测 MiniMax。
            embedder = build_embedder(cfg)
            if embedder is None:
                print("[提示] 嵌入器未生效（没配厂商的 key，或选了不提供嵌入接口的厂商）")
        except Exception as exc:
            print(f"[提示] 嵌入器初始化失败：{exc}")
        idx = load_index(store, KB_VERSION)
        use_dense = (not args.no_dense) and embedder is not None and len(idx) > 0
        if args.no_dense:
            print("\n稠密通道：已被 --no-dense 关掉（不调云端，不产生计费）")
        elif not embedder:
            print("\n稠密通道：**未生效**（没有读到 API key）")
        elif not len(idx):
            print("\n稠密通道：**未生效**（向量索引为空 —— 先跑 dev.ps1 index）")
        else:
            print(f"\n稠密通道：**启用**（向量索引 {len(idx)} 条）")

        pipeline = RetrievalPipeline(store.list_kb_chunks(KB_VERSION), KB_VERSION)
        retriever = StoreRetriever(store, KB_VERSION, embedder=embedder, vector_index=idx)
        stats, intent_pairs = evaluate(cases, pipeline, retriever, use_dense)
        # 通道可观测（R17-06）：必须能区分「稠密通道**失败**」与「稠密通道**召回差**。
        # 不分开的话，稠密指标全是 0.000，而报表头部却写「启用」 ——
        # 读者会把「通道根本没跑」误读成「模型不行」（R15-04 同型）。
        if use_dense:
            calls = getattr(retriever.embedder, "calls", retriever.dense_calls)
            cost = (f"，估算成本 ≈ ¥{calls * args.price_per_call:.4f}"
                    f"（单价 {args.price_per_call} 元/次）") if args.price_per_call else \
                   "（单价未核实，不估算金额；真实单价见待验证项）"
            print(f"\n稠密通道计量：云端 HTTP 调用 {calls} 次{cost}")
            if retriever.dense_skipped:
                print(f"稠密通道未生效的原因：{retriever.dense_skipped}")
        else:
            print("\n稠密通道说明：**未生效** → 下面稠密通道的指标列不具备可比性，不得当作模型能力读。")

        print("\n" + "=" * 96)
        print("检索指标（SKU 级）：R@1 / R@3 / R@5 / MRR，仅统计有期望型号的样本")
        print(f"{'语种':<6}{'通道':<12}{'n':>5}{'R@1':>9}{'R@3':>9}{'R@5':>9}{'MRR':>9}"
              f"{'同语Top1':>12}{'跨语Top1':>12}{'产品块R@5':>13}{'同语产品R@5':>15}")
        for lang in ALL_LANGS:
            for ch in CHANNELS:
                st = stats[lang][ch]
                n = st["n"] or 1
                same = f"{st['same_lang_top1'] / st['n_same']:.3f}" if st["n_same"] else "—"
                cross = f"{st['cross_lang_top1'] / st['n_cross']:.3f}" if st["n_cross"] else "—"
                np_ = st["n_prod"] or 1
                prod = f"{st['prod5'] / np_:.3f}"
                prod_same = f"{st['prod5_same'] / np_:.3f}"
                print(f"{lang:<8}{ch:<12}{st['n']:>5}{st['hit1'] / n:>9.3f}{st['hit3'] / n:>9.3f}"
                      f"{st['hit5'] / n:>9.3f}{st['mrr'] / n:>9.3f}{same:>12}{cross:>12}{prod:>13}{prod_same:>15}")
        print("\n意图分类（Macro-F1）：")
        for lang in ALL_LANGS:
            f1, per = macro_f1(intent_pairs[lang])
            print(f"  {lang}: {f1:.4f}   " + " ".join(f"{k}={v}" for k, v in per.items()))
        print("\n口径：")
        print(f"  - 语料为模板合成，量级远低于 PRD 9.8 目标（en/es/ar 各 ≥300 意图 + 200 检索 + 100 会话），"
              "属**缩减版**，数字不得当作真实买家问法分布。")
        print(f"  - ar 语料尚未经母语抽检（PRD 9.7 C2）。")
        print("  - 公开基准体检层（MIRACL/MLDR/Mr.TyDi/mMARCO/MKQA）本机下不可达，记为待验证项。")
        if args.json:
            Path(args.json).write_text(json.dumps(
                {"corpus_version": CORPUS_VERSION, "kb_version": KB_VERSION,
                 "dense_enabled": use_dense, "stats": stats,
                 "intent_f1": {l: macro_f1(intent_pairs[l])[0] for l in ALL_LANGS}},
                ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\n机器可读结果已写入：{args.json}")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
