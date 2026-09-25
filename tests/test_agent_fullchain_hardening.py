# -*- coding: utf-8 -*-
"""Agent 全链路加固回归（2026-09-15）。

对应《AI Agent 全链路检测报告》(.trae/documents/agent_fullchain_audit_20260915.md)
中 1 个 P0 + 6 个 P1 + 关键 P2 的修复，逐条固化为断言：

- P0-1 `_lg_planner` 必须带 user 轮（否则智谱 400/1214，规划静默降级）
- P1-1 工具缓存：文件指纹隔离 + 定向失效 + 上传/删除后失效
- P1-2 conversation_memory 按 conversation_id 分桶隔离
- P1-3 路由缓存键含会话维度 + 历史签名
- P1-4 工具执行墙钟超时 + 整轮 deadline
- P1-5 `_finalize_agent_result` 透传 truncated/degraded/trace_id 等
- P1-6 原生 function-calling 按模型自适应降级
- P2-1 trace seq 进程内递增；P2-2 轨迹保留策略；P2-4 usage 归一；P2-6 内存历史上限
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402


# ======================================================================
# P0-1 planner 调用形态
# ======================================================================
def test_planner_messages_include_user_turn(monkeypatch):
    """planner 必须发送 system + user 两轮，否则 provider 报 400/1214 并静默降级 plan=[]。"""
    import ai_agent

    captured = {}

    def fake_chat(messages, cfg, **kw):
        captured['messages'] = messages
        return {'content': '["先取数", "再分析"]', 'usage': {'total_tokens': 5}}

    monkeypatch.setattr(ai_agent.llm_client, 'chat', fake_chat)
    monkeypatch.setattr(ai_agent, '_should_plan', lambda *a, **k: True)
    monkeypatch.setattr(ai_agent, 'get_long_term',
                        lambda uid: type('LT', (), {'to_natural_language': lambda self, *a, **k: ''})())

    state = {
        'cfg': {
            'llm_config': {'provider': 'zhipu', 'model': 'glm-4.7-flash'},
            'user_message': '全面分析这个数据集并输出一份完整报告',
            'max_iterations': 5, 'session_id': 's1', 'user_id': 'u1',
            'trace_id': None, 'memory_context': '',
        },
        'messages': [{'role': 'user', 'content': '全面分析这个数据集并输出一份完整报告'}],
        'usage_total': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0},
    }
    out = ai_agent._lg_planner(state)

    roles = [m.get('role') for m in captured['messages']]
    assert roles[0] == 'system', '首条应为 system 提示'
    assert 'user' in roles, 'planner 必须带 user 轮（P0-1）'
    assert out.get('plan') == ['先取数', '再分析'], '计划应被解析并注入'


def test_planner_error_degrades_silently(monkeypatch):
    """planner 失败仍应静默返回空计划（不阻断主流程）。"""
    import ai_agent

    def boom(*a, **k):
        raise RuntimeError('400 code 1214 messages 参数非法')

    monkeypatch.setattr(ai_agent.llm_client, 'chat', boom)
    monkeypatch.setattr(ai_agent, '_should_plan', lambda *a, **k: True)
    monkeypatch.setattr(ai_agent, 'get_long_term',
                        lambda uid: type('LT', (), {'to_natural_language': lambda self, *a, **k: ''})())

    state = {
        'cfg': {'llm_config': {}, 'user_message': 'x' * 30, 'max_iterations': 5,
                'session_id': 's1', 'user_id': 'u1', 'trace_id': None, 'memory_context': ''},
        'messages': [{'role': 'user', 'content': 'x' * 30}],
        'usage_total': {},
    }
    assert ai_agent._lg_planner(state) == {'plan': [], 'plan_tools_hint': {}}


# ======================================================================
# P1-1 工具缓存：文件指纹 + 定向失效
# ======================================================================
def test_tool_cache_fingerprint_isolates_versions():
    from ai_agent import ToolCache
    c = ToolCache(maxsize=5)
    c.put('describe', {'file': 'a.csv'}, 'v1', fingerprint='1:100')
    assert c.get('describe', {'file': 'a.csv'}, fingerprint='1:100') == 'v1'
    # 同名文件被覆盖 → 指纹变化 → 必须 MISS，绝不能返回旧列结构
    assert c.get('describe', {'file': 'a.csv'}, fingerprint='2:200') is None
    # 不带指纹（旧调用形态）仍可命中不带指纹写入的缓存
    c.put('head', {'file': 'b.csv'}, 'h1')
    assert c.get('head', {'file': 'b.csv'}) == 'h1'


def test_tool_cache_invalidate_file():
    from ai_agent import ToolCache
    c = ToolCache()
    c.put('describe', {'file': 'x.csv'}, 'r1')
    c.put('head', {'file': 'x.csv', 'n': 5}, 'r2')
    c.put('describe', {'file': 'y.csv'}, 'r3')
    assert c.invalidate_file('x.csv') == 2
    assert c.get('describe', {'file': 'x.csv'}) is None
    assert c.get('head', {'file': 'x.csv', 'n': 5}) is None
    assert c.get('describe', {'file': 'y.csv'}) == 'r3'
    assert c.invalidate_file('') == 0


def test_file_fingerprint_tracks_mtime_and_size():
    from ai_agent import _file_fingerprint
    from core_config import OUT_DIR

    p = OUT_DIR / f'_fp_probe_{os.getpid()}.txt'
    try:
        with open(p, 'w', encoding='utf-8') as f:
            f.write('a')
        fp1 = _file_fingerprint(p.name)
        os.utime(str(p), (time.time() + 5, time.time() + 5))
        fp2 = _file_fingerprint(p.name)
        assert fp1 and fp2 and fp1 != fp2, 'mtime 变化后指纹必须变化'
        # 绝对路径同样可解析
        assert _file_fingerprint(str(p))
        assert _file_fingerprint('__definitely_missing__.csv') == ''
        assert _file_fingerprint('') == ''
    finally:
        try:
            os.remove(p)
        except Exception:
            pass


# ======================================================================
# P1-4 工具执行超时 + 整轮 deadline
# ======================================================================
def test_tool_timeout_raises_and_returns_fast():
    import ai_agent

    def slow(**kw):
        time.sleep(2.0)
        return 'never'

    t0 = time.time()
    with pytest.raises(ai_agent.ToolTimeoutError):
        ai_agent._call_tool_with_timeout(slow, {}, timeout=0.3)
    assert time.time() - t0 < 1.5, '超时应立即返回，而非等待工具跑完'


def test_tool_timeout_disabled_runs_synchronously():
    import ai_agent
    assert ai_agent._call_tool_with_timeout(lambda **kw: 'ok', {}, timeout=0) == 'ok'
    assert ai_agent._call_tool_with_timeout(lambda **kw: 'ok', {}, timeout=-1) == 'ok'


def test_lg_tools_reports_timeout_as_error(monkeypatch):
    """工具超时 → 记为该轮失败（status=error），不写缓存，不阻塞。"""
    import ai_agent

    def slow(**kw):
        time.sleep(2.0)
        return 'never'

    monkeypatch.setitem(ai_agent.registry._tools, 'slowprobe',
                        {'func': slow, 'params': ['file'], 'description': 'slow probe'})
    monkeypatch.setattr(ai_agent, 'TOOL_TIMEOUT_S', 0.3)

    cfg = {'llm_config': {}, 'file_name': None, 'session_id': None, 'runtime_token': '',
           'max_iterations': 5, 'use_native': False, 'openai_tools': None, 'trace_id': None,
           'plan': [], 'fail_limit': 2, 'min_success_calls': 1, 'step_guard_max': 2,
           'user_message': 'x', 'deadline_ts': None}
    state = {'cfg': cfg, 'iteration': 1, 'messages': [{'role': 'user', 'content': 'x'}],
             'calls_made': [], 'failed_tools': {}, 'usage_total': {},
             'pending_calls': [{'tool': 'slowprobe', 'params': {}}],
             'last_content': '', 'facts': [], 'missing': []}

    out = ai_agent._lg_tools(state)
    assert out['calls_made'][0]['status'] == 'error'
    assert out['failed_tools'].get('slowprobe') == 1
    assert ai_agent.tool_cache.get('slowprobe', {}) is None, '超时结果不得入缓存'


def test_lg_tools_truncates_long_error_message(monkeypatch):
    """P2-8：工具异常原文截断后再回灌（防路径/凭据片段外泄，控制 token）。"""
    import ai_agent

    def boom(**kw):
        raise RuntimeError('E' * 2000)

    monkeypatch.setitem(ai_agent.registry._tools, 'boomprobe',
                        {'func': boom, 'params': ['file'], 'description': 'boom probe'})
    cfg = {'llm_config': {}, 'file_name': None, 'session_id': None, 'runtime_token': '',
           'max_iterations': 5, 'use_native': False, 'openai_tools': None, 'trace_id': None,
           'plan': [], 'fail_limit': 2, 'min_success_calls': 1, 'step_guard_max': 2,
           'user_message': 'x', 'deadline_ts': None}
    state = {'cfg': cfg, 'iteration': 1, 'messages': [{'role': 'user', 'content': 'x'}],
             'calls_made': [], 'failed_tools': {}, 'usage_total': {},
             'pending_calls': [{'tool': 'boomprobe', 'params': {}}],
             'last_content': '', 'facts': [], 'missing': []}
    out = ai_agent._lg_tools(state)
    preview = out['calls_made'][0]['result_preview']
    assert preview.startswith('Error executing boomprobe')
    assert len(preview) <= 200, f'异常原文必须截断，实际 {len(preview)}'


def test_route_tools_honours_deadline():
    """超墙钟预算 → 直接 force_final（保留已完成部分成稿，而非无限占用线程）。"""
    import ai_agent

    st = {'iteration': 1, 'cfg': {'max_iterations': 5, 'deadline_ts': time.time() + 100}}
    assert ai_agent._lg_route_tools(st) == 'agent'
    st['cfg']['deadline_ts'] = time.time() - 1
    assert ai_agent._lg_route_tools(st) == 'force_final'
    st['cfg']['deadline_ts'] = None
    assert ai_agent._lg_route_tools(st) == 'agent'
    st['iteration'] = 5
    assert ai_agent._lg_route_tools(st) == 'force_final'


# ======================================================================
# P1-2 会话记忆分桶
# ======================================================================
def test_conversation_memory_isolated_per_conversation():
    from conversation_memory import ConversationMemory
    m = ConversationMemory()
    m.set_active_conversation('A')
    m.add_turn('分析一下现金流', {'intent': 'analysis', 'entities': {}}, 'fin.csv')
    assert len(m.short_term) == 1

    m.set_active_conversation('B')
    assert m.short_term == [], '切换会话后不得看到 A 的短期记忆（P1-2）'
    m.add_turn('然后呢', {'intent': 'chat', 'entities': {}})
    assert len(m.short_term) == 1 and m.short_term[0]['intent'] == 'chat'

    m.set_active_conversation('A')
    assert len(m.short_term) == 1 and m.short_term[0]['intent'] == 'analysis'


def test_conversation_memory_reset_clears_all_buckets():
    from conversation_memory import ConversationMemory
    m = ConversationMemory()
    m.set_active_conversation('A')
    m.add_turn('x', {'intent': 'chat'})
    m.reset()
    assert m.short_term == [] and m.active_conversation_id == 'legacy'
    m.set_active_conversation('A')
    assert m.short_term == []


def test_conversation_memory_bucket_pointer_stable_after_trim():
    """add_turn 截断后 short_term 仍须指向所属会话桶（不能重新赋值丢引用）。"""
    from conversation_memory import ConversationMemory
    m = ConversationMemory(max_short=2)
    m.set_active_conversation('A')
    for i in range(5):
        m.add_turn(f'q{i}', {'intent': 'chat'})
    assert len(m.short_term) == 2
    assert m._buckets['A'] is m.short_term
    assert [t['message'] for t in m.short_term] == ['q3', 'q4']


# ======================================================================
# P1-3 路由缓存键
# ======================================================================
def test_route_cache_key_includes_conversation_and_history(monkeypatch):
    import ai_assistant
    import conversation_memory

    mem = conversation_memory.get_memory()
    mem.reset()
    monkeypatch.setattr(ai_assistant, '_chat_manager',
                        type('CM', (), {'active_conversation_id': 'A'})())
    k_a = ai_assistant._route_cache_key('然后呢')

    monkeypatch.setattr(ai_assistant, '_chat_manager',
                        type('CM', (), {'active_conversation_id': 'B'})())
    k_b = ai_assistant._route_cache_key('然后呢')
    assert k_a != k_b, '不同会话的同文本必须使用不同缓存键（P1-3）'

    mem.set_active_conversation('B')
    mem.add_turn('分析一下这家公司', {'intent': 'analysis'})
    k_b2 = ai_assistant._route_cache_key('然后呢')
    assert k_b2 != k_b, '历史变化后缓存键必须变化（DST 续接依赖历史）'
    mem.reset()


def test_route_intent_cache_still_hits_for_same_context(monkeypatch):
    """同一会话同一历史下重复调用仍命中缓存（不破坏既有性能优化）。"""
    from unittest import mock
    import ai_assistant
    import intent_router

    ai_assistant._router_cache.clear()
    calls = {'n': 0}
    real_route = intent_router.route

    def counting_route(text, history=None, llm_config=None):
        calls['n'] += 1
        return real_route(text, history=history, llm_config=llm_config)

    with mock.patch.object(intent_router, 'route', side_effect=counting_route):
        r1 = ai_assistant._route_intent('你好')
        r2 = ai_assistant._route_intent('你好')
        assert r1 is r2
        assert calls['n'] == 1
    ai_assistant._router_cache.clear()


# ======================================================================
# P1-5 结果契约透传
# ======================================================================
def _fake_agent_output():
    from ai_agent import _ensure_final_text
    calls = [{'tool': 'web_search', 'params': {'query': 'q'}, 'status': 'ok'}]
    return _ensure_final_text('## 核心结论\n' + '这是一份有实质内容的深度分析报告，' * 12, calls)


def test_finalize_passes_through_degradation_and_trace_fields(monkeypatch):
    import ai_assistant

    monkeypatch.setattr(ai_assistant, '_self_correct_answer', lambda *a, **k: None)
    prep = {'expert_ctx': '', 'calc_note': ''}
    result = {
        'ok': True, 'output': _fake_agent_output(),
        'calls': [], 'usage': {}, 'elapsed_ms': 12, 'trace': {'errors': 0},
        'truncated': True, 'degraded': True, 'end_status': 'done',
        'resumable': True, 'trace_id': '20260915-120000-abcd1234',
        'trace_file': 'output/traces/20260915-120000-abcd1234.jsonl', 'iterations': 6,
    }
    out = ai_assistant._finalize_agent_result(result, '全面分析', None, None, prep, depth='deep')

    assert out and out['reply']
    assert out['truncated'] is True
    assert out['degraded'] is True
    assert out['resumable'] is True
    assert out['end_status'] == 'done'
    assert out['iterations'] == 6
    assert out['trace_id'] == '20260915-120000-abcd1234'
    assert out['trace_file'].endswith('.jsonl')


def test_finalize_defaults_flags_when_absent(monkeypatch):
    import ai_assistant

    monkeypatch.setattr(ai_assistant, '_self_correct_answer', lambda *a, **k: None)
    prep = {'expert_ctx': '', 'calc_note': ''}
    result = {'ok': True, 'output': _fake_agent_output(), 'calls': [], 'usage': {},
              'elapsed_ms': 5, 'trace': {'errors': 0}}
    out = ai_assistant._finalize_agent_result(result, '深入分析', None, None, prep, depth='deep')

    assert out['truncated'] is False
    assert out['degraded'] is False
    assert out['trace_id'] == ''
    assert out['end_status'] == ''


def test_stalled_result_is_degraded():
    import ai_assistant
    out = ai_assistant._stalled_result('分析一下', None)
    assert out.get('degraded') is True
    assert out.get('end_status') == 'stalled'
    assert out.get('reply')


# ======================================================================
# P1-6 原生 tools 自适应降级
# ======================================================================
def test_native_tools_auto_degrade_per_model(monkeypatch):
    import ai_agent

    monkeypatch.setattr(ai_agent, '_native_disabled', set())
    monkeypatch.setattr(ai_agent, '_native_miss', {})
    monkeypatch.setattr(ai_agent, '_NATIVE_AUTO_DEGRADE', True)
    monkeypatch.setattr(ai_agent, '_NATIVE_MISS_THRESHOLD', 2)

    assert ai_agent._provider_native_tools('zhipu', 'glm-4.7-flash') is True
    ai_agent._note_native_miss('zhipu', 'glm-4.7-flash')
    assert ai_agent._provider_native_tools('zhipu', 'glm-4.7-flash') is True
    ai_agent._note_native_miss('zhipu', 'glm-4.7-flash')
    assert ai_agent._provider_native_tools('zhipu', 'glm-4.7-flash') is False, \
        '连续未采纳原生 tool_calls 达阈值 → 该模型自动降级'
    # 其它模型不受影响
    assert ai_agent._provider_native_tools('zhipu', 'glm-5') is True
    # 全局开关仍可强制关闭
    monkeypatch.setattr(ai_agent, '_NATIVE_TOOLS_ENABLED', False)
    assert ai_agent._provider_native_tools('openai', 'gpt-4o-mini') is False


def test_native_degrade_can_be_disabled(monkeypatch):
    import ai_agent
    monkeypatch.setattr(ai_agent, '_native_disabled', set())
    monkeypatch.setattr(ai_agent, '_native_miss', {})
    monkeypatch.setattr(ai_agent, '_NATIVE_AUTO_DEGRADE', False)
    monkeypatch.setattr(ai_agent, '_NATIVE_MISS_THRESHOLD', 1)

    ai_agent._note_native_miss('zhipu', 'glm-4.7-flash')
    assert ai_agent._provider_native_tools('zhipu', 'glm-4.7-flash') is True


# ======================================================================
# P2-4 usage 归一
# ======================================================================
def test_normalize_usage_maps_aliases_and_total():
    from llm_client import _normalize_usage

    u = _normalize_usage({'input_tokens': 10, 'output_tokens': 5})
    assert u['prompt_tokens'] == 10 and u['completion_tokens'] == 5 and u['total_tokens'] == 15

    u2 = _normalize_usage({'prompt_tokens': '3', 'completion_tokens': 4})
    assert u2['prompt_tokens'] == 3 and u2['total_tokens'] == 7

    assert _normalize_usage(None) == {}
    assert _normalize_usage({'flag': True}) == {}
    # 嵌套 details 对象不得影响归一结果
    u3 = _normalize_usage({'prompt_tokens': 1, 'total_tokens': 2,
                           'prompt_tokens_details': {'cached_tokens': 1}})
    assert u3['prompt_tokens'] == 1 and u3['total_tokens'] == 2
    assert 'prompt_tokens_details' not in u3


def test_lg_emit_usage_accumulates_alias_fields():
    import ai_agent
    usage = {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}
    ai_agent._lg_emit_usage(usage, {'usage': {'input_tokens': 10, 'output_tokens': 5}})
    assert usage['prompt_tokens'] == 11
    assert usage['completion_tokens'] == 6
    assert usage['total_tokens'] == 17


# ======================================================================
# P2-1 / P2-2 trace_store
# ======================================================================
def test_trace_seq_monotonic_and_cache_forgotten():
    import uuid
    import trace_store

    rid = 'pytest-' + uuid.uuid4().hex[:10]
    try:
        s1 = trace_store.event(rid, 'run_start', {'a': 1})
        s2 = trace_store.event(rid, 'step', {})
        s3 = trace_store.event(rid, 'step', {})
        assert (s1, s2, s3) == (1, 2, 3), 'seq 必须单调递增'
        assert trace_store._seq_cache.get(rid) == 3, 'seq 应进程内缓存，避免全量重读'
        trace_store.end_run(rid, 'ok')
        assert rid not in trace_store._seq_cache, 'run 结束后应回收 seq 计数'
    finally:
        trace_store.delete_run(rid)


def test_trace_event_rejects_path_traversal():
    import trace_store
    assert trace_store.event('../../etc/passwd', 'x', {}) == 0


def test_prune_runs_keeps_newest(monkeypatch):
    import shutil
    import uuid
    from pathlib import Path
    import trace_store

    d = Path(trace_store._base_dir()) / f'_prune_probe_{uuid.uuid4().hex[:8]}'
    d.mkdir(parents=True, exist_ok=True)
    try:
        monkeypatch.setattr(trace_store, '_trace_dir', lambda: d)
        now = time.time()
        for i in range(5):
            f = d / f'run-{i}.jsonl'
            f.write_text('{"seq":1,"type":"run_start","data":{}}\n', encoding='utf-8')
            os.utime(str(f), (now - i * 100, now - i * 100))
        removed = trace_store.prune_runs(keep_max=2, keep_days=0)
        remaining = sorted(p.name for p in d.glob('*.jsonl'))
        assert removed == 3
        assert len(remaining) == 2, f'应只保留最近 2 个，实际 {remaining}'
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_prune_runs_noop_when_disabled(monkeypatch):
    import trace_store
    assert trace_store.prune_runs(keep_max=0, keep_days=0) == 0


# ======================================================================
# P2-5 工具 schema 覆盖
# ======================================================================
def test_every_tool_has_param_schema():
    """schema 覆盖率须为 32/32（此前 9 个工具无 schema）。"""
    import ai_agent
    missing = [t['name'] for t in ai_agent.registry.list_tools()
               if t['name'] not in ai_agent._TOOL_PARAM_SCHEMA]
    assert missing == [], f'以下工具缺 schema: {missing}'


def test_openai_tools_have_typed_props_and_required():
    import ai_agent
    tools = {t['function']['name']: t['function'] for t in ai_agent._build_openai_tools()}
    fin = tools['finance_data']['parameters']
    assert fin['properties']['symbol']['type'] == 'string'
    assert 'symbol' in fin['required']
    assert 'period' not in fin['required'], 'period 可省略，不应标 required'
    # file-only 工具不应有任何 required（后端自动注入）
    assert tools['describe']['parameters']['required'] == []


def test_tool_param_alias_normalisation():
    """模型输出同义参数名（company_name）应被归一为声明名（company）而非丢弃。"""
    import ai_agent
    info = ai_agent.registry.get_tool('company_analysis')
    cleaned, dropped = ai_agent._normalize_tool_params(
        'company_analysis', {'company_name': '贵州茅台'}, info)
    assert cleaned.get('company') == '贵州茅台'
    assert 'company_name' in dropped


# ======================================================================
# P2-6 内存历史上限
# ======================================================================
def test_history_memory_cap_trims():
    import ai_assistant
    cm = ai_assistant.ChatManager.__new__(ai_assistant.ChatManager)
    cm.history = [ai_assistant.ChatMessage(role='user', content=str(i)) for i in range(10)]
    cm._HISTORY_MEM_CAP = 4
    cm._trim_history_memory()
    assert len(cm.history) == 4
    assert cm.history[-1].content == '9'
    cm._trim_history_memory()  # 幂等
    assert len(cm.history) == 4
