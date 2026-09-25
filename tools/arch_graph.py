#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/arch_graph.py — 模块级 import 图与环路检测（供 tools/arch_guard.py 使用）

单独成文件的原因：图构建（模块索引 / 边收敛 / Tarjan SCC）与「规则判定」是两件事，
拆开后两边都远低于 400 行门禁，也便于单独测试。

约定：只统计**模块级** import —— 函数体内的 import 是本项目有意的延迟导入（全仓 665 处），
若一并计入，依赖图会被噪声淹没，环路判定失去意义。
"""
import ast
import os

__all__ = ['module_index', 'resolve', 'module_edges', 'find_cycles']


def module_index(files):
    """files: [(绝对路径, 相对路径)] → {模块名: 相对路径}；tests/ 不参与依赖图"""
    mods = {}
    for p, rel in files:
        if rel.startswith('tests/'):
            continue
        key = (os.path.dirname(rel).replace('/', '.') if os.path.basename(p) == '__init__.py'
               else rel[:-3].replace('/', '.'))
        mods[key] = rel
    return mods


def resolve(name, mods):
    """把 'routes.finance_routes.helper' 解析到已知的最长模块前缀；解析不到返回 None"""
    parts = (name or '').split('.')
    for i in range(len(parts), 0, -1):
        cand = '.'.join(parts[:i])
        if cand in mods:
            return cand
    return None


def module_edges(tree, mods, self_name):
    """某模块的模块级出边集合（含 if/try/with 内的 import，不含函数体内）"""
    edges, stack = set(), list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Import):
            edges |= {t for t in (resolve(a.name, mods) for a in node.names) if t and t != self_name}
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # 相对导入：'from .x import y' / 'from . import x'
                base = self_name.rsplit('.', node.level)[0] if '.' in self_name else self_name
                target = f'{base}.{node.module}' if node.module else base
            else:
                target = node.module or ''
            hit = resolve(target, mods)
            if hit and hit != self_name:
                edges.add(hit)
        stack.extend(c for c in ast.iter_child_nodes(node) if isinstance(c, ast.stmt))
    return edges


def find_cycles(graph):
    """Tarjan 强连通分量 → 每个环返回一个稳定键（' <-> ' 连接排序后的成员，含自环）

    键必须稳定：baseline 里记录的是这些字符串，任何抖动都会造成假违规。
    """
    idx, low, stack, on, out, cnt = {}, {}, [], set(), [], [0]

    def strong(v):
        idx[v] = low[v] = cnt[0]
        cnt[0] += 1
        stack.append(v)
        on.add(v)
        for w in graph.get(v, ()):
            if w not in idx:
                strong(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], idx[w])
        if low[v] == idx[v]:
            comp = []
            while True:
                w = stack.pop()
                on.discard(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1 or v in graph.get(v, ()):
                out.append(' <-> '.join(sorted(comp)))

    for node in list(graph):
        if node not in idx:
            strong(node)
    return sorted(out)
