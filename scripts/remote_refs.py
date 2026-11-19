# -*- coding: utf-8 -*-
"""读远端分支的 ref（用 GitHub API，不依赖 git-over-HTTPS 通道）。

用法：python scripts/remote_refs.py --owner OWNER --repo REPO
输出：每行 "<sha>\trefs/heads/<branch>"
Token 从环境变量 TRADEDESK_GITHUB_TOKEN 读取。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner", required=True)
    ap.add_argument("--repo", required=True)
    args = ap.parse_args()
    token = os.environ.get("TRADEDESK_GITHUB_TOKEN", "")
    if not token:
        print("缺少 TRADEDESK_GITHUB_TOKEN", file=sys.stderr)
        return 2
    req = urllib.request.Request(
        f"https://api.github.com/repos/{args.owner}/{args.repo}/git/matching-refs/heads/",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "TradeDesk-AI-Refs"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"读取远端分支失败：{e}", file=sys.stderr)
        return 1
    for item in data:
        print(f"{item['object']['sha']}\t{item['ref']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())