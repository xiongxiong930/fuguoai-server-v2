# -*- coding: utf-8 -*-
"""
复果AI 后端 · 与参考基线的相似度度量（只读，不改任何代码）

用法
----
    python tools/similarity_report.py            # 写 tools/similarity_report.md
    python tools/similarity_report.py --quiet    # 仅打印摘要

方法
----
- 以 fuguoai-server/ 为"我们"，_reference_snapshot/ 为参考基线（默认本仓 fork
  起点 8f051a8 自有历史快照，缺失时自动从 git 重建，不依赖任何上游快照）。度量的是
  "我们相对参考基线改了多少"；基线换成自己历史后，数值即'自 fork 以来的真实自进化度'。
- 两种配对口径：
  1) 相对路径对齐：直接用文件相对路径配对（旧指标，因包名 fuguoai_core↔core
     不一致而几乎不重叠，会严重低估真实差异；仅作对照）。
  2) 内容级归一化：把每段首部的 `fuguoai_` 前缀剥掉（fuguoai_core/utils/util.py
     → core/utils/util.py）再双侧配对，这才是真实的内容自研度。
- 每对共同文件：字节相同 => 相似度 1.0(一字未改)；否则用 difflib 行级 ratio。
- 指标：
  - 条件相似度 = 仅统计"路径对齐/归一化对齐"的文件（复果专属排除在分母外）。
  - 整体重叠率 = 把复果专属文件计入分母（匹配行=0），回答"我们代码多大比例像上游"。

输出
----
tools/similarity_report.md：双口径摘要 + 一字未改清单 + 疑似未用 + 已改低相似度。
"""

import os
import ast
import difflib
import sys
import io
import subprocess
import tarfile
import warnings

SCRIPT = os.path.abspath(__file__)
# .../fuguoai-server/tools/similarity_report.py -> 上 3 级 = git 根（与 _reference_snapshot 并列）
GIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(SCRIPT)))
OUR = os.path.join(GIT_ROOT, "fuguoai-server")
REF = os.path.join(GIT_ROOT, "_reference_snapshot")
# 参考基线：本仓 fork 起点 8f051a8 的自有历史快照，度量"自 fork 以来真实自进化度"。
# 不依赖任何上游快照；若快照目录缺失，脚本在分析前自动用 git archive 从 8f051a8 重建（缺失自己补）。
# 可用 FUGUOAI_REF_SUB 环境变量手动覆盖为任意基线目录（默认即下方本仓快照）。
SELF_BASELINE_COMMIT = "8f051a8"
SELF_BASELINE_SUB = os.path.join(REF, "self", "fork-start", "fuguoai-server")
REF_SUB = os.environ.get("FUGUOAI_REF_SUB") or SELF_BASELINE_SUB
REPORT = os.path.join(OUR, "tools", "similarity_report.md")


def materialize_self_baseline():
    """参考基线缺失时，从本仓 git 历史(8f051a8)重建 fork 起点快照；不回退任何上游。"""
    target_parent = os.path.join(REF, "self", "fork-start")
    os.makedirs(target_parent, exist_ok=True)
    try:
        proc = subprocess.run(
            ["git", "-C", GIT_ROOT, "archive", SELF_BASELINE_COMMIT, "fuguoai-server"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        )
    except FileNotFoundError:
        raise RuntimeError("未找到 git 可执行文件，无法自动重建 fork 起点快照；"
                           "请手动放置快照或设置 FUGUOAI_REF_SUB")
    except subprocess.CalledProcessError as e:
        raise RuntimeError("git archive 失败（commit=%s）：%s"
                           % (SELF_BASELINE_COMMIT, e.stderr.decode(errors="replace")))
    try:
        with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r:*") as tf:
            tf.extractall(target_parent, filter="data")
    except tarfile.TarError as e:
        raise RuntimeError("fork 起点快照解包失败：%s" % e)


def collect_py(root):
    out = {}
    for cur, dirs, files in os.walk(root):
        # 跳过隐藏/缓存目录
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git", "node_modules", "venv")]
        for f in files:
            if f.endswith(".py"):
                ap = os.path.join(cur, f)
                rel = os.path.relpath(ap, root)
                out[rel] = ap
    return out


def normalize_rel(rel):
    """剥掉每段首部的 fuguoai_ 前缀，使 fuguoai_core/utils/util.py 映射到 core/utils/util.py。"""
    parts = rel.split("/")
    parts = [p[len("fuguoai_"):] if p.startswith("fuguoai_") else p for p in parts]
    return "/".join(parts)


def build_ref_graph(our):
    """扫描全仓 import 图 + 动态加载目录，供 is_referenced 判断文件是否被使用。"""
    imports_top = set()
    all_text = []
    for rel, ap in our.items():
        try:
            with open(ap, encoding="utf-8") as fh:
                src = fh.read()
        except Exception:
            continue
        all_text.append(src)
        try:
            # 分析工具只关心 AST 引用图；被扫描文件（含上游一字未改文件）的
            # 无效转义 SyntaxWarning 与相似度度量无关，且改动上游文件会破坏
            # "一字未改"基准，故在此抑制，不污染报告输出。
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                tree = ast.parse(src, filename=ap)
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for nm in node.names:
                    imports_top.add(nm.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports_top.add(node.module.split(".")[0])
    blob = "\n".join(all_text).lower()
    import re
    dyn_referenced_dirs = set()
    for rel, ap in our.items():
        try:
            with open(ap, encoding="utf-8") as fh:
                src = fh.read()
        except Exception:
            continue
        if "spec_from_file_location" in src and "listdir" in src:
            d = os.path.dirname(ap)
            for m in re.finditer("[\"']([^\"']+)[\"']", src):
                cand = os.path.join(d, m.group(1))
                if os.path.isdir(cand):
                    dyn_referenced_dirs.add(cand)
    return imports_top, blob, dyn_referenced_dirs


def analyze(our_map, ref_map, is_referenced):
    """对给定的 {rel: ap} 配对，计算相似度指标（不改任何文件）。"""
    common = sorted(set(our_map) & set(ref_map))
    identical, modified = [], []
    for rel in common:
        try:
            a = open(our_map[rel], encoding="utf-8").read()
            b = open(ref_map[rel], encoding="utf-8").read()
        except Exception:
            modified.append((rel, 0.0, 0))
            continue
        if a == b:
            identical.append(rel)
        else:
            la, lb = a.splitlines(), b.splitlines()
            sim = difflib.SequenceMatcher(None, la, lb).ratio() if (la or lb) else 1.0
            modified.append((rel, sim, len(la)))
    n_common = len(common)
    n_ident = len(identical)
    n_mod = len(modified)
    n_ours = len(our_map) - n_common
    file_sim = (n_ident + sum(s for _, s, _ in modified)) / n_common if n_common else 0.0
    matched_lines = our_lines_total = 0
    for rel in identical:
        n = len(open(our_map[rel], encoding="utf-8").read().splitlines())
        matched_lines += n
        our_lines_total += n
    for _, s, ln in modified:
        matched_lines += int(round(s * ln))
        our_lines_total += ln
    line_sim = matched_lines / our_lines_total if our_lines_total else 0.0
    ours_only_lines = 0
    for rel in (set(our_map) - set(ref_map)):
        try:
            n = len(open(our_map[rel], encoding="utf-8").read().splitlines())
        except Exception:
            n = 0
        ours_only_lines += n
    total_our_lines = our_lines_total + ours_only_lines
    n_total = n_common + n_ours
    overall_file_sim = (n_ident + sum(s for _, s, _ in modified)) / n_total if n_total else 0.0
    overall_line_sim = matched_lines / total_our_lines if total_our_lines else 0.0
    unused_identical = [r for r in identical if not is_referenced(r)]
    barely = sorted([(r, s) for r, s, _ in modified if s > 0.95], key=lambda x: -x[1])
    diverged = sorted([(r, s) for r, s, _ in modified if s < 0.7], key=lambda x: x[1])
    return dict(n_common=n_common, n_ident=n_ident, n_mod=n_mod, n_ours=n_ours,
                file_sim=file_sim, line_sim=line_sim, overall_file_sim=overall_file_sim,
                overall_line_sim=overall_line_sim, identical=identical,
                unused_identical=unused_identical, barely=barely, diverged=diverged)


def main():
    quiet = "--quiet" in sys.argv
    our = collect_py(OUR)
    # 确保参考基线存在：默认本仓 fork 起点快照；缺失则自动从 git 历史重建（不回退任何上游）。
    if not os.environ.get("FUGUOAI_REF_SUB") and not os.path.isdir(SELF_BASELINE_SUB):
        materialize_self_baseline()
    ref = collect_py(REF_SUB)

    imports_top, blob, dyn_referenced_dirs = build_ref_graph(our)

    def is_referenced(rel):
        mod = os.path.splitext(rel)[0].replace(os.sep, ".")
        top = mod.split(".")[0]
        base = os.path.splitext(rel)[0].split("/")[-1].split("\\")[-1]
        return (top in imports_top) or (base.lower() in blob) or (
            os.path.dirname(our[rel]) in dyn_referenced_dirs)

    # 分析 1：相对路径对齐（旧指标，包名不一致时失真，仅作对照）
    res_orig = analyze(our, ref, is_referenced)

    # 分析 2：内容级归一化（剥 fuguoai_ 前缀，真实自研度）。
    # our 与 ref 双侧都归一：无论基线目录是否带 fuguoai_ 前缀（fork 起点快照为
    # core/，未来若某基线带 fuguoai_ 前缀），归一后都能正确配对。
    our_norm = {normalize_rel(r): our[r] for r in our}
    ref_norm = {normalize_rel(r): ref[r] for r in ref}
    orig_by_norm = {normalize_rel(r): r for r in our}

    def is_ref_norm(rel):
        return is_referenced(orig_by_norm.get(rel, rel))

    res_norm = analyze(our_norm, ref_norm, is_ref_norm)

    o, n = res_orig, res_norm
    L = []
    L.append("# 复果AI 后端 · 与参考基线 相似度报告")
    L.append("")
    baseline_name = ("本仓 fork 起点(8f051a8) 自有历史快照"
                    if (os.path.isdir(SELF_BASELINE_SUB) and REF_SUB == SELF_BASELINE_SUB)
                    else REF_SUB)
    L.append(f"> 自动生成（只读扫描，未改动任何代码）。ours=`fuguoai-server`，ref=`{REF_SUB}`")
    L.append(f"> 参考基线：**{baseline_name}**（可用 FUGUOAI_REF_SUB 环境变量手动覆盖；"
             f"缺失时自动从 git 重建本仓 fork 起点快照，不回退上游）")
    L.append("")
    L.append("## 摘要指标（相对路径对齐口径）")
    L.append("")
    L.append(f"- 共同 .py 文件：**{o['n_common']}**")
    L.append(f"- 一字未改（相似度=1.0）：**{o['n_ident']}** ({o['n_ident']/max(o['n_common'],1)*100:.1f}%)")
    L.append(f"- 复果专属（上游无对应）：**{o['n_ours']}**")
    L.append(f"- 文件级平均相似度（条件·仅路径对齐文件）：{o['file_sim']*100:.1f}%")
    L.append(f"- 行加权相似度（条件·仅路径对齐文件）：{o['line_sim']*100:.1f}%")
    L.append(f"- 文件级整体重叠率（含复果专属=0）：{o['overall_file_sim']*100:.1f}%")
    L.append(f"- 行加权整体重叠率（含复果专属=0）：{o['overall_line_sim']*100:.1f}%")
    L.append("")
    L.append("## 关键：内容级归一化相似度（剥 `fuguoai_` 前缀后配对上游）")
    L.append("")
    L.append("> 相对路径口径因包名 fuguoai_core↔core 不一致而几乎不重叠，严重低估真实差异。")
    L.append("> 此段剥掉 `fuguoai_` 前缀再双侧配对，才是真实的内容自研度；自进化进度以此段为准。")
    L.append("")
    L.append(f"- 内容级共同 .py 文件：**{n['n_common']}**")
    L.append(f"- 内容级一字未改：**{n['n_ident']}**")
    L.append(f"- 内容级复果专属：**{n['n_ours']}**")
    L.append(f"- **内容级文件级平均相似度（条件）：{n['file_sim']*100:.1f}%**")
    L.append(f"- **内容级行加权相似度（条件）：{n['line_sim']*100:.1f}%**")
    L.append(f"- **内容级文件级整体重叠率（含复果专属=0）：{n['overall_file_sim']*100:.1f}%**")
    L.append(f"- **内容级行加权整体重叠率（含复果专属=0）：{n['overall_line_sim']*100:.1f}%**")
    L.append("")
    L.append("### 内容级几乎未改的高相似模块（>0.95，重构候选）")
    L.append("")
    if n['barely']:
        for r, s in n['barely']:
            L.append(f"- `{r}`  sim={s*100:.1f}%")
    else:
        L.append("- （无）")
    L.append("")
    L.append("### 内容级已明显自研的模块（<0.7，已是复果设计）")
    L.append("")
    if n['diverged']:
        for r, s in n['diverged']:
            L.append(f"- `{r}`  sim={s*100:.1f}%")
    else:
        L.append("- （无）")
    L.append("")
    L.append("### 内容级一字未改文件全清单（按归一化路径）")
    L.append("")
    for r in n['identical']:
        mark = "  ⚠疑似未用" if r in n['unused_identical'] else ""
        L.append(f"- `{r}`{mark}")
    L.append("")
    L.append("## 相对路径口径附属清单")
    L.append("")
    L.append("### 疑似未使用的一字未改文件（删前需 grep 复核配置名/字符串引用）")
    L.append("")
    if o['unused_identical']:
        for r in o['unused_identical']:
            L.append(f"- `{r}`")
    else:
        L.append("- （无）")
    L.append("")
    L.append("### 相对路径高相似模块（>0.95）")
    L.append("")
    if o['barely']:
        for r, s in o['barely']:
            L.append(f"- `{r}`  sim={s*100:.1f}%")
    else:
        L.append("- （无）")
    L.append("")
    L.append("### 相对路径一字未改清单")
    L.append("")
    for r in o['identical']:
        L.append(f"- `{r}`")
    L.append("")

    report = "\n".join(L) + "\n"
    if not quiet:
        with open(REPORT, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"[OK] 报告已写: {REPORT}")
    print(f"[路径对齐] 共同={o['n_common']} 一字未改={o['n_ident']} 复果专属={o['n_ours']} 整体重叠率={o['overall_file_sim']*100:.1f}%")
    print(f"[内容级·归一化] 共同={n['n_common']} 一字未改={n['n_ident']} 复果专属={n['n_ours']} 条件相似度={n['file_sim']*100:.1f}% 整体重叠率={n['overall_file_sim']*100:.1f}%")
    print(f"  内容级高相似(>0.95)={len(n['barely'])}  已自研(<0.7)={len(n['diverged'])}")


if __name__ == "__main__":
    main()
