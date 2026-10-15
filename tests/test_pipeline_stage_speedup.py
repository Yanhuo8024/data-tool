#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三阶段分析推理加速（2026-09-18）专项回归。

覆盖四项提速改造的契约：
  ① no_plan    —— 管线阶段跳过 legacy planner + LangGraph planner 节点
  ② tool_names —— 按阶段裁剪工具集（tools_prompt 与 openai tools 同步收窄）
  ③ slim_prompt—— 跳过技能正文/知识点库/领域框架重注入
  ④ 并行工具   —— 同轮多个工具调用并发执行，结果顺序不变
  ⑤ 节流分桶   —— 节流粒度细化到 (provider, model)，三阶段三模型不再互相串行化
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai_agent          # noqa: E402
import agent_pipeline    # noqa: E402
import agent_graph_nodes as _gn  # noqa: E402
import llm_client        # noqa: E402


def _cfg(**kw):
    base = {'llm_config': {}, 'file_name': None, 'session_id': None, 'runtime_token': '',
            'max_iterations': 5, 'use_native': False, 'openai_tools': None, 'trace_id': None,
            'plan': [], 'fail_limit': 2, 'min_success_calls': 1, 'step_guard_max': 2,
            'user_message': 'x', 'deadline_ts': None}
    base.update(kw)
    return base


def _state(calls, cfg=None, iteration=1):
    return {'cfg': cfg or _cfg(), 'iteration': iteration,
            'messages': [{'role': 'user', 'content': 'x'}],
            'calls_made': [], 'failed_tools': {}, 'usage_total': {},
            'pending_calls': calls, 'last_content': '', 'facts': [], 'missing': []}


# ==================== ② 工具白名单 ====================

def test_tools_prompt_shrinks_with_whitelist():
    """tools_prompt 支持按白名单收窄；未知工具名被忽略，顺序按白名单给出。"""
    full = ai_agent.registry.get_tools_prompt()
    names = ['web_search', 'safe_calc', '不存在的工具']
    part = ai_agent.registry.get_tools_prompt(names)
    assert len(part) < len(full)
    assert '`web_search`' in part and '`safe_calc`' in part
    assert '`ml_train`' not in part
    assert '不存在' not in part
    assert part.count('\n') == 2     # 表头 + 2 个有效工具


def test_openai_tools_respects_whitelist():
    """原生 function calling 的 tools payload 同样按白名单裁剪，且不含未在白名单的工具。"""
    tools = ai_agent._build_openai_tools(['web_search', 'safe_calc'])
    got = [t['function']['name'] for t in tools]
    assert got == ['web_search', 'safe_calc']
    assert len(ai_agent._build_openai_tools()) > 2


def test_stage_whitelists_are_subset_of_registry():
    """管线阶段白名单里的工具必须真实存在（拼错=阶段用不到工具，属静默失效）。"""
    for stage, names in agent_pipeline._STAGE_TOOL_WHITELIST.items():
        assert names, stage
        for n in names:
            assert ai_agent.registry.get_tool(n), f'{stage} 白名单里的 {n} 不在注册表'


# ==================== ① no_plan ====================

def test_no_plan_skips_both_planners(monkeypatch):
    """no_plan=True → legacy planner 与 LangGraph planner 节点都不发 LLM 调用。"""
    calls = []

    def fake_chat(messages, config=None, **kw):
        calls.append(messages)
        return {'content': '{"steps": ["a", "b"]}', 'usage': {}}

    monkeypatch.setattr(llm_client, 'chat', fake_chat)
    monkeypatch.setattr(ai_agent, '_should_plan', lambda *a, **k: True)
    # `_lg_planner` 在 agent_graph_nodes 内解析该全局名，只 patch ai_agent 打不中
    monkeypatch.setattr(_gn, '_should_plan', lambda *a, **k: True)

    # legacy planner 由 _needs_plan 驱动：max_iterations>=4 时必然为真
    res = ai_agent._lg_planner({'cfg': {'no_plan': True, 'user_message': 'x' * 200,
                                        'max_iterations': 5, 'llm_config': {}, 'trace_id': None},
                                'messages': [{'role': 'user', 'content': 'x'}]})
    assert res['plan'] == []
    assert calls == [], 'no_plan 下 planner 节点不得发起 LLM 调用'


def test_planner_still_runs_without_no_plan(monkeypatch):
    """默认路径（no_plan 未开）规划行为不变——零回归阀。"""
    calls = []

    def fake_chat(messages, config=None, **kw):
        calls.append(messages)
        return {'content': '{"steps": ["a"]}', 'usage': {}}

    monkeypatch.setattr(llm_client, 'chat', fake_chat)
    monkeypatch.setattr(ai_agent, '_should_plan', lambda *a, **k: True)
    # `_lg_planner` 在 agent_graph_nodes 内解析该全局名，只 patch ai_agent 打不中
    monkeypatch.setattr(_gn, '_should_plan', lambda *a, **k: True)
    res = ai_agent._lg_planner({'cfg': {'user_message': 'x' * 200, 'max_iterations': 5,
                                        'llm_config': {}, 'trace_id': None},
                                'messages': [{'role': 'user', 'content': 'x'}],
                                'usage_total': {}})
    assert calls, '未开 no_plan 时规划仍应执行'
    assert res['plan'] == ['a'], f'res={res}'


# ==================== ④ 工具并行执行 ====================

def test_lg_tools_runs_parallel_and_keeps_order(monkeypatch):
    """同轮 3 个独立工具：并发执行（耗时 ≈ 单个）且结果/记账顺序与下发顺序一致。"""
    def make(tag, delay):
        def _f(**kw):
            time.sleep(delay)
            return f'result-{tag}'
        return _f

    for name, d in (('par_a', 0.30), ('par_b', 0.30), ('par_c', 0.30)):
        monkeypatch.setitem(ai_agent.registry._tools, name,
                            {'func': make(name, d), 'params': [], 'description': name})
    monkeypatch.setattr(ai_agent, '_TOOLS_PARALLEL', True)
    monkeypatch.setattr(_gn, '_TOOLS_PARALLEL', True)
    monkeypatch.setattr(ai_agent, 'TOOL_TIMEOUT_S', 10.0)

    st = _state([{'tool': n, 'params': {}} for n in ('par_a', 'par_b', 'par_c')])
    t0 = time.time()
    out = ai_agent._lg_tools(st)
    elapsed = time.time() - t0

    assert elapsed < 0.75, f'三个 0.3s 工具应并发执行，实测 {elapsed:.2f}s（疑似串行）'
    assert [c['tool'] for c in out['calls_made']] == ['par_a', 'par_b', 'par_c']
    assert all(c['status'] == 'ok' for c in out['calls_made'])
    # 消息里的工具结果也要保持下发顺序
    joined = out['messages'][-1]['content']
    assert joined.index('result-par_a') < joined.index('result-par_b') < joined.index('result-par_c')


def test_lg_tools_parallel_can_be_disabled(monkeypatch):
    """AGENT_TOOLS_PARALLEL=0 → 退回串行（耗时 ≈ 之和），零回归阀。"""
    def make(delay):
        def _f(**kw):
            time.sleep(delay)
            return 'ok'
        return _f

    for name in ('ser_a', 'ser_b'):
        monkeypatch.setitem(ai_agent.registry._tools, name,
                            {'func': make(0.25), 'params': [], 'description': name})
    monkeypatch.setattr(ai_agent, '_TOOLS_PARALLEL', False)
    # `_lg_tools` 在 agent_graph_nodes 内解析该全局名
    monkeypatch.setattr(_gn, '_TOOLS_PARALLEL', False)
    monkeypatch.setattr(ai_agent, 'TOOL_TIMEOUT_S', 10.0)

    t0 = time.time()
    ai_agent._lg_tools(_state([{'tool': 'ser_a', 'params': {}}, {'tool': 'ser_b', 'params': {}}]))
    assert time.time() - t0 >= 0.45, '关闭并行后应逐个串行执行'


def test_lg_tools_stateful_tool_stays_serial(monkeypatch):
    """有状态工具（run_skill 等）不进并行预取，避免共享全局状态被并发写坏。"""
    monkeypatch.setattr(ai_agent, 'TOOL_TIMEOUT_S', 10.0)
    seen = []

    def fake_run_skill(**kw):
        seen.append(kw)
        return 'skill-ok'

    monkeypatch.setitem(ai_agent.registry._tools, 'run_skill',
                        {'func': fake_run_skill, 'params': ['skill_name', 'task'],
                         'description': 'run skill'})
    out = ai_agent._lg_tools(_state([{'tool': 'run_skill',
                                      'params': {'skill_name': 'x', 'task': 'y'}}]))
    assert seen and out['calls_made'][0]['status'] == 'ok'


def test_prepare_tool_call_normalizes_params():
    """准备函数：file 注入与未知键丢弃（并行/串行两条路径共用，必须一致）。"""
    p = ai_agent._prepare_tool_call({'tool': 'describe', 'params': {'file': 'a.csv',
                                                                   'bogus': 1}}, None)
    assert p['tool'] == 'describe'
    assert 'bogus' not in p['params']
    assert p['info'] is not None


# ==================== ⑤ 节流分桶 ====================

def test_throttle_bucket_by_model_isolated():
    """同一 provider 的不同模型互不阻塞（管线三阶段各用不同模型）。"""
    llm_client._LAST_CLOUD_CALL.clear()
    llm_client._ADAPT_INTERVAL.clear()
    llm_client._PROVIDER_LAST_START.clear()

    llm_client._throttle_cloud('sensenova', 'glm-5.2')
    t0 = time.time()
    llm_client._throttle_cloud('sensenova', 'deepseek-v4-pro')   # 不同模型 → 只受跨模型闸门
    dt = time.time() - t0
    assert dt < llm_client._CLOUD_MIN_INTERVAL, f'不同模型不应按 2.5s 串行，实测 {dt:.2f}s'

    # 同模型第二次调用仍按生效间隔错峰
    t0 = time.time()
    llm_client._throttle_cloud('sensenova', 'deepseek-v4-pro')
    assert time.time() - t0 >= llm_client._CLOUD_MIN_INTERVAL * 0.8


def test_rate_limit_escalation_is_per_model():
    """429 抬升只作用于 (provider, model) 桶，不污染同 provider 的其他模型。"""
    llm_client._ADAPT_INTERVAL.clear()
    llm_client._OK_STREAK.clear()
    key = ('probe', 'model-A')
    b1 = llm_client._bucket_key(*key)
    b2 = llm_client._bucket_key('probe', 'model-B')

    first = llm_client.note_rate_limited('probe', model='model-A')
    assert llm_client._ADAPT_INTERVAL.get(b1) == first
    assert b2 not in llm_client._ADAPT_INTERVAL
    llm_client._ADAPT_INTERVAL.clear()


def test_throttle_scope_env_restores_provider_level():
    """CLOUD_THROTTLE_SCOPE=provider → 回退 provider 级分桶（一键回旧行为）。"""
    old = llm_client._CLOUD_THROTTLE_SCOPE
    try:
        llm_client._CLOUD_THROTTLE_SCOPE = 'provider'
        assert llm_client._bucket_key('p', 'm1') == 'p'
        assert llm_client._bucket_key('p', 'm2') == 'p'
    finally:
        llm_client._CLOUD_THROTTLE_SCOPE = old
    assert llm_client._bucket_key('p', 'm1') != llm_client._bucket_key('p', 'm2')


# ==================== ③ slim_prompt 注入裁剪 ====================

def test_slim_prompt_skips_heavy_injections(monkeypatch):
    """slim_prompt=True → 不注入知识点库/技能正文/领域框架（系统提示显著变短）。"""
    import knowledge_rag
    monkeypatch.setattr(knowledge_rag, 'get_knowledge_context',
                        lambda *a, **k: '知识点库内容' * 200, raising=False)
    monkeypatch.setattr(ai_agent, '_should_plan', lambda *a, **k: False)

    sizes = {}
    for slim in (False, True):
        captured = {}

        def fake_chat(messages, config=None, **kw):
            captured['sys'] = messages[0]['content']
            return {'content': 'done', 'usage': {}}

        monkeypatch.setattr(llm_client, 'chat', fake_chat)
        monkeypatch.setattr(llm_client, 'chat_stream', 
                            lambda *a, **k: iter([{'type': 'delta', 'text': 'done'}]), raising=False)
        ai_agent.agent_loop('分析一下基金净值的波动特征并给出结论',
                            {'provider': 'test', 'model': 'm'},
                            max_iterations=1, session_id=None, slim_prompt=slim)
        sizes[slim] = len(captured.get('sys') or '')

    assert sizes[True] < sizes[False], f'slim 应更短：{sizes}'


def test_agent_loop_accepts_all_three_fast_switches(monkeypatch):
    """三个开关走真实 agent_loop 入口（端到端接线检查，防 kwarg 名拼错静默失效）。

    - no_plan=True → 规划调用次数降为 0（用同一 fake 对比 no_plan 关闭时的调用数）
    - tool_names   → 系统提示里只有白名单工具（`ml_train` 必须消失）
    """
    monkeypatch.setattr(ai_agent, '_should_plan', lambda *a, **k: True)
    # `_lg_planner` 在 agent_graph_nodes 内解析该全局名，只 patch ai_agent 打不中
    monkeypatch.setattr(_gn, '_should_plan', lambda *a, **k: True)
    monkeypatch.setattr(llm_client, 'chat_stream',
                        lambda *a, **k: iter([{'type': 'delta', 'text': 'done'}]), raising=False)

    def _drive(**fast):
        seen = {'chat': 0, 'sys': ''}

        def fake_chat(messages, config=None, **kw):
            seen['chat'] += 1
            seen['sys'] = messages[0]['content']
            return {'content': 'done', 'usage': {}}

        monkeypatch.setattr(llm_client, 'chat', fake_chat)
        ai_agent.agent_loop('做一份宁德时代的研究报告',
                            {'provider': 'test', 'model': 'm'},
                            max_iterations=1, session_id=None, **fast)
        return seen

    _off = _drive()
    _on = _drive(no_plan=True, slim_prompt=True,
                 tool_names=['web_search', 'safe_calc'])

    assert _on['chat'] < _off['chat'], f"no_plan 应减少调用：on={_on['chat']} off={_off['chat']}"
    assert '`web_search`' in _on['sys'] and '`safe_calc`' in _on['sys']
    assert '`ml_train`' not in _on['sys'], '白名单外的工具不得出现在系统提示里'
