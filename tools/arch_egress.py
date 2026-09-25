#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/arch_egress.py — 出站登记守卫（R8）的判定逻辑

规则：应用侧一旦出现对外网络调用（requests.* / urlopen / http.client / socket.create_connection /
aiohttp / httpx），其 URL 主机名必须在 design/DATA_EGRESS.md 中**登记**；未登记即报错。
目的：防止"代码加了出站、清单没更新"的漂移——清单既是白名单，也是对外可出示的合规材料。

判定口径（刻意收紧，避免"狼来了"）：
- 只扫应用代码（tests/ 与 tools/ 不计，测试里的假域名不是真实出站）；
- 主机名只取 AST 里的**字符串常量**，注释与文档中的链接不算；
- 回环地址与文档保留域内置放行（127.0.0.1 / localhost / 0.0.0.0 / example.com / *.invalid）；
- 主机名为**动态值**（如从环境变量拼出的端点）无法登记，单独给 info 提示，不阻断；
- 既有的未登记主机走基线棘轮（当前为 0 → 新增即拦）。
"""

import ast
import os
import re

CALL_RE = re.compile(
    r'\brequests\.(get|post|put|delete|patch|request|Session)\b'
    r'|\burlopen\b|\burllib\.request\b|\bhttp\.client\b|\bHTTPConnection\b'
    r'|\bsocket\.(socket|create_connection)\b|\baiohttp\b|\bhttpx\b')
HOST_RE = re.compile(r'https?://([A-Za-z0-9._\-]+)(?::\d+)?')
TOKEN_RE = re.compile(r'(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}')
# 回环 / 文档保留域 / JSON Schema 命名空间：非真实出境，免登记
BENIGN = {'127.0.0.1', 'localhost', '0.0.0.0', 'example.com', 'example.invalid', 'schemas'}
DOC_REL = 'design/DATA_EGRESS.md'
_EXCLUDED_TOP = ('tests', 'tools')


def _read(path):
    with open(path, encoding='utf-8', errors='ignore') as fh:
        return fh.read()


def _host_sites(src):
    """[(host, lineno)]：只取字符串常量中的 URL 主机名（注释/文档串不参与）"""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    out = []
    for node in ast.walk(tree):
        val = getattr(node, 'value', None)
        if isinstance(node, ast.Constant) and isinstance(val, str) and '://' in val:
            out.extend((h, node.lineno) for h in HOST_RE.findall(val))
    return out


def registered_hosts(root):
    """登记表：DATA_EGRESS.md 中出现的全部主机名（文档即白名单）+ 内置放行项"""
    doc = os.path.join(root, *DOC_REL.split('/'))
    text = _read(doc) if os.path.exists(doc) else ''
    return set(TOKEN_RE.findall(text)) | BENIGN, os.path.exists(doc)


def scan_egress(root, base, files):
    """扫出站登记情况 → (findings, metrics)

    findings：与 arch_guard 的 Violation 同序的 5 元组 (rule, severity, path, line, msg)
    metrics ：{'egress_hosts': {rel: [未登记主机]}}，供 --freeze 写成棘轮基线
    """
    registered, doc_exists = registered_hosts(root)
    known = {rel: set(hs) for rel, hs in (base.get('egress_hosts') or {}).items()}
    hits, metrics, info = [], {}, []
    for path, rel in files:
        if rel.split('/')[0] in _EXCLUDED_TOP:
            continue
        src = _read(path)
        if not CALL_RE.search(src):
            continue
        sites = _host_sites(src)
        unreg = sorted({h for h, _ in sites if h not in registered})
        if unreg:
            metrics.setdefault('egress_hosts', {})[rel] = unreg
        if not doc_exists:
            info.append(('R8', 'info', rel, 1, f'未找到 {DOC_REL}，出站登记无法校验'))
        elif not sites:
            info.append(('R8', 'info', rel, 1,
                         '存在出站调用但主机名非字面量（动态端点），清单无法覆盖，请人工确认'))
        for host, line in sites:
            if host in registered or host in known.get(rel, ()):
                continue
            hits.append(('R8', 'error', rel, line,
                         f'出站主机未登记：{host} —— 请写入 {DOC_REL} §8 或改用已登记端点'))
    return hits + info, metrics
