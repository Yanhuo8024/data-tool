# -*- coding: utf-8 -*-
"""
tools/arch_context.py — 模块阅读包（spec 先行）

对标 ZCode `architecture:context <module-id>`：把「契约 + 直接依赖 + 相关测试」
打包给 agent/人读，避免把整份实现塞进上下文。

用法：
  python tools/arch_context.py ai_agent
  python tools/arch_context.py agent_tool_registry --tests
  python tools/arch_context.py --modules
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _module_path(name: str) -> Path:
    for cand in (ROOT / f"{name}.py", ROOT / name / "__init__.py", ROOT / "routes" / f"{name}.py"):
        if cand.exists():
            return cand
    raise FileNotFoundError(name)


def _public_api(path: Path) -> list[dict]:
    tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    out = []
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and not n.name.startswith("_"):
            out.append({"kind": "fn", "name": n.name,
                        "doc": (ast.get_docstring(n) or "").split("\n")[0][:80],
                        "lineno": n.lineno})
        elif isinstance(n, ast.ClassDef) and not n.name.startswith("_"):
            methods = [m.name for m in n.body if isinstance(m, ast.FunctionDef) and not m.name.startswith("_")]
            out.append({"kind": "class", "name": n.name, "methods": methods[:12],
                        "doc": (ast.get_docstring(n) or "").split("\n")[0][:80],
                        "lineno": n.lineno})
    return out


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    mods = set()
    for n in tree.body:
        if isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module.split(".")[0])
        elif isinstance(n, ast.Import):
            for a in n.names:
                mods.add(a.name.split(".")[0])
    return sorted(mods)


def _related_tests(name: str) -> list[str]:
    out = []
    for f in (ROOT / "tests").glob("test_*.py"):
        t = f.read_text(encoding="utf-8", errors="ignore")
        if name in t:
            out.append(f.name)
    return out[:12]


def context_pack(module: str, with_tests: bool = True) -> str:
    path = _module_path(module)
    rel = path.relative_to(ROOT)
    api = _public_api(path)
    imps = _imports(path)
    lines = [
        f"# 模块阅读包 · {module}",
        f"- 路径: `{rel}`",
        f"- 行数: {sum(1 for _ in path.open(encoding='utf-8', errors='ignore'))}",
        f"- 直接 import（顶层）: {', '.join(imps[:20]) or '—'}",
        "",
        "## 公开 API（契约面）",
    ]
    if not api:
        lines.append("（无公开函数/类）")
    for item in api:
        if item["kind"] == "fn":
            lines.append(f"- `def {item['name']}()` — {item['doc']} (L{item['lineno']})")
        else:
            methods = ", ".join(item.get("methods") or [])
            lines.append(f"- `class {item['name']}` — {item['doc']} (L{item['lineno']}) methods: {methods}")
    if with_tests:
        lines += ["", "## 相关测试"]
        tests = _related_tests(module)
        lines += [f"- `tests/{t}`" for t in tests] or ["- （无直接引用）"]
    lines += [
        "",
        "## 改动前注意",
        f"- 先读本包，再按需读 `{rel}` 实现细节。",
        "- 改完跑 `python tools/arch_guard.py`（须 exit=0）与相关 pytest。",
        "- 新增公开符号请同步到调用方与测试。",
    ]
    return "\n".join(lines)


def list_modules() -> list[str]:
    names = []
    for p in ROOT.glob("*.py"):
        if not p.name.startswith("_"):
            names.append(p.stem)
    return sorted(names)[:40]


if __name__ == "__main__":
    if "--modules" in sys.argv:
        print("\n".join(list_modules()))
        raise SystemExit(0)
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not args:
        print(__doc__)
        raise SystemExit(1)
    print(context_pack(args[0], with_tests="--tests" in sys.argv or True))
