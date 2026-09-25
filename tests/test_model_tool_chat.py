# -*- coding: utf-8 -*-
"""单一模型工具调用入口：模型自行决定是否调用工具。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai_agent


def test_model_can_answer_without_being_forced_to_call_tools(monkeypatch):
    calls = []

    def fake_chat(messages, config, tools=None, tool_choice=None, max_tokens=None, **kw):
        calls.append({'messages': messages, 'tools': tools, 'tool_choice': tool_choice})
        return {'content': '直接回答', 'usage': {'total_tokens': 8}}

    monkeypatch.setattr(ai_agent.llm_client, 'chat', fake_chat)
    monkeypatch.setattr(ai_agent, '_build_openai_tools', lambda: [{'type': 'function'}])

    result = ai_agent.model_tool_chat('你好', {'provider': 'custom'})

    assert result['ok'] is True
    assert result['output'] == '直接回答'
    assert result['calls'] == []
    assert len(calls) == 1
    assert calls[0]['tools'] == [{'type': 'function'}]
    assert calls[0]['tool_choice'] == 'auto'


def test_model_tool_call_is_executed_and_returned_to_same_model(monkeypatch):
    model_calls = []

    def fake_chat(messages, config, tools=None, tool_choice=None, max_tokens=None, **kw):
        model_calls.append(messages)
        if len(model_calls) == 1:
            return {
                'content': '',
                'tool_calls': [{
                    'id': 'call_1', 'type': 'function',
                    'function': {'name': 'echo_test', 'arguments': '{"text":"abc"}'},
                }],
            }
        tool_messages = [m for m in messages if m.get('role') == 'tool']
        assert tool_messages[-1]['tool_call_id'] == 'call_1'
        assert tool_messages[-1]['content'] == 'result:abc'
        return {'content': '根据工具结果回答'}

    monkeypatch.setattr(ai_agent.llm_client, 'chat', fake_chat)
    monkeypatch.setattr(ai_agent, '_build_openai_tools', lambda: [{'type': 'function'}])
    monkeypatch.setattr(
        ai_agent.registry, 'get_tool',
        lambda name: {'params': ['text'], 'func': lambda text: 'result:' + text}
        if name == 'echo_test' else None,
    )

    result = ai_agent.model_tool_chat('调用工具', {'provider': 'custom'})

    assert result['ok'] is True
    assert result['output'] == '根据工具结果回答'
    assert result['calls'][0]['tool'] == 'echo_test'
    assert result['calls'][0]['status'] == 'ok'
    assert len(model_calls) == 2
