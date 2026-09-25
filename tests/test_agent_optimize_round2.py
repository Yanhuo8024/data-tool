# -*- coding: utf-8 -*-
"""本轮增量优化回归：核验透传 / prune 异步 / 意图计数否决 / 问题页过滤。"""
import inspect
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_finalize_passes_fact_audit():
    from ai_assistant import _finalize_agent_result
    src = inspect.getsource(_finalize_agent_result)
    assert "result.get('fact_audit')" in src
    assert "no_tool_with_file" in src


def test_frontend_renders_fact_audit():
    core = (PROJECT_ROOT / 'static' / 'js' / 'core.js').read_text(encoding='utf-8')
    assert 'fact_audit' in core
    assert '核验数据' in core
    assert '数据必须真实' in core or '不得作为结论依据' in core


def test_maybe_prune_is_async():
    import trace_store
    src = inspect.getsource(trace_store._maybe_prune)
    assert 'Thread' in src
    assert 'daemon' in src
    # 同步路径不得再紧跟着无条件 prune_runs（旧代码残留）
    assert src.count('prune_runs()') <= 2  # bg + except fallback


def test_counting_words_veto_out_of_scope():
    from intent_router import _out_of_scope_score, route
    assert _out_of_scope_score('统计请假条数量') == 0.0
    assert _out_of_scope_score('一共有多少条记录') == 0.0
    r = route('统计请假条数量')
    # 不应再被当成域外写作请求
    assert r.get('intent') not in ('out_of_scope', None)
    assert r.get('action') != 'chat' or r.get('intent') != 'out_of_scope'


def test_problems_page_real_filter():
    page = (PROJECT_ROOT / 'static' / 'agent-problems.html').read_text(encoding='utf-8')
    assert '真问题' in page
    assert 'NO_TOOL_CALLS_NOFILE' in page
    assert "f==='real'" in page
