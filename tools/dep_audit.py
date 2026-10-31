# -*- coding: utf-8 -*-
"""
复果AI 后端 · 依赖审计工具 (独立演进自建机制)

背景
----
2026-07-27 复果后端建立自有依赖审计机制，保障独立演进(self-evolution)。
依赖安全更新 / CVE 修复由本工具与月度审计节奏保障。
本工具定期扫描 requirements.txt 的过期版本与已知漏洞。

用法
----
    python tools/dep_audit.py            # 扫描并写 tools/dep_audit_report.md
    python tools/dep_audit.py --quiet    # 仅打印摘要到 stdout，不写文件
    python tools/dep_audit.py --req PATH # 指定 requirements 文件(默认仓库根 requirements.txt)

实现说明
--------
- 纯标准库 (urllib + json) 查询 PyPI JSON API 获取 latest 版本，无需第三方依赖。
- 版本比较为语义化启发式 (数字段 > 预发布文本段)，满足日常过期判断，不追求 PEP440 完全兼容。
- 有意锁定的包 (见 INTENTIONALLY_PINNED) 来自 requirements.txt 的"暂不推荐升级"注释，
  即便落后也不计入"建议升级"，仅标注 INTENTIONALLY-PINNED。
- CVE 扫描为可选增强：若环境已安装 pip-audit，则自动运行 `pip-audit -r requirements.txt`
  并把结果附到报告；未安装时给出安装提示，不阻塞审计。
- 网络不可达时单个包降级为 UNKNOWN，不影响整体报告。

退出码
------
0 = 无过期/无 CVE 或仅有锁定包
1 = 存在建议升级的过期包 (非锁定)
2 = 检测到 CVE (High/Critical) 或运行异常
"""
import os
import re
import sys
import json
import subprocess
import urllib.request
from datetime import datetime, timezone

# requirements.txt 中明确"暂不推荐升级"的包(上游兼容/重训成本)
INTENTIONALLY_PINNED = {"torch", "torchaudio", "numpy", "websockets"}

REPORT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dep_audit_report.md")


def version_key(v):
    """把版本串转成可比较的 key 列表。数字段 (1,n,'') 视为高于预发布文本段 (0,0,text)。"""
    v = re.sub(r"^[vV]", "", v.strip())
    segs = re.split(r"[.\-+_]+", v)
    out = []
    for s in segs:
        if re.fullmatch(r"\d+", s):
            out.append((1, int(s), ""))
        else:
            out.append((0, 0, s.lower()))
    while len(out) < 5:
        out.append((1, 0, ""))
    return out


def cmp_version(a, b):
    ka, kb = version_key(a), version_key(b)
    return (ka > kb) - (ka < kb)


def parse_requirements(path):
    """返回 [(raw_name, spec, operator, version), ...]，忽略注释/空行/非 pip 行。"""
    pkgs = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("-") or line.startswith("--"):
                continue
            m = re.match(r"^([A-Za-z0-9_.\-]+)\s*(==|>=|<=|~=|!=|>)?\s*([A-Za-z0-9_.\-\*]+)?$", line)
            if not m:
                continue
            name, op, ver = m.group(1), m.group(2), m.group(3) or ""
            pkgs.append((name, op or "", ver))
    return pkgs


def latest_version(pkg):
    """查 PyPI latest 版本；失败返回 None。"""
    norm = pkg.lower().replace("_", "-")
    url = f"https://pypi.org/pypi/{norm}/json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "fuguoai-dep-audit"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.load(r)
        return data["info"]["version"]
    except Exception:
        return None


def run_cve_scan(req_path):
    """可选: 若 pip-audit 可用则扫描并返回 (summary_text, has_high)。"""
    try:
        subprocess.run(["pip-audit", "--version"], capture_output=True, check=True, timeout=10)
    except Exception:
        return ("(pip-audit 未安装，跳过 CVE 扫描。建议: pip install pip-audit 后重跑)", False)
    try:
        res = subprocess.run(
            ["pip-audit", "-r", req_path, "--desc", "-f", "json"],
            capture_output=True, text=True, timeout=300,
        )
        try:
            data = json.loads(res.stdout)
            vulns = data.get("dependencies", [])
            total = sum(len(d.get("vulns", [])) for d in vulns)
            has_high = any(
                any(v.get("severity") in ("high", "critical") for v in d.get("vulns", []))
                for d in vulns
            )
            return (res.stdout if res.stdout.strip() else res.stderr, has_high)
        except Exception:
            return (res.stdout or res.stderr, False)
    except Exception as e:
        return (f"pip-audit 运行失败: {e}", False)


def main():
    args = sys.argv[1:]
    quiet = "--quiet" in args
    req_arg = None
    if "--req" in args:
        i = args.index("--req")
        if i + 1 < len(args):
            req_arg = args[i + 1]

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    req_path = req_arg or os.path.join(repo_root, "requirements.txt")
    if not os.path.exists(req_path):
        print(f"[ERR] requirements 未找到: {req_path}", file=sys.stderr)
        return 2

    pkgs = parse_requirements(req_path)
    rows = []
    outdated_count = 0
    pinned_old = 0
    cve_high = False

    for name, op, ver in pkgs:
        latest = latest_version(name)
        norm = name.lower().replace("_", "-")
        if latest is None:
            status = "UNKNOWN"
            note = "网络/PyPI 不可达"
        elif op == "==":
            if cmp_version(ver, latest) < 0:
                if norm in INTENTIONALLY_PINNED:
                    status = "PINNED-OLD"
                    note = "有意锁定(暂不升级)"
                    pinned_old += 1
                else:
                    status = "OUTDATED"
                    note = "建议评估升级"
                    outdated_count += 1
            else:
                status = "LATEST"
                note = "已是最新"
        elif op in (">=", "<=", "~=", ">", "!="):
            if ver and cmp_version(ver, latest) < 0:
                status = "OUTDATED"
                note = f"{op}{ver} 但最新为 {latest}"
                outdated_count += 1
            else:
                status = "LATEST"
                note = "满足约束"
        else:  # 无版本约束
            status = "UNPINNED"
            note = f"无上限约束(最新 {latest})"
            outdated_count += 1
        rows.append((name, op + ver, latest or "-", status, note))

    cve_text, cve_high = run_cve_scan(req_path)

    # 汇总
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = []
    lines.append(f"# 复果AI 后端依赖审计报告")
    lines.append("")
    lines.append(f"- 生成时间: {now}")
    lines.append(f"- 依赖文件: `{os.path.relpath(req_path, repo_root)}`")
    lines.append(f"- 总包数: {len(pkgs)}  | 建议升级: {outdated_count}  | 有意锁定落后: {pinned_old}  | CVE(High/Critical): {'是' if cve_high else '否'}")
    lines.append("")
    lines.append("## 明细")
    lines.append("")
    lines.append("| 包 | 声明 | PyPI最新 | 状态 | 备注 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for name, decl, latest, status, note in sorted(rows, key=lambda r: (r[3] != "OUTDATED", r[3] != "UNPINNED", r[0].lower())):
        lines.append(f"| {name} | {decl or '(无)'} | {latest} | {status} | {note} |")
    lines.append("")
    lines.append("## CVE 扫描 (pip-audit)")
    lines.append("")
    lines.append("```")
    lines.append(cve_text)
    lines.append("```")
    lines.append("")
    lines.append("## 处置建议")
    lines.append("")
    lines.append("1. OUTDATED / UNPINNED 包: 评估升级，优先处理有安全公告者。")
    lines.append("2. PINNED-OLD 包: 仅在确认兼容(尤其 torch/numpy/websockets 牵一发动全身)时升级。")
    lines.append("3. CVE(High/Critical): 24 小时内评估并出补丁版本(见 RELEASE.md)。")
    lines.append("4. 本审计为复果自建机制，每月 1 次 + 每次发版前必跑。")
    report = "\n".join(lines) + "\n"

    if not quiet:
        with open(REPORT_PATH, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"[OK] 报告已写: {REPORT_PATH}")

    # stdout 摘要
    print(f"包数={len(pkgs)}  建议升级={outdated_count}  有意锁定落后={pinned_old}  CVE高危={'是' if cve_high else '否'}")
    for name, decl, latest, status, note in rows:
        if status in ("OUTDATED", "UNPINNED"):
            print(f"  [升级] {name}: {decl or '(无)'} -> {latest} ({note})")

    if cve_high or outdated_count > 0:
        return 1 if not cve_high else 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
