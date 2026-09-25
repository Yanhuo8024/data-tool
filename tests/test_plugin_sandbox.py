# -*- coding: utf-8 -*-
"""plugin_sandbox.py 单测：AST 静态审查（白名单 import / 危险函数 / dunder 逃逸）"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plugin_sandbox import SandboxViolation, audit_plugin_source
import plugin_manager as pm


# ── 合法插件应通过 ──
def test_demo_plugins_pass():
    from pathlib import Path
    for f in ['demo_ridge_model.py', 'demo_scatter_chart.py']:
        src = Path('plugins', f).read_text(encoding='utf-8')
        audit_plugin_source(src)  # 不抛异常即通过


def test_allowed_imports_pass():
    audit_plugin_source('import pandas as pd\nimport numpy as np\n'
                        'from sklearn.linear_model import Ridge\n'
                        'import matplotlib.pyplot as plt\nimport json\nimport math')


def test_own_module_pass():
    audit_plugin_source('x = 1\ndef f(a):\n    return a + 1')


# ── 危险导入应被拒绝 ──
@pytest.mark.parametrize('src,kw', [
    ('import os', 'os'),
    ('import subprocess', 'subprocess'),
    ('import socket', 'socket'),
    ('import pickle', 'pickle'),
    ('import requests', 'requests'),
    ('from os import system', 'os'),
    ('from pathlib import Path', 'pathlib'),
    ('import threading', 'threading'),
    ('import sqlite3', 'sqlite3'),
    ('from urllib.request import urlopen', 'urllib'),
    ('import importlib', 'importlib'),
    ('import pandas as pd', None),  # 白名单应通过（反面校验）
])
def test_import_guard(src, kw):
    if kw is None:
        audit_plugin_source(src)  # 白名单通过
    else:
        with pytest.raises(SandboxViolation):
            audit_plugin_source(src)


# ── 危险函数调用应被拒绝 ──
@pytest.mark.parametrize('src,kw', [
    ('eval("1+1")', 'eval'),
    ('exec("x=1")', 'exec'),
    ('compile("1", "s", "eval")', 'compile'),
    ('open("C:/x")', 'open'),
    ('__import__("os")', '__import__'),
    ('getattr(obj, "x")', 'getattr'),
    ('globals()', 'globals'),
    ('vars()', 'vars'),
    ('dir()', 'dir'),
    ('type(obj)', 'type'),
    ('breakpoint()', 'breakpoint'),
])
def test_dangerous_call_guard(src, kw):
    with pytest.raises(SandboxViolation):
        audit_plugin_source(src)


# ── dunder 逃逸应被拒绝 ──
@pytest.mark.parametrize('src', [
    ('(1).__class__.__mro__',),
    ('x = [].__class__.__bases__',),
    ('obj.__subclasses__()',),
    ('x.__globals__',),
    ('__builtins__',),
])
def test_dunder_escape_guard(src):
    with pytest.raises(SandboxViolation):
        audit_plugin_source(src)


# ── 语法错误 / 空源码 ──
def test_syntax_error():
    with pytest.raises(SandboxViolation):
        audit_plugin_source('def f(:\n    pass')


def test_empty_source():
    with pytest.raises(SandboxViolation):
        audit_plugin_source('   ')


# ── 与 plugin_manager 集成：安装恶意插件被拒且回滚 ──
def test_install_evil_plugin_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, 'PLUGIN_DIR', tmp_path)
    with pytest.raises(pm.PluginError):
        pm.install_plugin('import os\nos.remove("C:/x")\n'
                          'PLUGIN_META={"name":"x","type":"model"}', 'evil.py')
    assert not (tmp_path / 'evil.py').exists()  # 已回滚删除


def test_install_legit_plugin(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, 'PLUGIN_DIR', tmp_path)
    src = ('PLUGIN_META={"name":"加和","type":"model","version":"1.0"}\n'
           'import pandas as pd\n'
           'def run(df, params):\n'
           '    return {"sum": float(df.sum().sum())}\n')
    name = pm.install_plugin(src, 'sum_plugin.py')
    assert (tmp_path / 'sum_plugin.py').exists()
    # 运行验证
    import pandas as pd
    r = pm.run_plugin(name, pd.DataFrame({'a': [1, 2], 'b': [3, 4]}), {})
    assert r['result']['sum'] == 10.0
