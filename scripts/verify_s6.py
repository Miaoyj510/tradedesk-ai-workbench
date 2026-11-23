# -*- coding: utf-8 -*-
"""S6 \u9a8c\u6536\u811a\u672c\uff1a\u524d\u7aef\u8054\u8c03\uff08\u771f\u540e\u7aef + \u771f\u7b7e\u6838\u94fe\u8def\uff09\u3002

\u7528\u6cd5
    python scripts/verify_s6.py            # \u81ea\u5efa\u4e34\u65f6\u5e93\u8dd1\u540e\u7aef\uff0c\u8dd1\u524d\u7aef\u6570\u636e\u6e90\u8054\u8c03\uff0c\u8dd1\u5b8c\u81ea\u6e05\u7406
    python scripts/verify_s6.py --port 8099

\u9a8c\u8bc1\u4ec0\u4e48
    1. \u9759\u6001\uff1a\u524d\u7aef\u4e09\u4e2a\u9875\u9762\u90fd\u5f15\u7528\u4e86 config.js / api.js\uff1bconfig.js \u9ed8\u8ba4 mock\uff08\u53cc\u51fb\u6253\u5f00\u4ecd\u53ef\u6f14\u793a\uff09\u3002
    2. \u540e\u7aef\uff1a`GET /workbench` \u7b26\u5408\u524d\u7aef\u5951\u7ea6\uff08\u4e09\u4e2a\u9875\u9762\u7528\u7684\u5b57\u6bb5\u90fd\u5728\uff09\u3002
    3. \u524d\u7aef\u6570\u636e\u6e90\uff08Node \u771f\u8dd1 ui-prototype/assets/api.js\uff09\uff1a\u771f\u5b9e\u53d1\u8be2\u76d8\u3001\u771f\u7b7e\u6838\u3001\u771f\u53d1\u9001\u3001
       \u4e00\u6b21\u6027\u95f8\u95e8\u4ee4\u724c\u88ab\u62d2\u3002
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
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


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append({"name": name, "ok": bool(ok), "detail": detail})
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (f" \u2014 {detail}" if detail else ""))
    return bool(ok)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http_json(url: str, method: str = "GET", body=None, headers=None):
    data = None
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body_text or "{}")
        except Exception:
            return e.code, {"raw": body_text[:200]}


def start_server(port: int, db_path: str):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    env["TRADEDESK_DB_PATH"] = db_path
    # \u8054\u8c03\u4e0d\u8c03\u4e91\u7aef\uff08\u4e0d\u8ba1\u8d39\uff09\uff1a\u672c\u7247\u9a8c\u8bc1\u7684\u662f\u524d\u540e\u7aef\u5951\u7ea6\uff0c\u4e0d\u662f\u6a21\u578b\u80fd\u529b
    env["TRADEDESK_LLM_DRAFT_ENABLED"] = "0"
    env["TRADEDESK_EMBEDDER_ENABLED"] = "0"
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "tradedesk.api.app:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    for _ in range(60):
        if proc.poll() is not None:
            out = proc.stdout.read().decode("utf-8", "replace") if proc.stdout else ""
            raise RuntimeError("\u540e\u7aef\u542f\u52a8\u5931\u8d25\uff1a\n" + out[-1500:])
        try:
            with urllib.request.urlopen(base + "/health", timeout=2):
                return proc, base
        except Exception:
            time.sleep(0.5)
    proc.terminate()
    raise RuntimeError("\u540e\u7aef\u542f\u52a8\u8d85\u65f6")


def check_static() -> None:
    print("\n--- \u9759\u6001\uff1a\u524d\u7aef\u9875\u9762\u4e0e\u8d44\u6e90\u63a5\u7ebf ---")
    ui = ROOT / "ui-prototype"
    for page in ("index.html", "dashboard.html", "knowledge.html"):
        text = (ui / page).read_text(encoding="utf-8")
        ok = ("assets/config.js" in text and "assets/api.js" in text
              and "assets/data.js" in text and "assets/app.js" in text)
        check(f"{page} \u5f15\u7528 config.js / api.js / data.js / app.js", ok)
    cfg = (ui / "assets" / "config.js").read_text(encoding="utf-8")
    check("\u9ed8\u8ba4\u6570\u636e\u6e90\u662f mock\uff08\u53cc\u51fb\u6253\u5f00\u4ecd\u53ef\u6f14\u793a\uff0c\u4e0d\u4f9d\u8d56\u540e\u7aef\uff09",
          "source: 'mock'" in cfg)
    check("\u771f\u6570\u636e\u6a21\u5f0f\u53ef\u7528 URL \u53c2\u6570\u5207\u6362\uff08?source=api\uff09",
          "source=api" in cfg or "source=(mock|api)" in
          (ui / "assets" / "app.js").read_text(encoding="utf-8"))


def check_backend(base: str) -> None:
    print("\n--- \u540e\u7aef\uff1a/workbench \u8bfb\u6a21\u578b\u5951\u7ea6 ---")
    st, d = http_json(base + "/workbench")
    check("GET /workbench \u8fd4\u56de 200", st == 200, str(st))
    if st != 200:
        return
    for k in ("threads", "kpis", "stages", "gates", "kbAssets", "terms",
              "costByLang", "costTrend", "riskEvents", "quality"):
        check("\u8bfb\u6a21\u578b\u542b " + k, k in d)
    check("quality \u4e3a\u7a7a\u6570\u7ec4\uff08\u5206\u8bed\u79cd\u8d28\u91cf\u4e0d\u662f\u5b9e\u65f6\u6570\u636e\uff0c\u4e0d\u4f2a\u9020\uff09",
          d.get("quality") == [])
    check("\u8bfb\u6a21\u578b\u5e26\u53e3\u5f84\u8bf4\u660e", bool(d.get("notes")))

    st, ing = http_json(base + "/inbound", "POST", {
        "buyer_id": "BY-S6-1", "language": "en", "company": "S6 Trading", "country": "BR",
        "message": "Quote 5000 pcs of X-200 FOB Shenzhen, do you have CE certification?"})
    check("POST /inbound \u4ea7\u751f\u7ebf\u7a0b\u4e0e\u8349\u7a3f", st == 200 and bool(ing.get("draft")),
          f"{ing.get('status')} / {ing.get('thread_id')}")
    st2, wb2 = http_json(base + "/workbench")
    threads = wb2.get("threads") or []
    check("\u65b0\u7ebf\u7a0b\u51fa\u73b0\u5728\u8bfb\u6a21\u578b\u91cc", any(t.get("id") == ing.get("thread_id") for t in threads))
    hit = [t for t in threads if t.get("id") == ing.get("thread_id")]
    if hit:
        t = hit[0]
        check("\u7ebf\u7a0b\u5e26 draft_id\uff08\u7b7e\u6838\u4e0e\u53d1\u9001\u8981\u7528\u5b83\uff09", bool(t.get("draft_id")))
        check("\u4e70\u5bb6\u539f\u8bdd\u5df2\u843d\u5e93\u5e76\u56de\u663e", "5000 pcs" in (t.get("buyer_message") or ""),
              (t.get("buyer_message") or "")[:40])
        g1 = [g for g in t.get("gates", []) if g.get("id") == "GATE-01"]
        check("\u542b\u4ef7\u683c\u7684\u8349\u7a3f\u547d\u4e2d GATE-01", bool(g1 and g1[0].get("hit")))


def run_node(base: str) -> None:
    print("\n--- \u524d\u7aef\u6570\u636e\u6e90\uff08Node \u771f\u8dd1 api.js\uff09---")
    node = shutil.which("node")
    if not node:
        check("\u627e\u5230 node\uff08\u7528\u4e8e\u8dd1\u524d\u7aef\u6570\u636e\u6e90\uff09", False,
              "\u672c\u673a\u6ca1\u6709 node\uff0c\u8df3\u8fc7\u524d\u7aef\u8054\u8c03\u9a8c\u8bc1")
        return
    proc = subprocess.run([node, str(ROOT / "scripts" / "verify_s6_ui.js"), base],
                          cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    print(proc.stdout.rstrip())
    if proc.stderr.strip():
        print(proc.stderr.rstrip()[:800])
    check("\u524d\u7aef\u6570\u636e\u6e90\u8054\u8c03\u5168\u90e8\u901a\u8fc7", proc.returncode == 0,
          f"exit={proc.returncode}")


def main() -> int:
    ap = argparse.ArgumentParser(description="S6 \u9a8c\u6536\uff1a\u524d\u7aef\u8054\u8c03")
    ap.add_argument("--port", type=int, default=0, help="\u540e\u7aef\u7aef\u53e3\uff080 = \u81ea\u52a8\u9009\uff09")
    args = ap.parse_args()

    print("=" * 66)
    print("S6 \u9a8c\u6536 \u2014\u2014 \u524d\u7aef\u8054\u8c03")
    print("=" * 66)

    port = args.port or free_port()
    tmp = tempfile.mkdtemp(prefix="td-verify-s6-")
    proc = None
    try:
        proc, base = start_server(port, os.path.join(tmp, "s6.db"))
        print(f"\u540e\u7aef\uff1a{base}\uff08\u4e34\u65f6\u5e93\uff0c\u4e0d\u8c03\u4e91\u7aef\u3001\u4e0d\u8ba1\u8d39\uff09")
        check_static()
        check_backend(base)
        run_node(base)
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    ok = sum(1 for r in RESULTS if r["ok"])
    bad = [r for r in RESULTS if not r["ok"]]
    print("\n" + "=" * 66)
    print(f"\u9a8c\u6536\u7ed3\u679c\uff1a\u901a\u8fc7 {ok}\uff0cFAIL {len(bad)}\uff0c\u5171 {len(RESULTS)} \u9879")
    for r in bad:
        print(f"  \u2717 {r['name']} \u2014 {r['detail']}")
    print("=" * 66)
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
