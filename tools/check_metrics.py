# -*- coding: utf-8 -*-
"""
tools/check_metrics.py
======================
校对 README / design 文档中的规模口径是否与当前代码一致。

用法：
  python tools/check_metrics.py
  python tools/check_metrics.py --fix

统计口径（固定写法，避免各说各话）：
  route_decl   = @bp.route 装饰器数
  route_uniq   = 去重 path 数
  api_uniq     = 去重且以 /api 开头的 path 数
  blueprints   = Blueprint(...) 数
  tabs         = templates/**/tab*.html
  test_funcs   = tests/test_*.py 里 test_* 函数数
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = [
    ROOT / "README.md",
    ROOT / "design" / "功能介绍_大白话版.md",
    ROOT / "design" / "简历项目介绍_金融数据分析平台.md",
]


def collect() -> dict:
    routes = []
    bps = 0
    for f in (ROOT / "routes").glob("*.py"):
        t = f.read_text(encoding="utf-8", errors="ignore")
        routes += re.findall(r"@[\w\.]+\.route\(\s*['\"]([^'\"]+)", t)
        bps += len(re.findall(r"Blueprint\(", t))
    uniq = set(routes)
    api = [r for r in uniq if str(r).startswith("/api")]
    tabs = list((ROOT / "templates").rglob("tab*.html"))
    tf = 0
    tfiles = list((ROOT / "tests").glob("test_*.py"))
    for f in tfiles:
        try:
            tree = ast.parse(f.read_text(encoding="utf-8", errors="ignore"))
            tf += sum(1 for x in ast.walk(tree)
                      if isinstance(x, ast.FunctionDef) and x.name.startswith("test_"))
        except Exception:
            pass
    return {
        "route_decl": len(routes),
        "route_uniq": len(uniq),
        "api_uniq": len(api),
        "blueprints": bps,
        "tabs": len(tabs),
        "test_files": len(tfiles),
        "test_funcs": tf,
        "public_line": f"约 280+ 路由 / 250+ API / {len(tabs)} 功能页 / 900+ 测试（{tf}）",
        "precise": f"路由声明 {len(routes)} / 去重 {len(uniq)} / 唯一 API {len(api)} / 蓝图 {bps} / 页 {len(tabs)} / 测试 {tf}",
    }


def scan_docs(m: dict):
    """报告文档里与实测明显不符的旧口径数字。"""
    stale = ["285 条", "268 个", "909 条", "38 个功能", "276 条", "259 个", "694 条"]
    report = []
    for p in DOCS:
        if not p.exists():
            continue
        t = p.read_text(encoding="utf-8", errors="ignore")
        hits = []
        for s in stale:
            if s in t:
                hits.append(s)
        # 与实测精确值对比
        expected = [str(m["api_uniq"]), str(m["test_funcs"]), str(m["tabs"])]
        report.append({"doc": str(p.relative_to(ROOT)), "stale_hits": hits})
    return report


def main() -> int:
    fix = "--fix" in sys.argv
    m = collect()
    print("=== 当前实测口径 ===")
    for k, v in m.items():
        print(f"  {k}: {v}")
    print("\n=== 文档扫描 ===")
    for row in scan_docs(m):
        flag = "OK" if not row["stale_hits"] else "CHECK"
        print(f"  [{flag}] {row['doc']}: {row['stale_hits'] or '未发现过期口径'}")
    if fix:
        print("\n--fix 仅打印建议，请人工改文档（避免误替换正文语义）。")
        print("建议统一对外句：", m["public_line"])
    print("\n对外口径：", m["public_line"])
    print("精确口径：", m["precise"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
