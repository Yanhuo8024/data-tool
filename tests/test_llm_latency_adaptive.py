# -*- coding: utf-8 -*-
"""LLM「思考时间过长」优化回归（2026-09-17）

覆盖三项根因修复：
  ① 固定 8s 节流 → **自适应节流**（健康态零额外空等，限流态按需抬升）
  ② Retry-After 健壮解析 + 429 学习回路（单次调用只抬升一档）
  ③ 终稿由"非流式静默等待" → **流式增量上报**
     （含工具调用 JSON 首块守卫、空流/异常回退、无回调回退非流式）
     + SSE 层把 delta 提升为顶层事件

隔离约定：全部用 pytest `monkeypatch` 安装 stub（结束自动还原），
不使用 `tmp_path`、不做任何删除（沙箱批量删除守卫）。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm_client as lc

KEY = '_t_latency_probe'


@pytest.fixture(autouse=True)
def _clean_throttle_state():
    """每个用例前后清掉探针 provider 的节流状态，避免跨用例污染。"""
    for d in (lc._ADAPT_INTERVAL, lc._OK_STREAK, lc._LAST_CLOUD_CALL):
        d.pop(KEY, None)
    yield
    for d in (lc._ADAPT_INTERVAL, lc._OK_STREAK, lc._LAST_CLOUD_CALL):
        d.pop(KEY, None)


class _FakeResp:
    def __init__(self, status=200, headers=None, text=''):
        self.status_code = status
        self.headers = headers or {}
        self.encoding = None
        self.text = text


# ============================================================
# ① 自适应节流
# ============================================================

def test_base_interval_is_lightweight():
    """健康态基础间隔必须轻量（旧的固定 8s 已废弃）。"""
    assert lc._CLOUD_MIN_INTERVAL <= 3.0
    assert lc._CLOUD_MAX_INTERVAL >= lc._CLOUD_MIN_INTERVAL
    assert lc._CLOUD_DECAY_AFTER >= 1


def test_no_wait_when_request_slower_than_interval(monkeypatch):
    """请求本身比基础间隔还慢时不应附加空等（旧设计会白等 间隔-请求耗时）。"""
    slept = []
    monkeypatch.setattr(lc.time, 'sleep', lambda s: slept.append(s))
    net = lc._CLOUD_MIN_INTERVAL + 1.0
    for _ in range(4):
        lc._throttle_cloud(KEY)
        lc._LAST_CLOUD_CALL[KEY] -= net        # 模拟请求耗时
    assert slept == [], f'健康态不应空等，实际 {slept}'


def test_waits_only_the_shortfall(monkeypatch):
    """请求很快时只等差额，而非全额固定间隔。"""
    slept = []
    monkeypatch.setattr(lc.time, 'sleep', lambda s: slept.append(round(s, 3)))
    lc._throttle_cloud(KEY)                    # 首次调用无历史 → 立即放行
    assert slept == []
    lc._LAST_CLOUD_CALL[KEY] -= 0.5            # 上一次只花了 0.5s
    lc._throttle_cloud(KEY)
    assert len(slept) == 1
    assert slept[0] == pytest.approx(lc._CLOUD_MIN_INTERVAL - 0.5, abs=0.05)


def test_rate_limit_escalates_monotonically_then_caps():
    seq = [lc.note_rate_limited(KEY) for _ in range(6)]
    assert seq[0] == pytest.approx(lc._CLOUD_MIN_INTERVAL * 2)
    assert seq == sorted(seq), '抬升应单调不减'
    assert seq[-1] == lc._CLOUD_MAX_INTERVAL, '必须封顶'
    assert lc._current_interval(KEY) == lc._CLOUD_MAX_INTERVAL
    assert lc._OK_STREAK.get(KEY) == 0, '被限流后连续成功计数应清零'


def test_rate_limit_honours_retry_after_and_ignores_garbage():
    lc.note_rate_limited(KEY)
    assert lc.note_rate_limited(KEY, 12) == 12.0
    bad = lc.note_rate_limited(KEY, 'Wed, 21 Oct 2015 07:28:00 GMT')
    assert bad >= lc._CLOUD_MIN_INTERVAL, '非法 Retry-After 不应破坏抬升'
    assert lc._current_interval(KEY) == bad


def test_success_decays_back_to_base_and_clears_entry():
    assert lc.note_rate_limited(KEY, 20) == 20.0
    for _ in range(lc._CLOUD_DECAY_AFTER * 15):
        lc.note_success(KEY)
    assert KEY not in lc._ADAPT_INTERVAL, '衰减到底后应移除动态档'
    assert lc._current_interval(KEY) == lc._CLOUD_MIN_INTERVAL


# ============================================================
# ② Retry-After 解析与 429 学习回路
# ============================================================

def test_retry_after_parsing_covers_float_and_garbage():
    assert lc._retry_after_seconds(_FakeResp(headers={'Retry-After': '3'})) == 3.0
    # 旧实现用 str(ra).isdigit()，小数会被误判为不可解析 → 丢掉服务端给定时长
    assert lc._retry_after_seconds(_FakeResp(headers={'Retry-After': ' 1.5 '})) == 1.5
    assert lc._retry_after_seconds(
        _FakeResp(headers={'Retry-After': 'Wed, 21 Oct 2015 07:28:00 GMT'})) is None
    assert lc._retry_after_seconds(_FakeResp(headers={'Retry-After': '-2'})) is None
    assert lc._retry_after_seconds(_FakeResp(headers={})) is None


def test_post_with_retry_escalates_once_per_call(monkeypatch):
    """一次逻辑调用内即使多次 429 也只抬升一档，避免连翻多档造成过长空等。"""
    monkeypatch.setattr(lc.time, 'sleep', lambda s: None)
    hits = {'n': 0}

    def _fake_post(url, headers=None, json=None, timeout=None):
        hits['n'] += 1
        return _FakeResp(429, {'Retry-After': '1'})

    monkeypatch.setattr(lc.requests, 'post', _fake_post)
    resp = lc._post_with_retry('http://x', {}, {}, 5, max_retries=3, provider=KEY)
    assert resp.status_code == 429
    assert hits['n'] == 4, '应为 1 次首调 + 3 次重试'
    assert lc._current_interval(KEY) == pytest.approx(lc._CLOUD_MIN_INTERVAL * 2)


def test_post_with_retry_notes_success(monkeypatch):
    monkeypatch.setattr(lc.requests, 'post',
                        lambda *a, **k: _FakeResp(200, {}))
    lc._OK_STREAK[KEY] = 0
    lc._post_with_retry('http://x', {}, {}, 5, provider=KEY)
    assert lc._OK_STREAK.get(KEY) == 1, '成功应回报给衰减计数器'


# ============================================================
# ③ 终稿流式
# ============================================================

import ai_agent as ag          # noqa: E402
import ai_assistant as A       # noqa: E402

_CFG = {'provider': 'zhipu', 'model': 'glm-4.7-flash'}


def test_final_answer_streams_without_char_loss(monkeypatch):
    monkeypatch.setattr(lc, 'chat_stream',
                        lambda m, c=None, max_tokens=None: iter(['\n\n', '# 报告\n', '结论：+12%。']))

    def _no_chat(*a, **k):
        raise AssertionError('有回调时应走流式，不应调用非流式')

    monkeypatch.setattr(lc, 'chat', _no_chat)
    got = []
    text, resp = ag._final_answer_call([{'role': 'user', 'content': 'x'}], _CFG,
                                       cb=lambda e: got.append(e))
    assert resp is None, '流式路径应返回 response=None'
    assert got and all(e['type'] == 'delta' for e in got)
    assert ''.join(e['text'] for e in got) == text, '上报的增量必须拼回完整正文（不丢字）'


def test_final_answer_guard_blocks_tool_call_json(monkeypatch):
    """首块即工具调用 JSON 时不得上报 delta。

    原因：前端在 done 阶段只在"最终结果更长且同源"时补差额，不会覆盖已渲染内容，
    若把 JSON 噪声流式渲染出去，用户会看到噪声且报告丢失。
    """
    monkeypatch.setattr(lc, 'chat_stream',
                        lambda m, c=None, max_tokens=None: iter(['{"tool": "desc', 'ribe"}']))
    got = []
    text, resp = ag._final_answer_call([{'role': 'user', 'content': 'x'}], _CFG,
                                       cb=lambda e: got.append(e))
    assert got == [], '工具调用 JSON 不应被流式上报'
    assert text == '{"tool": "describe"}', '正文仍须完整返回（交由 parse_calls 处理）'


def test_looks_like_tool_call_heuristic():
    assert ag._looks_like_tool_call('{"tool": "x"}') is True
    assert ag._looks_like_tool_call('  [{ }]') is True
    assert ag._looks_like_tool_call('# 分析报告') is False
    assert ag._looks_like_tool_call('\n\n结论如下') is False
    assert ag._looks_like_tool_call('') is False
    assert ag._looks_like_tool_call('   ') is False


def test_final_answer_falls_back_on_empty_stream(monkeypatch):
    monkeypatch.setattr(lc, 'chat_stream', lambda m, c=None, max_tokens=None: iter([]))
    monkeypatch.setattr(lc, 'chat',
                        lambda m, c=None, max_tokens=None: {'content': 'NONSTREAM'})
    text, resp = ag._final_answer_call([{'role': 'user', 'content': 'x'}], _CFG,
                                       cb=lambda e: None)
    assert text == 'NONSTREAM' and resp is not None


def test_final_answer_falls_back_on_stream_error(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError('stream unsupported')

    monkeypatch.setattr(lc, 'chat_stream', _boom)
    monkeypatch.setattr(lc, 'chat',
                        lambda m, c=None, max_tokens=None: {'content': 'NONSTREAM'})
    text, resp = ag._final_answer_call([{'role': 'user', 'content': 'x'}], _CFG,
                                       cb=lambda e: None)
    assert text == 'NONSTREAM' and resp is not None, '流式失败必须回退，成功率不降'


def test_final_answer_uses_non_stream_without_callback(monkeypatch):
    monkeypatch.setattr(lc, 'chat',
                        lambda m, c=None, max_tokens=None: {'content': 'SYNC'})

    def _no_stream(*a, **k):
        raise AssertionError('无回调（同步链路）不应走流式')

    monkeypatch.setattr(lc, 'chat_stream', _no_stream)
    text, resp = ag._final_answer_call([{'role': 'user', 'content': 'x'}], _CFG, cb=None)
    assert text == 'SYNC' and resp is not None


def test_lg_finish_routes_through_streaming_helper():
    """终稿节点必须经由 _final_answer_call，否则退回静默非流式等待。
    （2026-09-18：_lg_finish 改传 fin_cfg —— 终稿关思考 + 预算提到 4800，路由不变。）"""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'ai_agent.py'), encoding='utf-8').read()
    assert '_final_answer_call(messages, fin_cfg, cb' in src


def test_stream_agent_analysis_forwards_delta_as_top_level_event(monkeypatch):
    """SSE 层应把 delta 提升为顶层事件（前端据此边生成边渲染）。"""
    # **kw 必须保留：_prepare_agent_run 新增 fast 形参（⚡快速档）后，
    # 签名过窄的桩会抛 TypeError 并伪装成业务回归。
    monkeypatch.setattr(A, '_prepare_agent_run', lambda m, c, f, **kw: {
        'eff_cfg': _CFG, 'max_iter': 2, 'agent_msg': 'msg'})
    monkeypatch.setattr(A, '_maybe_debate', lambda *a, **k: None)
    monkeypatch.setattr(A, '_finalize_agent_result', lambda r, *a, **k: {
        'ok': True, 'reply': '终稿', 'source': 'agent',
        'intent': {'action': 'analyze_data', 'params': {}, 'confidence': 0.9, 'raw_text': 'x'},
        'plan': {'steps': [], 'context': ''}})

    def _fake_loop(user_message, llm_config, max_iterations, file_name,
                   session_id, progress_callback=None, **kw):
        if progress_callback:
            progress_callback({'status': 'running', 'iteration': 1, 'message': '推理中...'})
            progress_callback({'type': 'delta', 'text': 'A'})
            progress_callback({'type': 'tool_start', 'tool': 'describe'})
            progress_callback({'type': 'delta', 'text': 'B'})
        return {'ok': True, 'output': '终稿'}

    monkeypatch.setattr(ag, 'agent_loop', _fake_loop)
    events = list(A._stream_agent_analysis('分析', _CFG, 'f.csv'))

    deltas = [e['data']['text'] for e in events if e.get('type') == 'delta']
    assert deltas == ['A', 'B'], 'delta 应作为顶层 SSE 事件按序下发'
    assert not [e for e in events if e.get('type') == 'progress'
                and (e.get('data') or {}).get('type') == 'delta'], \
        'delta 不应再被包在 progress 里'
    assert any(e.get('type') == 'done' for e in events)
