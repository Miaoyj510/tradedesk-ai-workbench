# -*- coding: utf-8 -*-
"""从 ModelScope 直连下载本项目要用的公开数据集（免登录、免 token）。

为什么用 ModelScope 而不是 HuggingFace
--------------------------------------
本机 **HuggingFace 不可达**（`huggingface.co:443` 连不通，已实测），
而 `www.modelscope.cn:443` 直连可达。ModelScope 上这几个基准是 HF 的官方镜像
（`sentence-transformers/*`、`MTEB/*`、`C-MTEB/*`），内容一致。

**它只解决 PRD 9.5 的「能力体检层」**
========================================
公开检索基准（MIRACL / MLDR / Mr. TyDi / mMARCO / MKQA）能证明**多语种检索管线
没问题**（跨语种召回、混合检索、重排），**证明不了「询盘场景做得好」**。
询盘语料必须自建 —— 见 PRD 9.5 的两层口径，本脚本下载的数据只进第一层。

用法
----
    python scripts/fetch_datasets.py --list          # 看看有什么（不下载）
    python scripts/fetch_datasets.py --dry-run       # 列出将要下的文件与体积
    python scripts/fetch_datasets.py                  # 全量下载
    python scripts/fetch_datasets.py --only mldr --langs ar,es
    python scripts/fetch_datasets.py --verify        # 校验已下载文件的 SHA256

落盘位置：`data/datasets/<数据集>/<相对路径>`（`data/` 已在 .gitignore 里，不入库）
清单文件：`data/datasets/manifest.json`（机器可读）
人读清单：`docs/DATASETS.md`（入库，含来源、许可、PRD 依据）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

try:
    sys.stdout.reconfigure(encoding="utf-8")  # 控制台 GBK 会炸
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "data" / "datasets"
MS = "https://www.modelscope.cn/api/v1/datasets"

# ---------------------------------------------------------------------------
# 数据集登记表：来源方 / 语种 / 层级 / PRD 依据
# ---------------------------------------------------------------------------
DATASETS: Dict[str, Dict[str, Any]] = {
    "miracl": {
        "ns": "sentence-transformers", "name": "miracl",
        "title": "MIRACL 多语种检索基准（ar/es/zh/en 等 16 语）",
        "layer": "体检层",
        "prd": "PRD 9.5 公开基准体检层",
        "license": "见 ModelScope 页（原始为 CC BY-NC 4.0，**商用需另核**）",
        "langs": ["ar", "es", "zh", "en"],
        "note": "每语种一个 train parquet（query/positive/negative 三元组）",
    },
    "mldr": {
        "ns": "sentence-transformers", "name": "mldr",
        "title": "MLDR 多语种长文档检索基准（ar/es/pt/zh/en 等 13 语）",
        "layer": "体检层",
        "prd": "PRD 9.5 公开基准体检层",
        "license": "见 ModelScope 页",
        "langs": ["ar", "es", "pt", "zh", "en"],
        "note": "**唯一同时覆盖 ar/es/pt/zh 的基准**，长文档场景；zh 分片较大（约 228MB）",
    },
    "mr-tydi": {
        "ns": "sentence-transformers", "name": "mr-tydi",
        "title": "Mr. TyDi 多语种检索基准（ar/en/ja/ru/th 等 11 语）",
        "layer": "体检层",
        "prd": "PRD 9.5 公开基准体检层",
        "license": "见 ModelScope 页（原始 Apache-2.0）",
        "langs": ["ar", "en"],
        "note": "含 ar（阿拉伯语）检索段落；无 es/pt/zh —— 语种覆盖是短板，如实记录",
    },
    "mkqa": {
        "ns": "MTEB", "name": "MKQARetrieval",
        "title": "MKQA 多语种知识问答检索基准（26 语）",
        "layer": "体检层",
        "prd": "PRD 9.5 公开基准体检层",
        "license": "见 ModelScope 页（原始 CC BY-SA 4.0）",
        "langs": ["ar", "es", "pt"],
        "note": "每语种 corpus/queries/qrels 三件套；zh 为 zh_cn/zh_hk/zh_tw 三个变体",
    },
    "mmarco": {
        "ns": "C-MTEB", "name": "MMarcoRetrieval",
        "title": "mMARCO 多语种检索语料（整库，不按语种分片）",
        "layer": "体检层",
        "prd": "PRD 9.5 公开基准体检层",
        "license": "见 ModelScope 页",
        "langs": [],
        "note": "**该镜像没有语言字段**，只能整库跑，不能做分语种分层 —— "
                "语种分层用 MLDR / MIRACL / MKQA 兜（PRD 12.3 要求分语种）",
        "extra_files": ["data/corpus-00000-of-00001-b8cdf7f89c113423.parquet",
                        "data/queries-00000-of-00001-4542b172a462173e.parquet"],
    },
    "jd": {
        "ns": "DAMO_NLP", "name": "jd",
        "title": "京东商品评论对话集（JDDC，**纯中文单语**）",
        "layer": "语料种子（询盘语料自建的参考底本，非目标语种）",
        "prd": "PRD 9.5 询盘语料自建方案；PRD 第 9 章「电商对话语料是纯中文单语」这一现实约束",
        "license": "见 ModelScope 页",
        "langs": ["zh"],
        "note": "用来提炼**询盘话术结构与意图槽位**，不是拿来当多语种评测集",
        "extra_files": ["train.csv", "dev.csv", "jd.json"],
    },
    "trade-intent-zh": {
        "ns": "yangzailu", "name": "chinese-foreign-trade-intent-annotation-dataset",
        "title": "中文外贸意图标注数据集",
        "layer": "语料种子",
        "prd": "PRD 9.5「MASSIVE 意图体系重映射为询盘五类：询价/打样/规格/物流/投诉」的种子",
        "license": "见 ModelScope 页",
        "langs": ["zh"],
        "note": "体积很小，用于校准意图标签体系；**ar/es/pt 询盘语料仍必须自建**",
        "extra_files": ["wm.json", "README.md"],
    },
}

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TradeDesk-AI/1.0"}


def _get_json(url: str) -> Any:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def list_files(ns: str, name: str, root: str = "") -> List[Dict[str, Any]]:
    url = "%s/%s/%s/repo/tree?Revision=master&Root=%s&Recursive=true" % (
        MS, ns, name, urllib.parse.quote(root))
    data = _get_json(url).get("Data") or {}
    return data.get("Files") or []


def download(ns: str, name: str, path: str, out: Path) -> Dict[str, Any]:
    """下载单个文件；已存在且大小一致则跳过（断点友好）。"""
    url = "%s/%s/%s/repo?Revision=master&FilePath=%s" % (
        MS, ns, name, urllib.parse.quote(path))
    out.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers=UA)
    tmp = out.with_suffix(out.suffix + ".part")
    h = hashlib.sha256()
    size = 0
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=180) as r, open(tmp, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            size += len(chunk)
    tmp.replace(out)
    return {"path": path, "bytes": size, "sha256": h.hexdigest(),
            "secs": round(time.time() - t0, 1)}


def sha256_of(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve_targets(key: str, only: List[str], langs: List[str]) -> List[Dict[str, Any]]:
    """列出某个数据集要下载的具体文件。"""
    meta = DATASETS[key]
    ns, name = meta["ns"], meta["name"]
    targets: List[Dict[str, Any]] = []
    for lang in (langs or meta["langs"]):
        for suffix in ("triplet", "corpus", "queries", "qrels"):
            root = "%s-%s" % (lang, suffix)
            try:
                fs = list_files(ns, name, root)
            except Exception:
                continue
            for f in fs:
                p = f.get("Path")
                if p and p.endswith(".parquet"):
                    targets.append({"path": p, "bytes": f.get("Size") or 0})
    for p in meta.get("extra_files", []):
        targets.append({"path": p, "bytes": None})
    seen, out = set(), []
    for t in targets:
        if t["path"] in seen:
            continue
        seen.add(t["path"])
        out.append(t)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="下载 PRD 9.5 用的公开数据集")
    ap.add_argument("--list", action="store_true", help="列出已登记的数据集")
    ap.add_argument("--only", default="", help="只处理逗号分隔的 key")
    ap.add_argument("--langs", default="", help="只下这些语种，逗号分隔")
    ap.add_argument("--dry-run", action="store_true", help="只列计划，不下载")
    ap.add_argument("--verify", action="store_true", help="校验已下载文件的 SHA256")
    args = ap.parse_args()

    if args.list:
        for k, v in DATASETS.items():
            print("[%s] %s" % (k, v["title"]))
            print("     层级=%s | 语种=%s" % (v["layer"], ",".join(v["langs"]) or "整库"))
            print("     来源=%s/%s | %s" % (v["ns"], v["name"], v["prd"]))
            print("     备注：%s" % v["note"])
        return 0

    keys = [k.strip() for k in args.only.split(",") if k.strip()] or list(DATASETS)
    langs = [l.strip() for l in args.langs.split(",") if l.strip()] or []
    if args.verify:
        mf = DEST / "manifest.json"
        if not mf.exists():
            print("还没有 manifest.json，先下载一次")
            return 1
        data = json.loads(mf.read_text(encoding="utf-8"))
        bad = 0
        for rec in data["files"]:
            p = DEST / rec["dataset"] / rec["path"]
            if not p.exists():
                print("  缺文件 %s/%s" % (rec["dataset"], rec["path"]))
                bad += 1
                continue
            if sha256_of(p) != rec["sha256"]:
                print("  校验不符 %s/%s" % (rec["dataset"], rec["path"]))
                bad += 1
        print("校验完成：%d 个文件，%d 个有问题" % (len(data["files"]), bad))
        return 1 if bad else 0

    # 清单**合并**而不是覆盖：增量下载（--only）不能把上一轮的条目抹掉
    mf_path = DEST / "manifest.json"
    manifest: Dict[str, Any] = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                 "source": "ModelScope 直连（免登录）", "files": []}
    if mf_path.exists():
        try:
            old = json.loads(mf_path.read_text(encoding="utf-8"))
            manifest["files"] = old.get("files", [])
        except Exception:
            pass
    seen_paths = {(f["dataset"], f["path"]) for f in manifest["files"]}
    for key in keys:
        meta = DATASETS[key]
        targets = resolve_targets(key, [key], langs)
        if not targets:
            print("[%s] 没有匹配到文件，跳过" % key)
            continue
        total = sum(t["bytes"] or 0 for t in targets)
        print("[%s] %s —— %d 个文件, 约 %.1f MB" % (
            key, meta["title"], len(targets), total / 1048576.0))
        if args.dry_run:
            for t in targets:
                print("    %-64s %s" % (t["path"], t["bytes"]))
            continue
        for t in targets:
            out = DEST / key / t["path"]
            if out.exists() and (t["bytes"] is None or out.stat().st_size == t["bytes"]):
                print("    已存在  %s" % t["path"])
                rec = {"dataset": key, "path": t["path"],
                       "bytes": out.stat().st_size, "sha256": sha256_of(out)}
                if (key, t["path"]) not in seen_paths:
                    manifest["files"].append(rec)
                    seen_paths.add((key, t["path"]))
                continue
            info = download(meta["ns"], meta["name"], t["path"], out)
            print("    完成 %-56s %8.1f MB  %ss" % (
                t["path"], info["bytes"] / 1048576.0, info["secs"]))
            if (key, t["path"]) not in seen_paths:
                manifest["files"].append({"dataset": key, "path": t["path"],
                                           "bytes": info["bytes"], "sha256": info["sha256"]})
                seen_paths.add((key, t["path"]))
    if not args.dry_run:
        DEST.mkdir(parents=True, exist_ok=True)
        (DEST / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print("\n清单写入 %s" % (DEST / "manifest.json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
