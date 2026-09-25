#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工具风险语义（副作用闸门）测试

对标 DeepSeek Harness 的 permission 插件层：dsh 把权限做成独立可换插件；
本仓库工具总量小且单仓，用 egress_policy 内的风险表 + ToolRegistry 单一收口实现
「数据出境」与「工具副作用」两向闸门。

设计要点（改动前务必读）：
  · 默认**放行**是刻意的 —— 本地自用工具，默认拦截会打断现有工作流；
    本层提供能力（可查/可拦/可登记），不是默认行为变更。
  · 未登记工具按 readonly 处理 → 新增工具零回归。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import egress_policy as ep  # noqa: E402

_SIDE_EFFECT_TOOLS = ('clean_data', 'feature_eng', 'ml_train',
                      'generate_chart', 'run_analysis_api', 'web_search', 'db_query')
_READONLY_TOOLS = ('describe', 'head', 'columns', 'correlation', 'trend',
                   'hypothesis_test', 'list_files', 'safe_calc')


@pytest.fixture
def side_effects_on(monkeypatch):
    """复位为默认（放行），避免用例间相互污染"""
    monkeypatch.delenv('ALLOW_TOOL_SIDE_EFFECTS', raising=False)
    yield
    monkeypatch.delenv('ALLOW_TOOL_SIDE_EFFECTS', raising=False)


@pytest.fixture
def side_effects_off(monkeypatch):
    monkeypatch.setenv('ALLOW_TOOL_SIDE_EFFECTS', '0')
    yield
    monkeypatch.delenv('ALLOW_TOOL_SIDE_EFFECTS', raising=False)


# --------------------------- 风险分级 ---------------------------
def test_risk_is_三档且取值合法():
    """每项取值必须落在三档内 —— 防止手写登记时打错字符串被静默当 readonly"""
    assert set(ep.TOOL_RISK.values()) <= {'readonly', 'write', 'external'}


def test_写盘与出站工具均已登记(side_effects_on):
    for name in _SIDE_EFFECT_TOOLS:
        assert ep.tool_risk(name) in ('write', 'external'), f'{name} 未登记副作用等级'


def test_只读工具不登记且永远放行(side_effects_on):
    """只读工具不依赖环境变量：开关都不能拦它"""
    for name in _READONLY_TOOLS:
        assert ep.tool_risk(name) == 'readonly'
        assert ep.tool_side_effect_allowed(name) is True


def test_只读工具在关闭态仍放行(side_effects_off):
    for name in _READONLY_TOOLS:
        assert ep.tool_side_effect_allowed(name) is True, f'{name} 是只读工具，不应被副作用开关拦截'


def test_未登记工具按只读处理(side_effects_off):
    """新增工具未登记 → 放行（零回归）；这是刻意的兜底语义"""
    assert ep.tool_risk('__never_registered__') == 'readonly'
    assert ep.tool_side_effect_allowed('__never_registered__') is True


# --------------------------- 开关行为 ---------------------------
def test_默认放行(side_effects_on):
    for name in _SIDE_EFFECT_TOOLS:
        assert ep.tool_side_effect_allowed(name) is True, f'默认应放行 {name}'


def test_关闭后拦截写盘与出站(side_effects_off):
    for name in _SIDE_EFFECT_TOOLS:
        assert ep.tool_side_effect_allowed(name) is False, f'关闭后应拦截 {name}'


def test_开关取值变体(side_effects_on, monkeypatch):
    for raw in ('0', 'false', 'no', 'off', 'FALSE'):
        monkeypatch.setenv('ALLOW_TOOL_SIDE_EFFECTS', raw)
        assert ep.tool_side_effect_allowed('clean_data') is False, f'{raw!r} 应视为关闭'
    for raw in ('1', 'true', 'yes', 'on'):
        monkeypatch.setenv('ALLOW_TOOL_SIDE_EFFECTS', raw)
        assert ep.tool_side_effect_allowed('clean_data') is True, f'{raw!r} 应视为开启'


def test_拦截文案含原因与替代路径(side_effects_off):
    """给模型的反馈必须**可读且可行动** —— 只说"不可用"会诱发模型重试同一工具"""
    msg = ep.tool_side_effect_message('clean_data')
    assert 'clean_data' in msg and 'write' in msg
    assert 'ALLOW_TOOL_SIDE_EFFECTS' in msg
    assert 'describe' in msg, '应给出只读替代工具，否则模型会空转'
    msg_ext = ep.tool_side_effect_message('web_search')
    assert 'external' in msg_ext


def test_风险快照结构(side_effects_off):
    snap = ep.tool_risk_snapshot()
    assert snap['ok'] is True
    assert set(snap['levels']) == {'readonly', 'write', 'external'}
    assert 'clean_data' in snap['levels']['write']
    assert 'web_search' in snap['levels']['external']
    assert snap['side_effects_allowed'] is False


def test_快照开关状态不得用占位工具名探测(side_effects_off):
    """回归守卫：曾用 `tool_side_effect_allowed('__probe__')` 探测开关，
    但未登记工具恒为 readonly → 恒返回 True → 快照说谎（永远显示"放行"）。
    必须走 `side_effects_enabled()` 这个与工具名无关的入口。"""
    assert ep.side_effects_enabled() is False, '关闭态下全局开关必须读到关闭'
    assert ep.tool_risk_snapshot()['side_effects_allowed'] is False
    with_placeholder = ep.tool_side_effect_allowed('__probe__')
    assert with_placeholder is True, '占位名恒放行 —— 正因如此才不能拿它探测开关'


# --------------------------- 与 ToolRegistry 的单一收口 ---------------------------
def test_registry_关闭后既不列出也不可执行(side_effects_off):
    import ai_agent
    names = [t['name'] for t in ai_agent.registry.list_tools()]
    assert 'clean_data' not in names, '关闭后不应再列出该工具'
    assert 'describe' in names, '只读工具必须保留'
    info = ai_agent.registry.get_tool('clean_data')
    assert info is not None and 'ALLOW_TOOL_SIDE_EFFECTS' in info['func'](file='x.csv')


def test_registry_只读工具在关闭态可正常取到真身(side_effects_off):
    import ai_agent
    info = ai_agent.registry.get_tool('describe')
    assert info is not None and info['description'], '只读工具应返回真身而非桩'


def test_registry_默认态全部工具可见(side_effects_on):
    """默认放行时，工具总数不得因本次改动而减少（防"闸门收得过紧"）"""
    import ai_agent
    assert len(ai_agent.registry.list_tools()) == 32


def test_两向闸门正交_出境关但副作用开(side_effects_on):
    """`web_search` 同时受两向管制：出境关 → 拦。执行反馈必须是**出境**那条
    （`_tool_allowed` 判定在前），以此验证收口顺序正确、两向不互相覆盖。"""
    import ai_agent
    try:
        ep.set_flag('web_search', False)
        ep.reset_cache()
        info = ai_agent.registry.get_tool('web_search')
        assert '数据出境' in info['func'](query='x')
    finally:
        ep.set_flag('web_search', True)
        ep.reset_cache()
