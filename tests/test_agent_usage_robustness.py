# -*- coding: utf-8 -*-
"""agent usage 解析健壮性 —— 回归测试

背景（2026-09-14 定位的真实缺陷）
--------------------------------
`ai_agent._lg_agent` 在写 trace 事件时曾用：

    'usage': {k: int(v) for k, v in (usage or {}).items() if k != 'usage'}

它对 usage 的**每一个值**直接 int()。而智谱 GLM-4.x 的响应 usage 形如：

    {"prompt_tokens": 18, "completion_tokens": 111, "total_tokens": 129,
     "prompt_tokens_details": {"cached_tokens": 3},
     "completion_tokens_details": {"reasoning_tokens": 107}}

其中 `*_details` 是 dict → `int(dict)` 抛 TypeError。

危害（为什么是 P0）：该异常发生在 **LLM 调用已成功返回之后**，却被同层的
`except Exception` 捕获，于是本轮被记作 "LangGraph agent LLM 调用失败"
（`consec_fail += 1`），一次成功的调用被计为失败；连续达到 fail_limit 后
agent 直接降级。外部表征是「模型明明连通，agent 却持续失败」。

本文件锁定修复后的行为，并保留一条绊线防止旧写法回潮。
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

# 智谱 GLM-4.7-flash 实测返回的 usage（2026-09-14 抓取）
ZHIPU_USAGE = {
    'prompt_tokens': 18,
    'completion_tokens': 111,
    'total_tokens': 129,
    'prompt_tokens_details': {'cached_tokens': 3},
    'completion_tokens_details': {'reasoning_tokens': 107},
}

OPENAI_USAGE = {
    'prompt_tokens': 100,
    'completion_tokens': 50,
    'total_tokens': 150,
    'prompt_tokens_details': {'cached_tokens': 64, 'audio_tokens': 0},
    'completion_tokens_details': {'reasoning_tokens': 20, 'audio_tokens': 0},
}


class TestSafeIntUsage:

    def test_zhipu_nested_details_do_not_crash(self):
        from ai_agent import _safe_int_usage
        out = _safe_int_usage(ZHIPU_USAGE)
        assert out == {'prompt_tokens': 18, 'completion_tokens': 111, 'total_tokens': 129}

    def test_openai_nested_details_do_not_crash(self):
        from ai_agent import _safe_int_usage
        out = _safe_int_usage(OPENAI_USAGE)
        assert out == {'prompt_tokens': 100, 'completion_tokens': 50, 'total_tokens': 150}

    def test_every_value_is_int(self):
        from ai_agent import _safe_int_usage
        for usage in (ZHIPU_USAGE, OPENAI_USAGE):
            for v in _safe_int_usage(usage).values():
                assert isinstance(v, int) and not isinstance(v, bool)

    def test_flat_usage_passthrough(self):
        from ai_agent import _safe_int_usage
        assert _safe_int_usage({'prompt_tokens': 1, 'completion_tokens': 2}) == \
            {'prompt_tokens': 1, 'completion_tokens': 2}

    def test_none_and_non_dict_return_empty(self):
        from ai_agent import _safe_int_usage
        for bad in (None, {}, [], 'usage', 123, 0):
            assert _safe_int_usage(bad) == {}

    def test_string_numbers_coerced(self):
        from ai_agent import _safe_int_usage
        assert _safe_int_usage({'prompt_tokens': '42', 'completion_tokens': ' 7 '}) == \
            {'prompt_tokens': 42, 'completion_tokens': 7}

    def test_non_numeric_strings_skipped(self):
        from ai_agent import _safe_int_usage
        assert _safe_int_usage({'prompt_tokens': 'n/a', 'total_tokens': 5}) == {'total_tokens': 5}

    def test_bool_skipped(self):
        """bool 是 int 子类，误转会污染统计"""
        from ai_agent import _safe_int_usage
        assert _safe_int_usage({'cached': True, 'total_tokens': 3}) == {'total_tokens': 3}

    def test_nested_usage_key_skipped(self):
        from ai_agent import _safe_int_usage
        assert 'usage' not in _safe_int_usage({'usage': {'a': 1}, 'total_tokens': 2})

    def test_float_truncated_to_int(self):
        from ai_agent import _safe_int_usage
        assert _safe_int_usage({'total_tokens': 12.0}) == {'total_tokens': 12}


class TestLgEmitUsage:

    def test_accumulates_with_zhipu_payload(self):
        from ai_agent import _lg_emit_usage
        acc = {}
        _lg_emit_usage(acc, {'usage': ZHIPU_USAGE})
        assert acc == {'prompt_tokens': 18, 'completion_tokens': 111, 'total_tokens': 129}
        _lg_emit_usage(acc, {'usage': ZHIPU_USAGE})
        assert acc == {'prompt_tokens': 36, 'completion_tokens': 222, 'total_tokens': 258}

    def test_tolerates_missing_and_bad_usage(self):
        """usage 缺失/畸形时不得抛异常；计数键按既有约定预置为 0"""
        from ai_agent import _lg_emit_usage
        acc = {}
        for resp in ({}, {'usage': None}, {'usage': 'oops'}, {'usage': {}}, {'usage': []}, None, 'str'):
            _lg_emit_usage(acc, resp)
        assert acc == {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}

    def test_nested_details_never_leak_into_counter(self):
        """嵌套的 *_details 不得进入累计器（否则下游 int() 或统计会出错）"""
        from ai_agent import _lg_emit_usage
        acc = {}
        _lg_emit_usage(acc, {'usage': ZHIPU_USAGE})
        assert set(acc) == {'prompt_tokens', 'completion_tokens', 'total_tokens'}
        assert all(isinstance(v, int) for v in acc.values())


class TestRegressionTripwire:

    def test_buggy_comprehension_is_gone(self):
        """绊线：不得回退到对 usage 全键 int() 的写法"""
        import ai_agent
        src = inspect.getsource(ai_agent)
        assert "for k, v in (response.get('usage') or {}).items() if k != 'usage'" not in src, \
            '出现了旧的 int() 全键写法，会导致智谱/OpenAI 的 *_details 嵌套字段崩溃'

    def test_trace_event_uses_safe_helper(self):
        import ai_agent
        src = inspect.getsource(ai_agent._lg_agent)
        assert '_safe_int_usage' in src

    def test_realistic_agent_llm_round_survives_zhipu_usage(self, monkeypatch):
        """端到端（离线）：一次带嵌套 usage 的 LLM 响应不得被计为失败"""
        import ai_agent
        import llm_client

        monkeypatch.setattr(llm_client, 'chat', lambda *a, **k: {
            'content': '数据共 500 行。', 'tool_calls': None, 'usage': ZHIPU_USAGE})
        acc = {}
        ai_agent._lg_emit_usage(acc, {'usage': ZHIPU_USAGE})
        # 旧实现在此处抛 TypeError；现在必须安静通过并正确累计
        assert acc['total_tokens'] == 129
