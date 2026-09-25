# -*- coding: utf-8 -*-
"""llm_client.py 单测：配置解析 / 重试 / usage 透传（mock 网络请求）"""
import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm_client
from llm_client import (_resolve_api_key, _resolve_url, _compute_timeout,
                        _build_payload, chat, chat_stream)


def test_resolve_api_key_env_priority(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'env-key')
    # 环境变量优先于前端传入
    assert _resolve_api_key('deepseek', {'apiKey': 'frontend-key'}) == 'env-key'
    # 无环境变量时使用前端兜底
    monkeypatch.delenv('DEEPSEEK_API_KEY')
    assert _resolve_api_key('deepseek', {'apiKey': 'frontend-key'}) == 'frontend-key'


def test_resolve_url_upgrades_ollama_legacy():
    # 旧 /api/generate 地址自动升级到 OpenAI 兼容端点
    cfg = {'apiUrl': 'http://localhost:11434/api/generate'}
    assert _resolve_url('ollama', cfg) == 'http://localhost:11434/v1/chat/completions'
    # 已是 v1 端点保持不变
    cfg2 = {'apiUrl': 'http://localhost:11434/v1/chat/completions'}
    assert _resolve_url('ollama', cfg2) == 'http://localhost:11434/v1/chat/completions'
    # 默认端点
    assert _resolve_url('deepseek', {}) == 'https://api.deepseek.com/v1/chat/completions'


def test_minimax_uses_official_openai_compatible_endpoint_and_env_key(monkeypatch):
    """minimax 是 sensenova（商汤日日新）的别名——历史 .env 里的小名。
    API Key 同时认 MINIMAX_API_KEY 与 SENSENOVA_API_KEY。"""
    monkeypatch.setenv('MINIMAX_API_KEY', 'minimax-env-key')
    assert _resolve_url('minimax', {}) == 'https://token.sensenova.cn/v1/chat/completions'
    assert _resolve_api_key('minimax', {'apiKey': 'frontend-key'}) == 'minimax-env-key'
    # 兼容回退：MINIMAX_API_KEY 未设时，SENSENOVA_API_KEY 也应兜底
    monkeypatch.delenv('MINIMAX_API_KEY')
    monkeypatch.setenv('SENSENOVA_API_KEY', 'sensenova-env-key')
    assert _resolve_api_key('minimax', {'apiKey': ''}) == 'sensenova-env-key'


def test_custom_openai_compatible_base_url_is_expanded():
    assert _resolve_url('custom', {'apiUrl': 'https://api.minimaxi.com/v1'}) == (
        'https://api.minimaxi.com/v1/chat/completions'
    )
    assert _resolve_url('custom', {
        'apiUrl': 'https://api.minimaxi.com/v1/chat/completions'
    }) == 'https://api.minimaxi.com/v1/chat/completions'


def test_compute_timeout():
    assert _compute_timeout(None) == 30
    assert _compute_timeout(1000) == 30 + 1000 * 0.02


class _FakeResp:
    def __init__(self, status=200, json_data=None):
        self.status_code = status
        self._json = json_data or {}
        self.text = 'fake'

    def json(self):
        return self._json


def test_chat_returns_content_and_usage():
    resp = _FakeResp(200, {
        'choices': [{'message': {'content': '你好', 'tool_calls': [
            {'function': {'name': 'x', 'arguments': '{}'}}]}}],
        'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15},
    })
    with mock.patch('llm_client.requests.post', return_value=resp) as m:
        result = chat([{'role': 'user', 'content': 'hi'}],
                      {'provider': 'deepseek', 'apiKey': 'k', 'model': 'm'})
        assert result['content'] == '你好'
        assert len(result['tool_calls']) == 1
        assert result['usage']['total_tokens'] == 15
        # 请求头带 Authorization
        headers = m.call_args.kwargs['headers']
        assert headers['Authorization'] == 'Bearer k'


def test_chat_retries_on_429():
    resp_429 = _FakeResp(429)
    resp_ok = _FakeResp(200, {'choices': [{'message': {'content': 'ok'}}]})
    with mock.patch('llm_client.requests.post', side_effect=[resp_429, resp_ok]) as m:
        result = chat([{'role': 'user', 'content': 'x'}],
                      {'provider': 'deepseek', 'apiKey': 'k', 'model': 'm'})
        assert result['content'] == 'ok'
        assert m.call_count == 2  # 429 后重试一次


def test_chat_passes_finish_reason():
    """finish_reason 透传（2026-09-18）：'length' = 被 max_tokens 截断，上层据此续写。"""
    resp = _FakeResp(200, {'choices': [{'message': {'content': '半截'},
                                        'finish_reason': 'length'}]})
    with mock.patch('llm_client.requests.post', return_value=resp):
        result = chat([{'role': 'user', 'content': 'x'}],
                      {'provider': 'deepseek', 'apiKey': 'k', 'model': 'm'})
        assert result['finish_reason'] == 'length'


def test_chat_no_finish_reason_when_absent():
    resp = _FakeResp(200, {'choices': [{'message': {'content': 'ok'}}]})
    with mock.patch('llm_client.requests.post', return_value=resp):
        result = chat([{'role': 'user', 'content': 'x'}],
                      {'provider': 'deepseek', 'apiKey': 'k', 'model': 'm'})
        assert 'finish_reason' not in result


def test_build_payload_reasoning_effort_passthrough():
    """reasoning_effort 仅在显式设置时透传（防不支持该字段的 provider 400）。"""
    msgs = [{'role': 'user', 'content': 'x'}]
    p = _build_payload('sensenova', {'provider': 'sensenova', 'model': 'glm-5.2',
                                     'reasoning_effort': 'none'}, msgs)
    assert p['reasoning_effort'] == 'none'
    p2 = _build_payload('sensenova', {'provider': 'sensenova', 'model': 'glm-5.2'}, msgs)
    assert 'reasoning_effort' not in p2


def test_chat_stream_yields_tokens():
    # 模拟 SSE 数据流
    lines = [
        'data: {"choices":[{"delta":{"content":"你"}}]}',
        'data: {"choices":[{"delta":{"content":"好"}}]}',
        'data: [DONE]',
        '',
    ]
    resp = _FakeResp(200)
    with mock.patch('llm_client.requests.post', return_value=resp) as m:
        m.return_value.iter_lines = lambda decode_unicode=True: iter(lines)
        tokens = list(chat_stream([{'role': 'user', 'content': 'x'}],
                                  {'provider': 'deepseek', 'apiKey': 'k', 'model': 'm'}))
        assert tokens == ['你', '好']


def test_connection_error_raises_after_retries():
    import requests
    with mock.patch('llm_client.requests.post',
                    side_effect=requests.exceptions.ConnectionError('connection refused')):
        with pytest.raises(RuntimeError):
            chat([{'role': 'user', 'content': 'x'}],
                 {'provider': 'deepseek', 'apiKey': 'k', 'model': 'm'})


def test_chat_fallback_to_cloud_when_ollama_down(monkeypatch):
    """Ollama 连接失败时自动回退到环境变量可用的云端 provider"""
    import requests
    monkeypatch.setenv('SENSENOVA_API_KEY', 'sn-key')
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)

    def fake_post(url, **kwargs):
        if '11434' in url:
            raise requests.exceptions.ConnectionError('refused')
        # 回退后的 sensenova 请求
        assert 'sensenova' in url
        return _FakeResp(200, {'choices': [{'message': {'content': '云端回答'}}],
                               'usage': {'total_tokens': 5}})

    with mock.patch('llm_client.requests.post', side_effect=fake_post) as m:
        result = chat([{'role': 'user', 'content': 'hi'}], {'provider': 'ollama'})
        assert result['content'] == '云端回答'
        assert result.get('fallback') is True
        assert result.get('used_provider') == 'sensenova'
        # ollama 连接失败重试 3 次（共 4 次，max_retries=3）+ sensenova 成功 1 次 = 5 次请求
        assert m.call_count == 5


def test_no_fallback_when_no_cloud_key(monkeypatch):
    """没有任何云端 key 时，连接失败应直接抛错"""
    import requests
    for k in ('SENSENOVA_API_KEY', 'DEEPSEEK_API_KEY', 'OPENAI_API_KEY',
              'GROQ_API_KEY', 'ZHIPU_API_KEY'):
        monkeypatch.delenv(k, raising=False)
    with mock.patch('llm_client.requests.post',
                    side_effect=requests.exceptions.ConnectionError('refused')):
        with pytest.raises(RuntimeError):
            chat([{'role': 'user', 'content': 'x'}], {'provider': 'ollama'})


def test_chat_stream_fallback(monkeypatch):
    """流式调用同样支持 provider 回退"""
    import requests
    monkeypatch.setenv('SENSENOVA_API_KEY', 'sn-key')
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)

    lines = ['data: {"choices":[{"delta":{"content":"好"}}]}', 'data: [DONE]', '']
    resp = _FakeResp(200)

    def fake_post(url, **kwargs):
        if '11434' in url:
            raise requests.exceptions.ConnectionError('refused')
        r = _FakeResp(200)
        r.iter_lines = lambda decode_unicode=True: iter(lines)
        return r

    with mock.patch('llm_client.requests.post', side_effect=fake_post):
        tokens = list(chat_stream([{'role': 'user', 'content': 'x'}],
                                  {'provider': 'ollama'}))
        assert tokens == ['好']
