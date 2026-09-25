# -*- coding: utf-8 -*-
"""llm_client 思考兜底防泄漏（2026-09-19）

reasoning 系模型（glm-5.2 等）content 为空、只有 reasoning 时，旧兜底把思考当正文直出。
但思考的第一步常是"念题"（「1. 分析请求：* 用户问题：…* 角色设定：…」），
直出 = 把系统提示词结构泄漏给用户（实测 1343 字符原文）。
本文件锁定：复述型思考必须被拦下、正常兜底必须保留。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm_client as lc  # noqa: E402

ECHO = ("1. **分析请求：**\n * **用户问题：** 什么是夏普比率\n"
        " * **角色设定：** 靠谱的金融顾问\n * **回答风格：** 自然口语\n")
GOOD = "夏普比率衡量每承担一单位风险能换来多少超额收益，公式是 (Rp - Rf) / σp。"


# ---------------- 判定函数 ----------------

def test_looks_like_task_echo_detects_prompt_repeat():
    assert lc.looks_like_task_echo(ECHO) is True


def test_looks_like_task_echo_allows_normal_answer():
    assert lc.looks_like_task_echo(GOOD) is False
    # 单个元标签不算命中 —— 避免误伤正常业务表述
    assert lc.looks_like_task_echo("用户问题是关于久期凸性的，下面逐层解释：") is False


def test_echo_markers_cover_the_observed_leak():
    """实测泄漏文本里的元标签必须都在词表内。"""
    for m in ('分析请求', '角色设定', '用户问题：', '回答风格'):
        assert m in lc._ECHO_MARKERS


# ---------------- 流式路径 ----------------

class _FakeResp:
    status_code = 200
    encoding = 'utf-8'
    text = ''

    def __init__(self, lines=None, payload=None):
        self._lines = lines or []
        self._payload = payload

    def iter_lines(self, decode_unicode=True):
        return iter(self._lines)

    def json(self):
        return self._payload


def _delta_line(reasoning='', content=''):
    d = {'choices': [{'delta': {}}]}
    if reasoning:
        d['choices'][0]['delta']['reasoning_content'] = reasoning
    if content:
        d['choices'][0]['delta']['content'] = content
    return 'data: ' + json.dumps(d, ensure_ascii=False)


def test_stream_blocks_reasoning_echo(monkeypatch):
    """只有复述型思考时：不直出思考，改用关思考重试拿到的正文。"""
    lines = [_delta_line(reasoning=ECHO), 'data: [DONE]']
    monkeypatch.setattr(lc, '_post_with_retry', lambda *a, **k: _FakeResp(lines=lines))
    monkeypatch.setattr(lc, '_plain_answer_retry', lambda *a, **k: GOOD)
    out = ''.join(lc._stream_with_provider(
        'sensenova', {'model': 'glm-5.2'}, [{'role': 'user', 'content': 'q'}]))
    assert out == GOOD
    assert '角色设定' not in out
    assert '分析请求' not in out


def test_stream_drops_echo_when_retry_fails(monkeypatch):
    """重试仍拿不到干净正文时返回空 —— 宁可空也不泄漏提示词结构。"""
    lines = [_delta_line(reasoning=ECHO), 'data: [DONE]']
    monkeypatch.setattr(lc, '_post_with_retry', lambda *a, **k: _FakeResp(lines=lines))
    monkeypatch.setattr(lc, '_plain_answer_retry', lambda *a, **k: '')
    out = ''.join(lc._stream_with_provider(
        'sensenova', {'model': 'glm-5.2'}, [{'role': 'user', 'content': 'q'}]))
    assert out == ''


def test_stream_keeps_useful_reasoning_fallback(monkeypatch):
    """非复述型思考仍走原兜底 —— 不能把"防空回复"这个能力一起杀掉。"""
    lines = [_delta_line(reasoning=GOOD), 'data: [DONE]']
    monkeypatch.setattr(lc, '_post_with_retry', lambda *a, **k: _FakeResp(lines=lines))

    def _boom(*a, **k):
        raise AssertionError('非复述型思考不应触发重试')

    monkeypatch.setattr(lc, '_plain_answer_retry', _boom)
    out = ''.join(lc._stream_with_provider(
        'sensenova', {'model': 'glm-5.2'}, [{'role': 'user', 'content': 'q'}]))
    assert out == GOOD


def test_stream_prefers_real_content(monkeypatch):
    """有正文时照常直出，不受防护影响。"""
    lines = [_delta_line(content='夏普比率是'), _delta_line(content='性价比指标。'), 'data: [DONE]']
    monkeypatch.setattr(lc, '_post_with_retry', lambda *a, **k: _FakeResp(lines=lines))
    out = ''.join(lc._stream_with_provider(
        'sensenova', {'model': 'glm-5.2'}, [{'role': 'user', 'content': 'q'}]))
    assert out == '夏普比率是性价比指标。'


# ---------------- 非流式路径 ----------------

def test_chat_blocks_reasoning_echo(monkeypatch):
    payload = {'choices': [{'message': {'content': '', 'reasoning_content': ECHO}}]}
    monkeypatch.setattr(lc, '_post_with_retry', lambda *a, **k: _FakeResp(payload=payload))
    monkeypatch.setattr(lc, '_plain_answer_retry', lambda *a, **k: GOOD)
    r = lc._chat_with_provider('sensenova', {'model': 'glm-5.2'},
                               [{'role': 'user', 'content': 'q'}])
    assert r['content'] == GOOD
    assert r.get('reasoning_echo_blocked') is not True
    assert r.get('reasoning_retry') is True


def test_chat_marks_blocked_when_retry_fails(monkeypatch):
    payload = {'choices': [{'message': {'content': '', 'reasoning_content': ECHO}}]}
    monkeypatch.setattr(lc, '_post_with_retry', lambda *a, **k: _FakeResp(payload=payload))
    monkeypatch.setattr(lc, '_plain_answer_retry', lambda *a, **k: '')
    r = lc._chat_with_provider('sensenova', {'model': 'glm-5.2'},
                               [{'role': 'user', 'content': 'q'}])
    assert not str(r['content']).strip()
    assert r.get('reasoning_echo_blocked') is True


def test_chat_keeps_useful_reasoning_fallback(monkeypatch):
    payload = {'choices': [{'message': {'content': '', 'reasoning_content': GOOD}}]}
    monkeypatch.setattr(lc, '_post_with_retry', lambda *a, **k: _FakeResp(payload=payload))
    r = lc._chat_with_provider('sensenova', {'model': 'glm-5.2'},
                               [{'role': 'user', 'content': 'q'}])
    assert r['content'] == GOOD
    assert r.get('reasoning_fallback') is True


def test_chat_never_touches_reasoning_when_tool_calls_present(monkeypatch):
    """有 tool_calls 时空 content 是正常语义，不得兜底。"""
    payload = {'choices': [{'message': {'content': '', 'reasoning_content': ECHO,
                                        'tool_calls': [{'id': '1',
                                                        'function': {'name': 'f', 'arguments': '{}'}}]}}]}
    monkeypatch.setattr(lc, '_post_with_retry', lambda *a, **k: _FakeResp(payload=payload))
    r = lc._chat_with_provider('sensenova', {'model': 'glm-5.2'},
                               [{'role': 'user', 'content': 'q'}])
    assert not str(r['content']).strip()
    assert r.get('tool_calls')
    assert r.get('reasoning_fallback') is not True
