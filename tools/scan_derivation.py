#!/usr/bin/env python3
"""全仓派生扫描：工作树 .py vs 上游 MIT 原仓同名文件，canonical FULL/LOGIC + git 改动状态。

判据（沿用 MIT_DECOUPLING_NATIVE.md）：
  - 上游无同名  => 复果自有，不在待办
  - 上游有同名  => 派生候选；handler/util 类目标 LOGIC <0.40（骨架/协议类适用等价性代价豁免）
"""
import io
import os
import subprocess
import sys
import tokenize
import difflib

ROOT = "/Users/xiongxiong/WorkBuddy/2026-07-09-21-43-20"
UP = os.path.join(ROOT, "_reference_snapshot/main/xiaozhi-server")
WT = os.path.join(ROOT, "fuguoai-server")
SCAN_DIRS = ["fuguoai_core", "fuguoai_plugins", "fuguoai_config"]
EXTRA = ["app.py", "websocket_server.py", "connection.py"]


def strip_logic(src):
    out = []
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except Exception:
        return "\n".join(l.split("#", 1)[0] for l in src.splitlines() if l.strip())
    prev = None
    for tok in toks:
        t, s = tok.type, tok.string
        if t in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT):
            continue
        if t == tokenize.STRING:
            if prev is None or prev.type in (tokenize.NEWLINE, tokenize.INDENT,
                                             tokenize.DEDENT, tokenize.NL):
                continue
        out.append(s)
        prev = tok
    return " ".join(out)


def ratio(a, b):
    return difflib.SequenceMatcher(None, a, b).ratio()


def build_upstream_index():
    idx = {}
    for dp, _, fns in os.walk(UP):
        if "__pycache__" in dp:
            continue
        for fn in fns:
            if fn.endswith(".py"):
                idx.setdefault(fn, []).append(os.path.join(dp, fn))
    return idx


def git_changed():
    out = subprocess.run(["git", "-C", ROOT, "status", "--short"],
                         capture_output=True, text=True).stdout
    changed = set()
    for line in out.splitlines():
        st, path = line[:2], line[3:].strip()
        if "M" in st or "A" in st:
            changed.add(os.path.normpath(path))
    return changed


def main():
    idx = build_upstream_index()
    changed = git_changed()
    rows = []
    files = []
    for d in SCAN_DIRS:
        for dp, _, fns in os.walk(os.path.join(WT, d)):
            if "__pycache__" in dp:
                continue
            for fn in fns:
                if fn.endswith(".py"):
                    files.append(os.path.join(dp, fn))
    for e in EXTRA:
        p = os.path.join(WT, e)
        if os.path.exists(p):
            files.append(p)

    for p in sorted(set(files)):
        fn = os.path.basename(p)
        ups = idx.get(fn)
        if not ups:
            continue  # 复果自有（上游无同名）
        try:
            s_new = open(p, encoding="utf-8").read()
        except Exception:
            continue
        best = None
        for up in ups:
            try:
                s_old = open(up, encoding="utf-8").read()
            except Exception:
                continue
            full = ratio(s_old, s_new)
            logic = ratio(strip_logic(s_old), strip_logic(s_new))
            if best is None or logic > best[2]:
                best = (full, logic, logic, up)
        if best is None:
            continue
        full, logic, _, up = best
        rel = os.path.relpath(p, ROOT)
        rows.append((logic, full, rel, rel in changed, os.path.relpath(up, ROOT)))

    rows.sort(reverse=True)
    print(f"扫描到「上游有同名」的工作树文件: {len(rows)} 个\n")
    print(f"{'LOGIC':>6} {'FULL':>6} {'已改':>4}  文件")
    print("-" * 100)
    for logic, full, rel, is_ch, up in rows:
        flag = "M" if is_ch else "-"
        print(f"{logic:6.3f} {full:6.3f} {flag:>4}  {rel}")

    # 汇总
    ch = [r for r in rows if r[3]]
    un = [r for r in rows if not r[3]]
    print("\n=== 汇总 ===")
    print(f"上游有同名总计      : {len(rows)}")
    print(f"  已改(M)           : {len(ch)}   其中 LOGIC>=0.40(未达标): {sum(1 for r in ch if r[0] >= 0.40)}")
    print(f"  未改(漏网候选)    : {len(un)}   其中 LOGIC>=0.40(确为派生): {sum(1 for r in un if r[0] >= 0.40)}")
    print("\n=== 未改且 LOGIC>=0.40（真漏网，从未重写）===")
    for logic, full, rel, _, up in un:
        if logic >= 0.40:
            print(f"  {logic:6.3f}  {rel}   (上游: {up})")


if __name__ == "__main__":
    sys.exit(main())
