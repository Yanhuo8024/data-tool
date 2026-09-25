#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/arch_guard.py — data.tool 架构守卫（零依赖 / 纯标准库 / 可离线）

无 .git → ZCode 式 pre-push 门禁没有宿主，故做成「脚本 + pytest」双入口：手动跑看违规清单、
回归跑自动拦。基线 arch_baseline.json 是棘轮，只允许下降，放宽须显式 --force。

    R0 syntax / R1 file-lines：单文件 ≤ 400 行（存量入基线，只允许收紧）
    R2 swallow-except：except 体只有 pass/continue 的"真吞异常"不得新增（按文件棘轮）
    R3 base-exception：禁捕获 BaseException 基类（测试文件除外——那里是本规则自身的断言）
    R4 lazy-symbol：禁 from core_config import <占位符>（值永不更新，须用 has_*/get_* API）
    R5 import-cycle：禁新增模块级循环 import（函数体内 import 属有意设计，不计）
    R6 test-stub：测试桩 fake_*/stub_* 必须带 **kw（窄签名桩被 except 吞掉后会假通过）
    R7 undefined-name：模块级未定义全局引用（取代已丢失的 output/_scan_undefined_names.py）
    R8 egress-unregistered：出站调用主机名必须在 design/DATA_EGRESS.md 登记（防清单漂移）

CLI：--rule R2,R6 | --json | --strict | --freeze [--force] | --self-test
"""
from __future__ import annotations

import argparse
import ast
import builtins
import json
import os
import re
import sys
from collections import namedtuple
from datetime import datetime

MAX_FILE_LINES = 400
ALL_RULES = ('R0', 'R1', 'R2', 'R3', 'R4', 'R5', 'R6', 'R7', 'R8')
TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TOOLS_DIR)
BASELINE_PATH = os.path.join(TOOLS_DIR, 'arch_baseline.json')
# import 图分析单独成文件（职责分离 + 两边都低于行数门禁）；直接跑本脚本时 tools/ 已在 sys.path[0]
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)
from arch_graph import find_cycles, module_edges, module_index  # noqa: E402
from arch_egress import scan_egress  # noqa: E402
# output/ 是运行期产物目录（CSV/报告/一次性 _probe_*.py 探针），不纳入门禁
SKIP_DIRS = {'__pycache__', '.git', '.workbuddy', '.trae', 'node_modules', 'site-packages',
             'python-portable', 'venv', '.venv', 'dist', 'build', 'archive', 'output'}
R7_SKIP = {'core_config.py'}  # 该文件用 globals()[...] 动态注入，符号表判定不适用
BUILTINS = set(dir(builtins)) | {
    '__name__', '__file__', '__doc__', '__package__', '__spec__', '__loader__',
    '__builtins__', '__all__', '__dict__', '__path__', '__annotations__'}
NEEDLE_BASE_EXC = 'except' + ' ' + 'Base' + 'Exception'  # 拼串：避免本文件自命中 R3
STAR_RE = re.compile(r'^\s*from\s+\S+\s+import\s+\*', re.M)
Violation = namedtuple('Violation', 'rule severity path line msg')


def _read(path):
    with open(path, encoding='utf-8', errors='ignore') as fh:
        return fh.read()


def _iter_py(root):
    for dp, dn, fns in os.walk(root):
        dn[:] = sorted(d for d in dn if d not in SKIP_DIRS and not d.startswith('.'))
        for fn in sorted(f for f in fns if f.endswith('.py')):
            p = os.path.join(dp, fn)
            yield p, os.path.relpath(p, root).replace(os.sep, '/')


def _is_test(rel):
    base = os.path.basename(rel)
    return rel.startswith('tests/') or base.startswith('test_') or base == 'conftest.py'


def _swallow_sites(tree):
    """except 体只含 pass/continue（文档字符串不算语句）→ 真·吞异常的行号"""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            body = [b for b in node.body if not (isinstance(b, ast.Expr)
                    and isinstance(b.value, ast.Constant) and isinstance(b.value.value, str))]
            if len(body) == 1 and isinstance(body[0], (ast.Pass, ast.Continue)):
                out.append(node.lineno)
    return sorted(out)


def _undef_names(src, path):
    """模块级未定义全局引用；star-import 会让符号表判定不可靠 → 整体跳过"""
    if STAR_RE.search(src):
        return set()
    try:
        import symtable
        st = symtable.symtable(src, path, 'exec')
    except (SyntaxError, ValueError, UnicodeError):
        return set()
    defined = {s.get_name() for s in st.get_symbols()}
    bad = set()

    def walk(tbl):
        for sym in tbl.get_symbols():
            nm = sym.get_name()
            if nm not in BUILTINS and nm not in defined and sym.is_global() and sym.is_referenced():
                bad.add(nm)
        for child in tbl.get_children():
            walk(child)

    walk(st)
    return bad


def scan_undefined_names(path):
    """公共入口：该文件中「被引用但从未定义」的全局名集合（供测试与外部脚本复用）"""
    return _undef_names(_read(path), path)


def collect_core_config_symbols(cc_path):
    """解析 core_config 的惰性机制，返回 (placeholders, lazy_ok)

    placeholders：**真占位符** —— 模块级绑定了哨兵值，之后只在 ensure/trigger 里刷新。属性查找
        会「成功」拿到 False/None/{}，__getattr__ 不触发 → from-import 永不更新 → 禁止。
        （HAS_FIN_MODELS / HAS_ADV_VIZ / MLTrainer / ALGORITHM_CATALOG …；注意 HAS_CLEAN_ADV /
        HAS_REPORT 在 try/except 里赋值、导入时即终值，**不算**占位符）
    lazy_ok：其余惰性符号（_LAZY_IMPORTS 表 + trigger 注册项）—— from-import 能正确解析到真身，
        只是会提前加载 → 允许，仅作汇总提示。
    """
    if not os.path.exists(cc_path):
        return set(), set()
    try:
        tree = ast.parse(_read(cc_path))
    except SyntaxError:
        return set(), set()

    def vals(node):
        return {e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)}

    def inject_name(target):
        """g['x'] = ... / globals()['x'] = ... → 'x'；否则 None"""
        if not (isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant)
                and isinstance(target.slice.value, str)):
            return None
        src = target.value
        hit = ((isinstance(src, ast.Name) and src.id == 'g')
               or (isinstance(src, ast.Call) and getattr(src.func, 'id', '') == 'globals'))
        return target.slice.value if hit else None

    def is_sentinel(node):
        return isinstance(node, (ast.Lambda, ast.Dict, ast.List, ast.Tuple)) or isinstance(node, ast.Constant)

    lazy_ok, injected_sentinel = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                name = inject_name(t)
                if name and is_sentinel(node.value):
                    injected_sentinel.add(name)
                if isinstance(t, ast.Name) and t.id == '_LAZY_IMPORTS' and isinstance(node.value, ast.Dict):
                    for val in node.value.values:
                        if isinstance(val, (ast.List, ast.Tuple)):
                            lazy_ok |= vals(val)
        elif isinstance(node, ast.For) and isinstance(node.iter, (ast.List, ast.Tuple)):
            triggers = [b for b in node.body if isinstance(b, ast.Assign) and b.targets
                        and isinstance(b.targets[0], ast.Subscript)
                        and getattr(b.targets[0].value, 'id', '') == '_LAZY_TRIGGERS']
            if triggers:
                lazy_ok |= vals(node.iter)
    global_declared = {n for node in ast.walk(tree) if isinstance(node, ast.Global) for n in node.names}
    sentinel_top = {t.id for node in tree.body if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                    for t in node.targets if isinstance(t, ast.Name)}
    return (injected_sentinel | (sentinel_top & global_declared)), lazy_ok


# ------------------------------------------------------------------ 规则
def rule_lines(rel, src, base, v):
    n = len(src.splitlines())
    b = (base.get('file_lines') or {}).get(rel)
    allowed = MAX_FILE_LINES if b is None else max(MAX_FILE_LINES, b)
    if n > allowed:
        tail = '（存量基线，禁止继续增长）' if b is not None else ''
        v.append(Violation('R1', 'error', rel, allowed + 1, f'{n} 行 > 允许 {allowed} 行{tail}'))
    elif b is not None and n < b:
        v.append(Violation('R1', 'info', rel, 1, f'已由 {b} 行降至 {n} 行，可 --freeze 收紧基线'))


def rule_swallow(rel, sites, base, v):
    allowed = (base.get('swallow_except') or {}).get(rel, 0)
    if len(sites) > allowed:
        head = ', '.join(f'L{x}' for x in sites[:10])
        v.append(Violation('R2', 'error', rel, sites[0] if sites else 0,
                           f'吞异常 {len(sites)} 处 > 基线 {allowed} 处（except 体只有 pass/continue）: {head}'))
    elif len(sites) < allowed:
        v.append(Violation('R2', 'info', rel, 1, f'吞异常已降至 {len(sites)} 处（基线 {allowed}），可 --freeze'))


def rule_base_exc(rel, src, v):
    if _is_test(rel):  # 测试文件里出现的同名字符串是规则执行者自身的断言，不算违规
        return
    for i, line in enumerate(src.split('\n'), 1):
        if NEEDLE_BASE_EXC in line:
            v.append(Violation('R3', 'error', rel, i, '捕获 BaseException 基类（禁止）'))


def rule_lazy_import(rel, tree, placeholders, lazy_ok, v):
    """只报占位符；其余惰性符号计入 eager 返回值（功能正常，仅抵消延迟加载收益）"""
    eager = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == 'core_config':
            for a in node.names:
                if a.name in placeholders:
                    v.append(Violation('R4', 'error', rel, node.lineno,
                                       f'from core_config import {a.name} — 占位符值永不更新，'
                                       f'请改用 has_*/get_* 取用 API'))
                elif a.name in lazy_ok:
                    eager += 1
    return eager


def rule_test_stub(rel, tree, v):
    if not _is_test(rel):
        return
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and re.match(r'^(fake_|stub_)', node.name):
            if node.args.kwarg is None:
                v.append(Violation('R6', 'error', rel, node.lineno,
                                   f'测试桩 {node.name}() 缺少 **kw —— 窄签名桩被 except 吞掉后会假通过'))


def rule_undefined(rel, names, base, v):
    allowed = set((base.get('undefined_names') or {}).get(rel, []))
    for name in sorted(set(names) - allowed):
        v.append(Violation('R7', 'error', rel, 0, f'引用未定义的全局名 {name!r}'))


# ------------------------------------------------------------ 主流程
def _scan(root, rules, baseline=None, placeholders=None):
    """核心扫描：(violations, metrics)。metrics 是与基线无关的现状，供 --freeze 复用。"""
    base = baseline or {}
    active = set(rules or ALL_RULES)
    if placeholders is None:
        placeholders, lazy_ok = collect_core_config_symbols(os.path.join(root, 'core_config.py'))
    else:
        placeholders, lazy_ok = set(placeholders), set()
    files = list(_iter_py(root))
    mods = module_index(files) if 'R5' in active else {}
    rev = {v: k for k, v in mods.items()}  # rel → 模块名
    graph, v, eager = {}, [], 0
    met = {'file_lines': {}, 'swallow_except': {}, 'undefined_names': {}, 'cycles': []}
    for p, rel in files:
        src = _read(p)
        try:
            tree = ast.parse(src)
        except SyntaxError as e:
            v.append(Violation('R0', 'error', rel, e.lineno or 0, f'无法解析：{e.msg}'))
            continue
        sites = _swallow_sites(tree)
        undef = set() if os.path.basename(rel) in R7_SKIP else _undef_names(src, p)
        met['file_lines'][rel] = len(src.splitlines())
        if sites:
            met['swallow_except'][rel] = len(sites)
        if undef:
            met['undefined_names'][rel] = sorted(undef)
        if 'R1' in active: rule_lines(rel, src, base, v)
        if 'R2' in active: rule_swallow(rel, sites, base, v)
        if 'R3' in active: rule_base_exc(rel, src, v)
        if 'R4' in active: eager += rule_lazy_import(rel, tree, placeholders, lazy_ok, v)
        if 'R6' in active: rule_test_stub(rel, tree, v)
        if 'R7' in active: rule_undefined(rel, undef, base, v)
        if 'R5' in active and rel in rev: graph[rev[rel]] = module_edges(tree, mods, rev[rel])
    if 'R4' in active and eager:
        v.append(Violation('R4', 'info', '(汇总)', 0,
                           f'{eager} 处 from core_config import 惰性符号：能解析到真身（非占位符），'
                           f'但会提前触发加载、抵消冷启动优化'))
    if 'R5' in active:
        met['cycles'] = find_cycles(graph)
        known = set(base.get('known_cycles') or [])
        for key in met['cycles']:
            hit = key in known
            v.append(Violation('R5', 'info' if hit else 'error', mods.get(key.split(' <-> ')[0], '?'), 1,
                               f'{"已知" if hit else "新增"}循环 import：{key}'))
    if 'R8' in active:
        findings, met8 = scan_egress(root, base, files)
        met.update(met8)
        v.extend(Violation(*f) for f in findings)
    return sorted(v, key=lambda x: (x.rule, x.path, x.line)), met


def check_tree(root, baseline=None, placeholders=None, rules=None):
    """对外 API（脚本与 pytest 共用）：规则内部已应用基线，返回违规列表"""
    return _scan(root, rules, baseline, placeholders)[0]


def load_baseline(path):
    try:
        with open(path, encoding='utf-8') as fh:
            return json.load(fh)
    except (ValueError, OSError):
        return {}


def cmd_freeze(root, base_path, force=False):
    old = load_baseline(base_path)
    met = _scan(root, ALL_RULES, {})[1]  # 空基线 → 未经豁免的现状
    new = {'generated_at': datetime.now().strftime('%Y-%m-%dT%H:%M:%S'), 'max_file_lines': MAX_FILE_LINES,
           'note': '存量豁免基线（棘轮）：只允许下降；放宽额度必须 --force 并让 diff 可见',
           'file_lines': {}, 'swallow_except': {}, 'known_cycles': [], 'undefined_names': {},
           'egress_hosts': {}}

    def keep(field, cur):
        """计数型棘轮（行数 / 吞异常）：已有条目取较小值，首次采纳记现状"""
        prev = old.get(field) or {}
        return {f: (n if (prev.get(f) is None or force) else min(prev[f], n)) for f, n in cur.items()}

    def keep_set(field, cur):
        """集合型棘轮（未定义名 / 未登记主机）：首次采纳取现状，之后取交集，只允许缩小"""
        prev = old.get(field) or {}
        return {f: sorted((set(v) | set(prev[f])) if (force or f not in prev) else (set(v) & set(prev[f])))
                for f, v in cur.items()}

    new['file_lines'] = keep('file_lines', {f: n for f, n in met['file_lines'].items() if n > MAX_FILE_LINES})
    new['swallow_except'] = keep('swallow_except', met['swallow_except'])
    new['undefined_names'] = keep_set('undefined_names', met['undefined_names'])
    new['egress_hosts'] = keep_set('egress_hosts', met.get('egress_hosts') or {})
    cur_cycles, prev_cycles = set(met['cycles']), set(old.get('known_cycles') or [])
    new['known_cycles'] = sorted(cur_cycles if (force or 'known_cycles' not in old) else (cur_cycles & prev_cycles))
    with open(base_path, 'w', encoding='utf-8') as fh:
        json.dump(new, fh, ensure_ascii=False, indent=2, sort_keys=True)
    gone = (set(old.get('file_lines') or {}) - set(new['file_lines'])) | (prev_cycles - cur_cycles)
    print(f'基线已写入 {os.path.relpath(base_path, root)}：超长文件 {len(new["file_lines"])} / 吞异常文件 '
          f'{len(new["swallow_except"])} / 已知环 {len(new["known_cycles"])} / 未定义名文件 '
          f'{len(new["undefined_names"])} / 未登记主机 {len(new["egress_hosts"])}')
    if gone:
        print(f'  已清理失效条目 {len(gone)} 个：{", ".join(sorted(gone)[:8])}')


def cmd_self_test():
    """守卫自己也要被验证：违规样本必须逐条命中、干净样本 0 误报（自证逻辑独立成文件）"""
    from arch_selftest import run_self_test   # 惰性导入：避免守卫自身触发行数门禁 R1
    return run_self_test()


def main(argv=None):
    ap = argparse.ArgumentParser(description='data.tool 架构守卫（零依赖）')
    ap.add_argument('--root', default=ROOT, help='被检查的仓库根')
    ap.add_argument('--baseline', default=BASELINE_PATH, help='基线 JSON 路径')
    ap.add_argument('--rule', default='', help='只跑指定规则，如 R2,R6')
    ap.add_argument('--json', action='store_true', help='机器可读输出')
    ap.add_argument('--strict', action='store_true', help='info 提示也视为失败')
    ap.add_argument('--freeze', action='store_true', help='用当前状态收紧基线（棘轮）')
    ap.add_argument('--force', action='store_true', help='配合 --freeze：允许放宽额度')
    ap.add_argument('--self-test', action='store_true', help='内置违规样本自证')
    args = ap.parse_args(argv)
    if args.self_test:
        return cmd_self_test()
    if args.freeze:
        cmd_freeze(args.root, args.baseline, force=args.force)
        return 0
    rules = [r.strip().upper() for r in args.rule.split(',') if r.strip()] or None
    res = check_tree(args.root, baseline=load_baseline(args.baseline), rules=rules)
    errs = [x for x in res if x.severity == 'error']
    infos = [x for x in res if x.severity == 'info']
    if args.json:
        print(json.dumps({'errors': [x._asdict() for x in errs], 'infos': [x._asdict() for x in infos],
                          'counts': {r: sum(1 for x in errs if x.rule == r) for r in ALL_RULES}},
                         ensure_ascii=False, indent=2))
        return 1 if errs else 0
    for x in errs + infos:
        print(f'[{"ERROR" if x.severity == "error" else "info "}] {x.rule}  {x.path}:{x.line}  {x.msg}')
    if errs:
        cnts = {r: sum(1 for x in errs if x.rule == r) for r in ALL_RULES}
        print('违规汇总：' + ', '.join(f'{k}={v}' for k, v in cnts.items() if v))
    else:
        print(f'架构守卫通过：0 条阻断级违规，{len(infos)} 条可收紧提示')
    return 1 if (errs or (args.strict and infos)) else 0


if __name__ == '__main__':
    sys.exit(main())
