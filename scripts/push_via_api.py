# -*- coding: utf-8 -*-
"""用 GitHub Git Database API 把本地提交推送到远端（绕过被拦截的 git-over-HTTPS）。

为什么需要它
------------
本机到 github.com:443 的 git 流量会被重置/超时（Recv failure: Connection was reset），
但 api.github.com 稳定可用。于是改为直接调用 Git 的底层 API 复刻提交历史：

    blob  →  POST /repos/{o}/{r}/git/blobs
    tree  →  POST /repos/{o}/{r}/git/trees
    commit→  POST /repos/{o}/{r}/git/commits
    ref   →  PATCH /repos/{o}/{r}/git/refs/heads/{branch}

内容一致时，GitHub 算出的 commit SHA 与本地**完全相同**，因此远端与本地历史天然对齐，
后续仍可用 git fetch/merge。

用法
    python scripts/push_via_api.py --owner Miaoyj510 --repo tradedesk-ai-workbench \
        --branches main agent-a agent-b
    （Token 从环境变量 TRADEDESK_GITHUB_TOKEN 读，或交互式输入，不回显、不落盘）
"""
from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GIT = r"C:\Users\DELL\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\git\cmd\git.exe"
API = "https://api.github.com"


def _setup_stdout() -> None:
    """自适应输出编码（与 scripts/verify_s1.py 同一套做法）。

    本脚本会打印 ✓ / ✗，这两个字符**不在 GBK 码表里** —— 中文 Windows 的
    默认控制台（CP936）编码它们时会抛 UnicodeEncodeError，把真正要报的错
    （比如 401 Bad credentials）盖掉。所以：
    - 交互式控制台（tty）：沿用控制台原生编码，只放宽字符错误处理；
    - 被重定向 / 被工具捕获（非 tty）：改用 UTF-8。
    外部已设 PYTHONIOENCODING 时以环境变量为准。
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


def _marks() -> tuple:
    """挑一组当前控制台能编码的标记字符。

    ✓ / ✗ 不在 GBK 码表里；中文 Windows 的默认控制台（CP936）编码它们会抛
    UnicodeEncodeError，把真正要报的错盖掉。控制台编不了就退回 ASCII。
    """
    enc = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        "✓✗".encode(enc)
    except Exception:
        return ("[OK]", "[FAIL]")
    return ("✓", "✗")


OK_MARK, FAIL_MARK = _marks()


# ---------------------------------------------------------------- git 辅助
def git(*args: str, binary: bool = False):
    p = subprocess.run([GIT, *args], cwd=str(ROOT), capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{p.stderr.decode('utf-8', 'replace').strip()}")
    return p.stdout if binary else p.stdout.decode("utf-8", "replace")


def git_bytes(*args: str) -> bytes:
    p = subprocess.run([GIT, *args], cwd=str(ROOT), capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{p.stderr.decode('utf-8', 'replace').strip()}")
    return p.stdout


# ---------------------------------------------------------------- API 辅助
class GitHubAPI:
    def __init__(self, owner: str, repo: str, token: str) -> None:
        self.owner, self.repo, self.token = owner, repo, token
        self.blob_cache: dict[str, str] = {}
        self.tree_cache: dict[str, str] = {}
        self.commit_cache: dict[str, str] = {}

    def _req(self, method: str, path: str, payload=None):
        url = f"{API}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "TradeDesk-AI-ApiPusher")
        if data is not None:
            req.add_header("Content-Type", "application/json; charset=utf-8")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            raise RuntimeError(f"{method} {path} → HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"{method} {path} → 网络不可达：{e.reason}。"
                f"若本机开着代理，可设置环境变量 TRADEDESK_PROXY=http://127.0.0.1:7897 后重试"
            ) from e

    # ---- 对象创建 ----
    def ensure_blob(self, sha: str) -> str:
        if sha in self.blob_cache:
            return sha
        raw = git_bytes("cat-file", "blob", sha)
        out = self._req("POST", f"/repos/{self.owner}/{self.repo}/git/blobs",
                        {"content": base64.b64encode(raw).decode("ascii"), "encoding": "base64"})
        got = out["sha"]
        if got != sha:
            print(f"    ! blob SHA 不一致：本地 {sha[:10]} → 远端 {got[:10]}")
        self.blob_cache[sha] = got
        return got

    def ensure_tree(self, tree_sha: str) -> str:
        if tree_sha in self.tree_cache:
            return tree_sha
        entries = []
        for line in git("ls-tree", tree_sha).splitlines():
            meta, path = line.split("\t", 1)
            mode, _kind, sha = meta.split()
            entries.append({"path": path, "mode": mode, "type": "blob", "sha": self.ensure_blob(sha)})
        out = self._req("POST", f"/repos/{self.owner}/{self.repo}/git/trees", {"tree": entries})
        got = out["sha"]
        if got != tree_sha:
            print(f"    ! tree SHA 不一致：本地 {tree_sha[:10]} → 远端 {got[:10]}")
        self.tree_cache[tree_sha] = got
        return got

    def ensure_commit(self, sha: str) -> str:
        if sha in self.commit_cache:
            return self.commit_cache[sha]
        raw = git("cat-file", "commit", sha)
        head, _, message = raw.partition("\n\n")
        tree = ""
        parents = []
        author = {}
        committer = {}
        for line in head.splitlines():
            if line.startswith("tree "):
                tree = line[5:].strip()
            elif line.startswith("parent "):
                parents.append(line[7:].strip())
            elif line.startswith("author "):
                author = self._parse_ident(line[7:])
            elif line.startswith("committer "):
                committer = self._parse_ident(line[10:])
        payload = {
            "message": message,
            "tree": self.ensure_tree(tree),
            "parents": [self.ensure_commit(p) for p in parents],
            "author": author,
            "committer": committer,
        }
        out = self._req("POST", f"/repos/{self.owner}/{self.repo}/git/commits", payload)
        got = out["sha"]
        if got != sha:
            print(f"    ! commit SHA 不一致：本地 {sha[:10]} → 远端 {got[:10]}")
        self.commit_cache[sha] = got
        return got

    @staticmethod
    def _parse_ident(s: str) -> dict:
        """把 'Name <mail> 1752938400 +0800' 解析成 API 需要的 name/email/date。"""
        name, _, rest = s.partition(" <")
        email, _, date = rest.partition("> ")
        date = date.strip()
        # 原始格式：epoch +tz  →  转为 ISO 8601（保留同一时区偏移，SHA 才能一致）
        iso = date
        if " " in date:
            epoch, tz = date.split(" ", 1)
            sign = 1 if tz.startswith("+") else -1
            tz = tz.lstrip("+-")
            off = sign * (int(tz[:2]) * 60 + int(tz[2:4]))
            import datetime as _dt
            dt = _dt.datetime.fromtimestamp(int(epoch), _dt.timezone(_dt.timedelta(minutes=off)))
            iso = dt.isoformat()
        return {"name": name.strip(), "email": email.strip(), "date": iso}

    # ---- 空仓库引导 ----
    def ensure_repo_initialized(self) -> bool:
        """空仓库不能直接创建 blob/tree/commit（GitHub 返回 409 Git Repository is empty.）。
        先用 Contents API 建一个初始 README，让仓库有第一个 commit，之后 git 数据 API 才可用。"""
        try:
            self._req("GET", f"/repos/{self.owner}/{self.repo}/contents/README.md")
            return False
        except RuntimeError as e:
            msg = str(e)
            if "HTTP 401" in msg or "HTTP 403" in msg:
                raise
            if "HTTP 409" in msg and "empty" in msg.lower():
                pass          # 空仓库：正好需要引导
            elif "HTTP 404" in msg:
                pass          # 没有 README 但仓库非空
            else:
                raise
        content = base64.b64encode(
            ("# " + self.repo + "\n\n由 TradeDesk-AI 发布脚本初始化。\n").encode("utf-8")
        ).decode("ascii")
        print("    · 仓库为空：先用 Contents API 建初始 commit（让仓库不再是空仓库）")
        self._req("PUT", f"/repos/{self.owner}/{self.repo}/contents/README.md",
                  {"message": "chore: initialize repository (TradeDesk-AI publisher)",
                   "content": content})
        print(f"    {OK_MARK} 初始 commit 已建立")
        return True

    # ---- 分支引用 ----
    def get_ref(self, branch: str):
        try:
            return self._req("GET", f"/repos/{self.owner}/{self.repo}/git/ref/heads/{branch}")
        except RuntimeError as e:
            msg = str(e)
            if "HTTP 401" in msg or "HTTP 403" in msg:
                raise RuntimeError(
                    "Token 无效或权限不足（%s）。请检查 Token 是否过期、是否勾选了 repo 权限。" % msg[:120]
                ) from e
            if "HTTP 404" in msg:
                return None      # 远端还没有这个分支
            if "HTTP 409" in msg and "empty" in msg.lower():
                return None      # 空仓库：GitHub 返回 409 "Git Repository is empty."
            raise

    def set_ref(self, branch: str, sha: str, force: bool = True) -> None:
        self._req("PATCH", f"/repos/{self.owner}/{self.repo}/git/refs/heads/{branch}",
                  {"sha": sha, "force": force})


def main() -> int:
    ap = argparse.ArgumentParser(description="用 GitHub API 推送本地分支")
    ap.add_argument("--owner", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--branches", nargs="+", default=["main", "agent-a", "agent-b"])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    token = os.environ.get("TRADEDESK_GITHUB_TOKEN", "")
    if not token and not args.dry_run:
        token = getpass.getpass("GitHub Token（不回显）: ").strip()
    if not token and not args.dry_run:
        print("没有 Token，终止。")
        return 1

    api = GitHubAPI(args.owner, args.repo, token or "dry-run-placeholder")
    print(f"仓库：{args.owner}/{args.repo}")
    try:
        return _push_all(api, args)
    except RuntimeError as e:
        print(f"\n{FAIL_MARK} 推送失败：{e}")
        return 1
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130


def _push_all(api, args) -> int:

    for branch in args.branches:
        try:
            commits = git("rev-list", "--reverse", branch).split()
        except RuntimeError as e:
            print(f"[{branch}] 本地分支不存在或出错：{e}")
            return 1
        if args.dry_run:
            print(f"\n[{branch}] 本地 {len(commits)} 个提交（dry-run，不访问网络）")
            for c in commits:
                print(f"    would push {c[:10]}  {git('log','-1','--format=%s',c)}")
            continue

        if not api.ensure_repo_initialized():
            pass
        ref = api.get_ref(branch)
        remote_sha = ref["object"]["sha"] if ref else None
        print(f"\n[{branch}] 本地 {len(commits)} 个提交；远端当前：{remote_sha or '（无）'}")

        last = remote_sha
        for c in commits:
            if last and c == last:
                continue
            new_sha = api.ensure_commit(c)
            if new_sha != last:
                api.set_ref(branch, new_sha)
                last = new_sha
            print(f"    {OK_MARK} {c[:10]} {git('log', '-1', '--format=%s', c)}")

        # 核验
        ref2 = api.get_ref(branch)
        remote_now = ref2["object"]["sha"] if ref2 else None
        local_tip = git("rev-parse", branch).strip()
        same = f"{OK_MARK} 一致" if remote_now == local_tip else f"{FAIL_MARK} 不一致（远端 {remote_now} / 本地 {local_tip}）"
        print(f"  → 远端分支 {branch}：{same}")

    print("\n完成。可用下面的命令复核：")
    print(f"  git ls-remote --heads https://github.com/{args.owner}/{args.repo}.git")
    return 0


if __name__ == "__main__":
    sys.exit(main())