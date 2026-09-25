#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""惰性符号取用正确性回归测试（2026-09-17）。

背景（同源事故三连）：
1. `core_config` 的占位符（HAS_* / MLTrainer / ALGORITHM_CATALOG ...）写进了模块 __dict__，
   而 PEP 562 的模块级 __getattr__ 只在「查找失败」时触发 —— 占位符存在 ⇒ 触发器永不执行。
   于是 `from core_config import HAS_ML_ENHANCED` 拿到的是导入瞬间的 False，且永不更新。
2. `ml_routes` 把顶层 import 删了却没在函数内补 —— 裸用 HAS_ML_ENHANCED → NameError → 500；
   `/api/ml/models` 命中占位符 lambda → 恒返回 []（静默错值）。
3. `finance_routes` 顶层快照 HAS_FIN_MODELS/HAS_ADV_VIZ=False → 9 个端点永久「模块未加载」；
   这些端点此前从未真正执行，掩盖了 `finviz_advanced` 的 `df[typical]` 拼写错误。

本测试锁定：
- 取用 API 的语义（触发 + 返回最新值）
- 被修端点的真实行为（不再 500 / 不再空列表）
- 源码层面禁止「再出现顶层快照」（防止回归）
- 项目范围内无「未定义全局引用」（同类缺陷的通用护栏）

隔离约定：用「独立 mini Flask 应用 + 单独注册某路由模块」，不依赖全局 app 的
分阶段注册状态（那条路径本身存在竞态），因此本文件快速且确定性。
"""
import importlib.util
import os
import re
import sys

import pytest
from flask import Flask

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import core_config as CC  # noqa: E402


def _read(rel_path):
    with open(os.path.join(ROOT, rel_path), encoding='utf-8') as f:
        return f.read()


class _DummyLimiter:
    """最小限流器替身：路由模块用 @limiter.limit(...) / limiter.exempt 装饰视图。"""

    def limit(self, *args, **kwargs):
        def _deco(fn):
            return fn
        return _deco

    def exempt(self, fn=None):
        if fn is None:
            return lambda f: f
        return fn


def _mini_app(module_name, register_name='register_routes'):
    """在独立 Flask 应用上注册单个路由模块 —— 避开全局注册竞态。

    并摘掉蓝图的 before_request（local_guard 依赖真实 app 的 SQLAlchemy 初始化，
    与本测试要验证的「惰性符号取用」无关）。
    """
    app = Flask(f'test_{module_name}')
    app.config['PROPAGATE_EXCEPTIONS'] = False
    app.config['TESTING'] = True
    mod = importlib.import_module(f'routes.{module_name}')
    getattr(mod, register_name)(app, _DummyLimiter())
    app.before_request_funcs.clear()
    return app


# --------------------------------------------------------------------------
# 1. 取用 API 语义
# --------------------------------------------------------------------------
def test_accessors_return_correct_types():
    assert isinstance(CC.has_fin_models(), bool)
    assert isinstance(CC.has_adv_viz(), bool)
    assert isinstance(CC.has_ml_enhanced(), bool)
    assert isinstance(CC.get_algorithm_catalog(), dict)
    assert isinstance(CC.ml_model_list(), list)


def test_accessor_triggers_lazy_load():
    """取用 API 必须先触发懒加载再返回，否则会拿到占位值。"""
    assert CC.has_ml_enhanced() is True, 'ml_enhanced 可导入，取用 API 应触发并返回 True'
    assert CC.get_algorithm_catalog(), '算法目录不应为空 —— 空说明命中了占位符 {}'
    assert CC.get_ml_trainer() is not None, 'MLTrainer 不应为 None —— 说明未触发懒加载'


def test_ml_model_list_not_empty():
    """历史缺陷：命中 `get_ml_model_list = lambda: []` 占位符 → /api/ml/models 恒返回 []。"""
    models = CC.ml_model_list()
    assert models, '可用模型列表为空说明取到的是占位符 lambda'
    assert 'regression' in models


def test_placeholder_still_guards_cold_start():
    """占位符本身必须保留 —— 它是 system_routes/finance_routes 顶层导入不拖垮冷启动的保障。
    回归护栏：有人「顺手删掉占位符」会让 sklearn/plotly 在启动期被加载。"""
    src = _read('core_config.py')
    for name in ('MLTrainer = None', 'HAS_ML_ENHANCED = False', 'ALGORITHM_CATALOG = {}'):
        assert name in src, f'占位符 {name!r} 被移除，将导致启动期加载重依赖'


# --------------------------------------------------------------------------
# 2. 被修端点的真实行为
# --------------------------------------------------------------------------
def test_ml_catalog_endpoint_returns_200_and_catalog():
    """历史缺陷：NameError: name 'HAS_ML_ENHANCED' is not defined → 500。"""
    app = _mini_app('ml_routes')
    r = app.test_client().get('/api/ml/catalog')
    assert r.status_code == 200, f'/api/ml/catalog 应 200，实际 {r.status_code}'
    body = r.get_json()
    assert body.get('catalog'), '算法目录不应为空'


def test_ml_models_endpoint_not_empty():
    """历史缺陷：返回 [] —— ML 页模型下拉为空。"""
    app = _mini_app('ml_routes')
    r = app.test_client().get('/api/ml/models')
    assert r.status_code == 200
    assert r.get_json(), '/api/ml/models 不应返回空列表'


def test_ml_feature_engineering_no_nameerror():
    """历史缺陷：NameError: name 'HAS_ML_ENHANCED' / 'auto_feature_engineering' → 500。"""
    app = _mini_app('ml_routes')
    r = app.test_client().post('/api/ml/feature-engineering', json={})
    assert r.status_code != 500, f'不应 500，实际 {r.status_code}'


def test_ml_evaluation_panel_no_nameerror():
    """历史缺陷：NameError: name 'HAS_ML_ENHANCED' / 'generate_evaluation_panel' → 500。"""
    app = _mini_app('ml_routes')
    r = app.test_client().post('/api/ml/evaluation-panel', json={})
    assert r.status_code != 500, f'不应 500，实际 {r.status_code}'


# --------------------------------------------------------------------------
# 3. 源码层面禁止「再出现顶层快照」——本次事故的根因形态
# --------------------------------------------------------------------------
@pytest.mark.parametrize('rel_path,banned', [
    ('routes/finance_routes.py', ['HAS_FIN_MODELS', 'HAS_ADV_VIZ']),
    ('routes/ml_routes.py', ['HAS_ML_ENHANCED', 'ALGORITHM_CATALOG']),
    ('routes/core_routes.py', ['HAS_ML_ENHANCED', 'HAS_FIN_MODELS']),
    ('routes/system_routes.py', ['MLTrainer']),
])
def test_no_bare_snapshot_of_lazy_symbols(rel_path, banned):
    """这些名字出现在这些文件里几乎一定是错的：
    要么是未导入（NameError），要么是顶层快照（永久 False/None）。"""
    src = _read(rel_path)
    offenders = []
    for name in banned:
        for i, line in enumerate(src.splitlines(), 1):
            if name not in line:
                continue
            stripped = line.strip()
            # 允许：正确取用 API（has_*/get_*/_Trainer）、注释、以及本模块自己定义的名字
            if re.search(rf'\b(has_|get_|ml_model_list)', line):
                continue
            if stripped.startswith('#'):
                continue
            offenders.append(f'{rel_path}:{i}: {stripped}')
    assert not offenders, '发现惰性符号的裸用/快照：\n' + '\n'.join(offenders)


def test_finance_routes_uses_accessor_for_guards():
    src = _read('routes/finance_routes.py')
    assert 'has_fin_models()' in src
    assert 'has_adv_viz()' in src


def test_analytics_and_data_routes_import_safe_path_or_resp():
    """历史缺陷：两文件调用 safe_path_or_resp 但从未导入 → NameError 500。"""
    for rel in ('routes/analytics_routes.py', 'routes/data_routes.py'):
        src = _read(rel)
        if 'safe_path_or_resp(' not in src:
            continue
        assert re.search(r'import\s*\(?[^)]*safe_path_or_resp', src, re.S), \
            f'{rel} 使用了 safe_path_or_resp 但没有导入'


def test_finviz_typical_is_quoted():
    """历史缺陷：`df[typical]` 少了引号 → NameError: name 'typical' is not defined。
    该缺陷长期被 finance_routes 的「模块未加载」永久短路所掩盖。"""
    src = _read('finviz_advanced.py')
    assert "df['typical']" in src
    assert not re.search(r"df\[\s*typical\s*\]", src), '存在未加引号的 df[typical]'


# --------------------------------------------------------------------------
# 4. 通用护栏：项目内无「未定义全局引用」
# --------------------------------------------------------------------------
# 原实现依赖 output/_scan_undefined_names.py，该脚本已从仓库丢失 → 本用例长期恒 SKIP。
# 2026-09-21 起该职责由架构守卫的 R7 承担（tools/arch_guard.py，零依赖 symtable），
# 同一实现另有 tests/test_arch_guard.py 与 `--self-test` 双重覆盖。
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import arch_guard as _guard  # noqa: E402


def test_no_undefined_global_references_in_routes():
    """复用架构守卫的 symtable 检查（R7），覆盖所有路由模块。
    globals() 动态注入会误报，故只断言 routes/ 目录为空命中。"""
    hits = {}
    routes_dir = os.path.join(ROOT, 'routes')
    for fn in sorted(os.listdir(routes_dir)):
        if fn.endswith('.py'):
            path = os.path.join(routes_dir, fn)
            bad = sorted(_guard.scan_undefined_names(path))
            if bad:
                hits[fn] = bad
    assert not hits, f'routes/ 存在未定义全局引用（潜在 NameError）：{hits}'
