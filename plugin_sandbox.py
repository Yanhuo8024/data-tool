#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
plugin_sandbox.py — 插件沙箱安全审查（AST 静态分析）

策略：白名单 + 黑名单双层
1. import 白名单：仅允许数据分析常用库（pandas/numpy/matplotlib/sklearn 等）与纯内置库
2. 危险函数黑名单：eval/exec/compile/open/input/__import__/getattr 链式逃逸等
3. dunder 属性访问禁止：防 __class__.__mro__ / __subclasses__ 逃逸

注意：AST 静态审查是**第一道防线**（上传时 + 加载时双重校验），运行时另有超时与
输出大小限制兜底；无法完全隔离恶意代码（如纯字节码），残余风险在文档中明示。
"""
import ast

# ── import 白名单（前缀匹配；含子模块如 matplotlib.pyplot / sklearn.linear_model）──
ALLOWED_IMPORT_PREFIXES = (
    'pandas', 'numpy', 'matplotlib', 'seaborn', 'plotly', 'sklearn', 'scipy',
    'statmodels', 'statsmodels', 'json', 'base64', 'io', 'math', 're',
    'random', 'datetime', 'collections', 'statistics', 'functools',
    'itertools', 'typing', 'decimal', 'string', 'copy', 'time', 'calendar',
    'heapq', 'bisect', 'textwrap', 'unicodedata', 'abc', 'enum', 'dataclasses',
)

# ── import 黑名单（即使白名单前缀未命中，明确拒绝的危险模块）──
FORBIDDEN_IMPORT_PREFIXES = (
    'os', 'sys', 'subprocess', 'socket', 'shutil', 'ctypes', 'importlib',
    'pathlib', 'requests', 'urllib', 'http', 'ftplib', 'smtplib',
    'multiprocessing', 'threading', 'pickle', 'marshal', 'tempfile',
    'builtins', 'pty', 'signal', 'mmap', 'winreg', '_winapi', 'pip',
    'setuptools', 'pkg_resources', 'zipfile', 'tarfile', 'gzip', 'bz2',
    'lzma', 'sqlite3', 'shelve', 'dbm', 'crypt', 'hashlib', 'hmac', 'ssl',
    'asyncio', 'concurrent', 'queue', 'platform', 'getpass', 'inspect',
    'traceback', 'warnings', 'logging', 'configparser', 'xml', 'html',
    'webbrowser', 'cgi', 'cgitb', 'wsgiref', 'email', 'telnetlib', 'imaplib',
    'poplib', 'nntplib', 'xmlrpc', 'dbm', 'glob', 'fnmatch', 'argparse',
    'optparse', 'getopt', 'fileinput', 'linecache', 'code', 'codeop',
    'codecs', 'encodings', 'site', 'sitecustomize', 'sysconfig', 'dis',
    'opcode', 'types', 'gc', 'faulthandler', 'resource', 'pdb', 'bdb',
)

# ── 危险函数调用黑名单（eval 类 / 动态执行 / 文件与导入逃逸）──
FORBIDDEN_FUNCS = {
    'eval', 'exec', 'compile', 'open', 'input', '__import__',
    'getattr', 'setattr', 'delattr', 'globals', 'locals', 'vars',
    'breakpoint', 'memoryview', '__build_class__', 'dir', 'id',
    'object', 'type', 'super', 'classmethod', 'staticmethod',
    'property', 'map', 'filter', 'reduce', 'frozenset',
}

# ── 拒绝的 dunder 属性（完整双下划线包裹）──
def _is_dunder(name: str) -> bool:
    return name.startswith('__') and name.endswith('__') and len(name) > 4


class SandboxViolation(Exception):
    """插件代码违反沙箱规则"""


def audit_plugin_source(source: str):
    """审查插件源码，违规抛 SandboxViolation；通过返回 None"""
    if not isinstance(source, str) or not source.strip():
        raise SandboxViolation('插件源码为空')
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        raise SandboxViolation(f'插件语法错误: {e}')

    for node in ast.walk(tree):
        # ── import / from x import y ──
        if isinstance(node, ast.Import):
            for alias in node.names:
                _check_import(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or '').strip()
            if mod == '__future__':
                continue
            _check_import(mod)
            for alias in node.names:
                if alias.name == '*':
                    raise SandboxViolation(f'禁止通配导入 from {mod} import *')
        # ── 危险函数调用（含链式：os.system → Attribute 逐层校验）──
        elif isinstance(node, ast.Call):
            name = _call_name(node.func)
            if name and name in FORBIDDEN_FUNCS:
                raise SandboxViolation(f'禁止调用危险函数/方法: {name}')
            # __import__('os') 字符串形式
            if isinstance(node.func, ast.Name) and node.func.id == '__import__':
                raise SandboxViolation('禁止动态导入 __import__')
        # ── dunder 属性访问逃逸（__class__ / __mro__ / __subclasses__ 等）──
        elif isinstance(node, ast.Attribute):
            if _is_dunder(node.attr):
                raise SandboxViolation(f'禁止 dunder 属性访问: {node.attr}')
        # ── Name 直接引用 __builtins__ ──
        elif isinstance(node, ast.Name):
            if node.id in ('__builtins__', '__import__'):
                raise SandboxViolation(f'禁止引用内置逃逸: {node.id}')


def _check_import(modname: str):
    """校验单个 import 模块名（取根模块）"""
    root = modname.split('.')[0]
    if root in FORBIDDEN_IMPORT_PREFIXES or any(root.startswith(p) for p in FORBIDDEN_IMPORT_PREFIXES):
        raise SandboxViolation(f'禁止导入模块: {modname}')
    if not any(root == p or root.startswith(p + '.') for p in ALLOWED_IMPORT_PREFIXES):
        raise SandboxViolation(f'未授权导入模块: {modname}（白名单: 数据分析常用库 + 纯内置库）')


def _call_name(func) -> str:
    """提取调用目标名（Name → id；Attribute → 末尾属性名）"""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def audit_plugin_file(path) -> None:
    """审查插件文件（供加载时校验）"""
    with open(path, 'r', encoding='utf-8') as f:
        audit_plugin_source(f.read())
