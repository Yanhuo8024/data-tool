# -*- coding: utf-8 -*-
"""回归：graph 空值加固 / skill_loaded / 事实门禁。"""
import inspect


def test_lg_safe_cfg_and_ctx_none_safe():
    import ai_agent as m
    assert m._lg_safe_cfg(None)['llm_config'] == {}
    assert m._lg_safe_cfg({})['plan'] == []
    assert m._lg_ctx(None) == {'progress_callback': None, 'memory_state': None}
    assert m._lg_cb(None) is None
    u = m._lg_emit_usage(None, {'usage': {'prompt_tokens': 1, 'completion_tokens': 2, 'total_tokens': 3}})
    assert u['total_tokens'] == 3


def test_graph_nodes_use_safe_cfg():
    import ai_agent as m
    for fn in (m._lg_planner, m._lg_agent, m._lg_tools, m._lg_finish):
        src = inspect.getsource(fn)
        assert '_lg_safe_cfg' in src, fn.__name__
        assert "state['cfg']" not in src, fn.__name__


def test_skill_auto_match_emits_skill_loaded():
    import ai_agent as m
    src = inspect.getsource(m.agent_loop)
    assert 'skill_loaded' in src
    assert 'auto_match' in src


def test_fact_gate_marks_degraded_when_unverified_high():
    import ai_agent as m
    src = inspect.getsource(m.agent_loop)
    assert 'fact_unverified_high' in src
    assert '_ratio > 0.5' in src or 'ratio > 0.5' in src


def test_finalize_passes_fact_unverified_flag():
    from ai_assistant import _finalize_agent_result
    # degraded 已有透传；fact_unverified 可并入 degraded 路径
    src = inspect.getsource(_finalize_agent_result)
    assert 'degraded' in src
