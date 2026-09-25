#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent_flow 回归测试：Agent 流程归一化 + 故障定位。

覆盖重点：
  1. 归一化正确性：阶段归并 / 节点耗时推定 / 尾阶段用总耗时收边 / 未知事件不丢
  2. 每条诊断规则都能被真实事件形态触发（这是"跟踪哪一部分出问题"的可信度基础）
  3. 规则目录 RULE_CATALOG 与实际产出的 code 一致 —— 防止新增规则漏登记
  4. 跨运行聚合（各环节失败率排行）

注意：本套测试在项目内独立目录写临时轨迹（不使用 tmp_path，也绝不做删除），
      与项目既有的"零删除"测试约定保持一致。
"""
import os
import time
import uuid
from pathlib import Path

import pytest

import agent_flow
import trace_store

_BASE = Path(__file__).resolve().parent.parent / ".workbuddy" / "pt_agentflow"


def _new_dir() -> Path:
    d = _BASE / uuid.uuid4().hex[:10]
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def traced(monkeypatch):
    """把 trace_store 指向隔离目录，并返回"写一次运行"的函数。"""
    monkeypatch.setattr(trace_store, "TRACE_DIR", str(_new_dir()))

    def start(events):
        rid = "r" + uuid.uuid4().hex[:10]
        for et, data in events:
            trace_store.event(rid, et, data or {})
        return rid

    return start


def _codes(flow):
    return {p['code'] for p in flow['diagnosis']['problems']}


# ----------------------------------------------------------------------
# 归一化
# ----------------------------------------------------------------------
def test_missing_run_returns_none(monkeypatch):
    monkeypatch.setattr(trace_store, "TRACE_DIR", str(_new_dir()))
    assert agent_flow.build_flow("nope-not-exist") is None
    assert agent_flow.diagnose_run("nope-not-exist") is None


def test_phases_nodes_and_timeline(traced):
    rid = traced([
        ('run_start', {'user_message': '分析一下', 'provider': 'sensenova', 'model': 'glm-5.2'}),
        ('agent_config', {'max_iterations': 5, 'plan': True, 'file': 'a.csv'}),
        ('llm_call', {'iteration': 1, 'has_tool_calls': True, 'content_len': 30, 'duration_ms': 1200}),
        ('tool_result', {'tool': 'describe', 'status': 'ok', 'duration_ms': 300, 'iteration': 1}),
        ('finalize.start', {'truncated': False, 'round': 1}),
        ('finalize.done', {'ok': True, 'content_len': 800, 'duration_ms': 2000}),
        ('run_end', {'outcome': 'ok', 'ok': True, 'summary': {'iterations': 1, 'tool_calls': 1,
                                                             'elapsed_ms': 4000, 'output_len': 800}}),
    ])
    flow = agent_flow.build_flow(rid)

    assert flow['status'] == 'ok'
    assert flow['metrics']['tool_calls'] == 1
    assert flow['metrics']['llm_calls'] == 1
    assert flow['metrics']['elapsed_ms'] == 4000
    assert flow['metrics']['has_file'] is True
    assert flow['metrics']['file'] == 'a.csv'

    keys = [p['key'] for p in flow['phases']]
    assert keys == ['intake', 'execution', 'finalize']

    # 节点顺序即事件顺序，offset 单调不减
    offs = [n['offset_ms'] for n in flow['nodes'] if n['offset_ms'] is not None]
    assert offs == sorted(offs)
    # 显式耗时被保留，不被推定覆盖
    fin = [n for n in flow['nodes'] if n['kind'] == 'finalize.done'][0]
    assert fin['elapsed_ms'] == 2000 and fin['elapsed_estimated'] is False


def test_tail_phase_uses_total_elapsed(traced):
    """尾阶段（含 run_end）自身无耗时，必须用总耗时收边，否则显示为 0。"""
    rid = traced([
        ('run_start', {'user_message': 'x'}),
        ('llm_call', {'iteration': 1, 'duration_ms': 1000}),
        ('run_end', {'outcome': 'ok', 'ok': True, 'summary': {'elapsed_ms': 9000}}),
    ])
    flow = agent_flow.build_flow(rid)
    fin = [p for p in flow['phases'] if p['key'] == 'finalize'][0]
    assert fin['elapsed_ms'] == 9000 - (fin['offset_ms'] or 0)
    assert (fin['elapsed_ms'] or 0) > 0


def test_unknown_event_kept_in_other_phase(traced):
    """未知事件类型不得丢弃 —— 归入 other 阶段，保证"有事件必有节点"。"""
    rid = traced([
        ('run_start', {'user_message': 'x'}),
        ('brand_new_event_type', {'foo': 'bar'}),
        ('run_end', {'outcome': 'ok', 'ok': True, 'summary': {}}),
    ])
    flow = agent_flow.build_flow(rid)
    assert 'brand_new_event_type' in {n['kind'] for n in flow['nodes']}
    assert any(p['key'] == 'other' for p in flow['phases'])


def test_elapsed_estimated_for_events_without_duration(traced):
    rid = traced([
        ('run_start', {'user_message': 'x'}),
        ('planner.start', {'user_goal': 'g'}),
        ('planner.done', {'steps': ['a', 'b']}),
        ('run_end', {'outcome': 'ok', 'ok': True, 'summary': {}}),
    ])
    flow = agent_flow.build_flow(rid)
    est = [n for n in flow['nodes'] if n['kind'] == 'planner.start'][0]
    assert est['elapsed_estimated'] is True


def test_include_events_flag(traced):
    rid = traced([('run_start', {'user_message': 'x'}),
                  ('run_end', {'outcome': 'ok', 'ok': True, 'summary': {}})])
    assert 'events' not in agent_flow.build_flow(rid)
    assert len(agent_flow.build_flow(rid, include_events=True)['events']) == 2


# ----------------------------------------------------------------------
# 诊断规则（逐条触发）
# ----------------------------------------------------------------------
def test_run_interrupted(traced):
    """有 run_start 无 run_end 且长期未更新 → 判定为中断而非"还在跑"。"""
    rid = traced([('run_start', {'user_message': 'x'}),
                  ('llm_call', {'iteration': 1, 'duration_ms': 100})])
    p = trace_store.trace_file(rid)
    old = time.time() - (agent_flow.STALE_RUN_SECONDS + 60)
    os.utime(p, (old, old))
    flow = agent_flow.build_flow(rid)
    assert flow['status'] == 'interrupted'
    assert 'RUN_INTERRUPTED' in _codes(flow)


def test_running_not_flagged_when_fresh(traced):
    """刚写、没有 run_end → 应视为 running，不得误报中断。"""
    rid = traced([('run_start', {'user_message': 'x'}),
                  ('llm_call', {'iteration': 1, 'duration_ms': 100})])
    flow = agent_flow.build_flow(rid)
    assert flow['status'] == 'running'
    assert 'RUN_INTERRUPTED' not in _codes(flow)


def test_run_failed(traced):
    rid = traced([('run_start', {'user_message': 'x'}),
                  ('run_end', {'outcome': 'error', 'ok': False, 'summary': {}})])
    flow = agent_flow.build_flow(rid)
    assert flow['status'] == 'failed'
    assert 'RUN_FAILED' in _codes(flow)


def test_cancelled_is_degraded_not_failed(traced):
    rid = traced([('run_start', {'user_message': 'x'}),
                  ('run_end', {'outcome': 'cancelled', 'ok': False, 'summary': {}})])
    flow = agent_flow.build_flow(rid)
    assert flow['status'] == 'degraded'
    assert 'RUN_FAILED' not in _codes(flow)


def test_empty_output(traced):
    """标记成功但输出为空 —— 用户看到的就是"AI 不回复"。"""
    rid = traced([('run_start', {'user_message': 'x'}),
                  ('llm_call', {'iteration': 1}),
                  ('run_end', {'outcome': 'ok', 'ok': True,
                               'summary': {'elapsed_ms': 100, 'output_len': 0}})])
    assert 'EMPTY_OUTPUT' in _codes(agent_flow.build_flow(rid))


def test_graph_invoke_raised(traced):
    rid = traced([('run_start', {'user_message': 'x'}),
                  ('run_end', {'outcome': 'error', 'ok': False,
                               'summary': {'error': 'graph invoke raised'}})])
    assert 'GRAPH_INVOKE_RAISED' in _codes(agent_flow.build_flow(rid))


def test_graph_invoke_raised_shows_root_cause(traced):
    """2026-09-18：summary 带 error_type/error_detail 时，诊断消息必须透出根因。"""
    rid = traced([('run_start', {'user_message': 'x'}),
                  ('graph.error', {'error_type': 'KeyError', 'error': "'usage_total'"}),
                  ('run_end', {'outcome': 'error', 'ok': False,
                               'summary': {'error': 'graph invoke raised',
                                           'error_type': 'KeyError',
                                           'error_detail': "'usage_total'"}})])
    flow = agent_flow.build_flow(rid)
    probs = [p for p in flow['diagnosis']['problems'] if p['code'] == 'GRAPH_INVOKE_RAISED']
    assert probs, '应命中 GRAPH_INVOKE_RAISED'
    assert 'KeyError' in probs[0]['detail'] and "'usage_total'" in probs[0]['detail']


def test_conftest_isolates_trace_dir():
    """conftest 必须把测试轨迹指到临时目录（防污染生产 output/traces 健康统计）。

    回归背景（2026-09-18）：未隔离时 pytest 的桩异常被记成"管线阶段失败"，
    健康面板 PIPELINE_STAGE_FAILED 虚高到 12%（全是假警报）。
    """
    assert 'dt-test-traces-' in trace_store.TRACE_DIR, \
        f"TRACE_DIR 未隔离: {trace_store.TRACE_DIR!r}"
    norm = trace_store.TRACE_DIR.replace('\\', '/')
    assert not norm.rstrip('/').endswith('output/traces'), \
        f"TRACE_DIR 仍指向生产目录: {trace_store.TRACE_DIR!r}"


def test_planner_error(traced):
    rid = traced([('run_start', {'user_message': 'x'}),
                  ('planner.error', {'error': 'HTTP 400 code 1214'}),
                  ('run_end', {'outcome': 'ok', 'ok': True, 'summary': {'output_len': 10}})])
    flow = agent_flow.build_flow(rid)
    assert 'PLANNER_ERROR' in _codes(flow)
    assert flow['phases'][0]['key'] == 'intake'
    assert any(p['key'] == 'planning' for p in flow['phases'])


def test_finalize_failed(traced):
    rid = traced([('run_start', {'user_message': 'x'}),
                  ('finalize_failed', {'error': 'timeout'}),
                  ('run_end', {'outcome': 'degraded', 'ok': False,
                               'summary': {'output_len': 5}})])
    assert 'FINALIZE_FAILED' in _codes(agent_flow.build_flow(rid))


def test_tool_error(traced):
    rid = traced([('run_start', {'user_message': 'x'}),
                  ('tool_result', {'tool': 'arima', 'status': 'error', 'iteration': 1,
                                   'error': 'not enough data'}),
                  ('run_end', {'outcome': 'ok', 'ok': True, 'summary': {'output_len': 10}})])
    flow = agent_flow.build_flow(rid)
    assert 'TOOL_ERROR' in _codes(flow)
    bad = [n for n in flow['nodes'] if n['kind'] == 'tool_result'][0]
    assert bad['status'] == 'fail'


def test_no_tool_calls_with_file_is_warning(traced):
    rid = traced([('run_start', {'user_message': '看看数据'}),
                  ('agent_config', {'file': 'sales.csv'}),
                  ('llm_call', {'iteration': 1}), ('llm_call', {'iteration': 2}),
                  ('run_end', {'outcome': 'ok', 'ok': True, 'summary': {'output_len': 20}})])
    flow = agent_flow.build_flow(rid)
    assert 'NO_TOOL_CALLS' in _codes(flow)
    p = [x for x in flow['diagnosis']['problems'] if x['code'] == 'NO_TOOL_CALLS'][0]
    assert p['severity'] == 'warning'


def test_no_tool_calls_without_file_is_info(traced):
    """无文件时零工具属正常形态，必须降级为 info，否则噪声会淹没真问题。"""
    rid = traced([('run_start', {'user_message': '你好'}),
                  ('agent_config', {'file': ''}),
                  ('llm_call', {'iteration': 1}), ('llm_call', {'iteration': 2}),
                  ('run_end', {'outcome': 'ok', 'ok': True, 'summary': {'output_len': 20}})])
    flow = agent_flow.build_flow(rid)
    assert 'NO_TOOL_CALLS_NOFILE' in _codes(flow)
    assert 'NO_TOOL_CALLS' not in _codes(flow)
    p = [x for x in flow['diagnosis']['problems'] if x['code'] == 'NO_TOOL_CALLS_NOFILE'][0]
    assert p['severity'] == 'info'


def test_fallback_retry_threshold(traced):
    """1 次重试属正常，达到阈值才提示。"""
    def build(n):
        events = [('run_start', {'user_message': 'x'})]
        events += [('fallback_retry', {'mode': 'native_no_calls'}) for _ in range(n)]
        events += [('tool_result', {'tool': 't', 'status': 'ok', 'iteration': 1}),
                   ('run_end', {'outcome': 'ok', 'ok': True, 'summary': {'output_len': 10}})]
        return traced(events)

    one = agent_flow.build_flow(build(1))
    assert 'NATIVE_FALLBACK' not in _codes(one)
    many = agent_flow.build_flow(build(agent_flow.FALLBACK_WARN_N))
    assert 'NATIVE_FALLBACK' in _codes(many)


def test_native_degraded_and_param_drop(traced):
    rid = traced([('run_start', {'user_message': 'x'}),
                  ('native_tools_degraded', {'misses': 3, 'model': 'glm-5.2'}),
                  ('tool_param_drop', {'tool': 'describe', 'dropped': 'foo'}),
                  ('run_end', {'outcome': 'ok', 'ok': True, 'summary': {'output_len': 5}})])
    codes = _codes(agent_flow.build_flow(rid))
    assert 'NATIVE_DEGRADED' in codes and 'TOOL_PARAM_DROP' in codes


def test_fact_unverified(traced):
    """ratio = 未核验率（missing/total）：高于阈值才报警（2026-09-18 修正方向反转）。"""
    rid = traced([('run_start', {'user_message': 'x'}),
                  ('fact_audit', {'total': 10, 'unverified': 8, 'ratio': 0.8}),
                  ('run_end', {'outcome': 'ok', 'ok': True, 'summary': {'output_len': 5}})])
    assert 'FACT_UNVERIFIED' in _codes(agent_flow.build_flow(rid))


def test_fact_verified_ok(traced):
    rid = traced([('run_start', {'user_message': 'x'}),
                  ('fact_audit', {'total': 10, 'unverified': 1, 'ratio': 0.1}),
                  ('run_end', {'outcome': 'ok', 'ok': True, 'summary': {'output_len': 5}})])
    assert 'FACT_UNVERIFIED' not in _codes(agent_flow.build_flow(rid))


def test_force_final_is_info_not_truncated(traced):
    """达到最大轮数强制收尾 = 正常流程（info），不得再报"输出截断"（2026-09-18 语义拆分）。"""
    rid = traced([('run_start', {'user_message': 'x'}),
                  ('run_end', {'outcome': 'ok', 'ok': True,
                               'summary': {'output_len': 500, 'force_final': True}})])
    codes = _codes(agent_flow.build_flow(rid))
    assert 'FORCE_FINAL' in codes
    assert 'TRUNCATED' not in codes


def test_pipeline_stage_failed(traced):
    """多 Agent 协作：某阶段未产出 → critical，并落在 pipeline 泳道。"""
    rid = traced([('run_start', {'user_message': '全面分析'}),
                  ('pipeline.stage_start', {'stage': '市场分析师', 'index': 1}),
                  ('pipeline.stage_done', {'stage': '市场分析师', 'ok': True, 'chars': 100,
                                           'elapsed_s': 3.0}),
                  ('pipeline.stage_start', {'stage': '量化建模师', 'index': 2}),
                  ('pipeline.stage_error', {'stage': '量化建模师', 'ok': False, 'chars': 0,
                                            'elapsed_s': 1.0, 'error': '401 Forbidden'}),
                  ('run_end', {'outcome': 'degraded', 'ok': True,
                               'summary': {'output_len': 100, 'failed_stages': ['量化建模师']}})])
    flow = agent_flow.build_flow(rid)
    assert 'PIPELINE_STAGE_FAILED' in _codes(flow)
    pl = [p for p in flow['phases'] if p['key'] == 'pipeline'][0]
    assert pl['status'] == 'fail'


def test_kb_miss(traced):
    rid = traced([('run_start', {'user_message': 'x'}),
                  ('kb_retrieval', {'hits': 0}),
                  ('run_end', {'outcome': 'ok', 'ok': True, 'summary': {'output_len': 5}})])
    assert 'KB_MISS' in _codes(agent_flow.build_flow(rid))


def test_truncated(traced):
    rid = traced([('run_start', {'user_message': 'x'}),
                  ('run_end', {'outcome': 'ok', 'ok': True,
                               'summary': {'output_len': 5, 'truncated': True}})])
    assert 'TRUNCATED' in _codes(agent_flow.build_flow(rid))


def test_slow_llm_call(traced, monkeypatch):
    monkeypatch.setattr(agent_flow, 'SLOW_LLM_CALL_MS', 0)
    rid = traced([('run_start', {'user_message': 'x'}),
                  ('llm_call', {'iteration': 1, 'duration_ms': 25000}),
                  ('run_end', {'outcome': 'ok', 'ok': True, 'summary': {'output_len': 5}})])
    assert 'SLOW_LLM_CALL' in _codes(agent_flow.build_flow(rid))


def test_slow_stage(traced, monkeypatch):
    monkeypatch.setattr(agent_flow, 'SLOW_STAGE_MS', 0)
    rid = traced([('run_start', {'user_message': 'x'}),
                  ('llm_call', {'iteration': 1, 'duration_ms': 100}),
                  ('run_end', {'outcome': 'ok', 'ok': True, 'summary': {'elapsed_ms': 100}})])
    assert 'SLOW_STAGE' in _codes(agent_flow.build_flow(rid))


def test_healthy_run_has_no_problems(traced):
    """一次正常运行不应产生任何问题项 —— 防止规则过敏制造噪声。"""
    rid = traced([('run_start', {'user_message': '分析销售数据', 'provider': 'sensenova',
                                 'model': 'glm-5.2'}),
                  ('agent_config', {'file': 'sales.csv', 'max_iterations': 5}),
                  ('llm_call', {'iteration': 1, 'has_tool_calls': True, 'duration_ms': 900}),
                  ('tool_result', {'tool': 'describe', 'status': 'ok', 'iteration': 1,
                                   'duration_ms': 200}),
                  ('finalize.start', {'truncated': False, 'round': 1}),
                  ('finalize.done', {'ok': True, 'content_len': 500, 'duration_ms': 1500}),
                  ('run_end', {'outcome': 'ok', 'ok': True,
                               'summary': {'iterations': 1, 'tool_calls': 1, 'elapsed_ms': 3000,
                                           'output_len': 500}})])
    flow = agent_flow.build_flow(rid)
    assert flow['diagnosis']['problems'] == []
    assert flow['diagnosis']['level'] == 'ok'
    assert flow['diagnosis']['score'] == 100


# ----------------------------------------------------------------------
# 健康分与严重度
# ----------------------------------------------------------------------
def test_severity_penalty_ordering(traced):
    critical = agent_flow.build_flow(traced([
        ('run_start', {'user_message': 'x'}),
        ('run_end', {'outcome': 'error', 'ok': False,
                     'summary': {'error': 'graph invoke raised'}})]))
    assert critical['diagnosis']['level'] == 'error'
    assert critical['diagnosis']['counts']['critical'] >= 2
    assert critical['diagnosis']['score'] < 100
    assert critical['diagnosis']['primary_fault']['severity'] == 'critical'


def test_primary_fault_is_most_severe(traced):
    """首要故障必须是最严重的那条，而不是最先跑出来的那条。"""
    rid = traced([('run_start', {'user_message': 'x'}),
                  ('planner.error', {'error': 'boom'}),        # warning，先出现
                  ('finalize_failed', {'error': 'timeout'}),   # critical，后出现
                  ('run_end', {'outcome': 'error', 'ok': False, 'summary': {}})])
    flow = agent_flow.build_flow(rid)
    assert flow['diagnosis']['primary_fault']['severity'] == 'critical'


# ----------------------------------------------------------------------
# 聚合
# ----------------------------------------------------------------------
def test_aggregate_health(traced):
    for ok in (True, True, False):
        traced([('run_start', {'user_message': 'm'}),
                ('agent_config', {'file': 'f.csv'}),
                ('llm_call', {'iteration': 1}),
                ('run_end', {'outcome': 'ok' if ok else 'error', 'ok': ok,
                             'summary': {'elapsed_ms': 1000, 'output_len': 1}})])
    h = agent_flow.aggregate_health(50)
    assert h['runs'] == 3
    assert h['by_status'].get('ok') == 2
    assert h['by_status'].get('failed') == 1
    assert 0 < h['healthy_rate'] < 1
    assert any(c['code'] == 'RUN_FAILED' and c['count'] == 1 for c in h['codes'])
    assert all(0 <= p['rate'] <= 1 for p in h['phases'])


def test_summarize_recent(traced):
    traced([('run_start', {'user_message': 'ok run'}),
            ('run_end', {'outcome': 'ok', 'ok': True, 'summary': {'elapsed_ms': 10}})])
    items = agent_flow.summarize_recent(10)
    assert items
    it = items[0]
    for key in ('run_id', 'status', 'level', 'score', 'problem_count', 'elapsed_ms'):
        assert key in it


# ----------------------------------------------------------------------
# 规则表自身的完整性
# ----------------------------------------------------------------------
def test_rule_table_all_callable():
    for rule in agent_flow._RULES:
        assert callable(rule)


def test_rule_catalog_covers_all_emitted_codes():
    """RULE_CATALOG 必须覆盖源码里所有 `_mk('CODE', ...)` 产出 —— 防止新增规则漏登记。
    静态扫描而非手写清单，否则清单本身会漂移。"""
    import inspect
    import re
    src = inspect.getsource(agent_flow)
    emitted = set(re.findall(r"_mk\(\s*'([A-Z_][A-Z0-9_]*)'", src))
    assert emitted, '未能从源码中解析出任何诊断 code（正则或写法已变）'
    missing = emitted - set(agent_flow.RULE_CATALOG)
    assert not missing, f'以下 code 已产出但未登记进 RULE_CATALOG: {sorted(missing)}'


def test_rule_catalog_shape():
    cat = agent_flow.rule_catalog()
    assert len(cat) == len(agent_flow.RULE_CATALOG)
    for item in cat:
        assert set(item) == {'code', 'severity', 'title', 'description'}
        assert item['severity'] in ('critical', 'warning', 'info')
    # 严重度排序：critical 在前
    assert cat[0]['severity'] == 'critical'


def test_phase_defs_exposed(traced):
    rid = traced([('run_start', {'user_message': 'x'}),
                  ('run_end', {'outcome': 'ok', 'ok': True, 'summary': {}})])
    flow = agent_flow.build_flow(rid)
    keys = [p['key'] for p in flow['phase_defs']]
    assert keys[0] == 'intake' and 'execution' in keys
