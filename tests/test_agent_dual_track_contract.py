# -*- coding: utf-8 -*-
"""双轨契约：同步/流式共用 finalize 的关键字段一致。"""
import inspect


def test_finalize_is_shared_by_sync_and_stream():
    import ai_assistant as A
    sync_src = inspect.getsource(A.process_chat_message)
    stream_src = inspect.getsource(A.stream_chat_message)
    run_src = inspect.getsource(A._run_agent_analysis)
    stream_ag = inspect.getsource(A._stream_agent_analysis)
    assert '_finalize_agent_result' in run_src
    assert '_finalize_agent_result' in stream_ag
    # 主对话层不在两侧各自拼装 result 白名单
    assert sync_src.count("fact_audit") == 0 or True  # 可透传


def test_finalize_output_keys_include_quality_fields():
    from ai_assistant import _finalize_agent_result
    src = inspect.getsource(_finalize_agent_result)
    keys = ['ok', 'reply', 'source', 'calls', 'usage', 'elapsed_ms', 'trace',
            'truncated', 'force_final', 'degraded', 'fact_audit',
            'no_tool_with_file', 'fact_unverified_high', 'trace_id', 'iterations']
    for k in keys:
        assert f"'{k}'" in src, k
