# -*- coding: utf-8 -*-
"""⚡快速模式在「分析」场景下必须走快速档 agent（关思考 + 收紧迭代预算）。

背景（2026-09-20）：修复前快速模式与深度思考在分析请求上**传给 agent 的参数逐项相同**
（file / depth / max_iter / agent_msg 全一致），因此"快速"没有任何提速 ——
用户实测两种模式耗时几乎一样。修复后：
  - fast=True  → reasoning_effort='none' + max_iter=_FAST_AGENT_MAX_ITER + 跳过辩论与管线
  - fast=False → 逐字节保持原行为（深度思考零回归）

另修：快速模式 + 带文件分析此前被 `if file_mentioned and not fast:` 整段跳过，
直接穿透到末尾 `yield {'type':'done','data':result}` —— 而 result 是规则引擎原生结构
（无 reply 字段，实测 reply_len=0），用户拿到空回复。现在同样走快速档 agent。
"""
import pytest

import ai_assistant as aa

_CFG = {'provider': 'sensenova', 'model': 'glm-5.2'}
_MSG = '分析一下新能源汽车行业的投资价值'
_DATA = 'finance_ml_final_project_data.csv'


@pytest.fixture
def _isolate():
    """清空会话状态，避免上一轮历史/缓存影响分流判定。"""
    aa._chat_manager.history.clear()
    aa._chat_manager.context.clear()
    try:
        aa._router_cache.clear()
    except Exception:
        pass
    yield aa
    aa._chat_manager.history.clear()
    aa._chat_manager.context.clear()


def _stub_agent_factory(seen):
    def _stub(message, llm_config, current_file, depth=None, session_id=None, fast=False):
        seen['fast'] = fast
        yield {'type': 'done', 'data': {'ok': True, 'reply': '[stub-agent]', 'source': 'agent'}}
    return _stub


def _collect(gen):
    done, deltas = None, []
    events = {}
    for evt in gen:
        events[evt['type']] = events.get(evt['type'], 0) + 1
        if evt['type'] == 'done':
            done = evt['data']
        elif evt['type'] == 'delta':
            deltas.append(evt.get('text') or (evt.get('data') or {}).get('text') or '')
    return done, deltas, events


# ======================================================================
# 1. _prepare_agent_run：快速档参数契约
# ======================================================================
def test_fast_disables_thinking_and_shrinks_iterations():
    """快速档必须关闭全链路思考并收紧迭代预算。"""
    prep = aa._prepare_agent_run(_MSG, _CFG, None, fast=True)
    assert prep['eff_cfg'].get('reasoning_effort') == 'none', '快速档必须关闭模型思考'
    assert prep['max_iter'] == aa._FAST_AGENT_MAX_ITER, '快速档必须收紧迭代预算'


def test_think_params_unchanged():
    """深度模式参数零回归：不塞 reasoning_effort，预算仍是动态 auto。"""
    prep = aa._prepare_agent_run(_MSG, _CFG, None, fast=False)
    assert 'reasoning_effort' not in prep['eff_cfg'], '深度模式不得被塞 reasoning_effort'
    assert prep['max_iter'] == 'auto', '深度模式必须保持动态预算'


def test_non_sensenova_provider_not_given_reasoning_effort():
    """不支持 reasoning_effort 的 provider 不得收到该字段（会 400）。"""
    prep = aa._prepare_agent_run(_MSG, {'provider': 'deepseek', 'model': 'deepseek-v4-flash'},
                                 None, fast=True)
    assert 'reasoning_effort' not in prep['eff_cfg']
    assert prep['max_iter'] == aa._FAST_AGENT_MAX_ITER, '预算收紧与 provider 无关'


def test_fast_must_not_pollute_caller_config():
    """快速档必须拷贝后再改，不能污染调用方传入的 llm_config。"""
    cfg = dict(_CFG)
    aa._prepare_agent_run(_MSG, cfg, None, fast=True)
    assert 'reasoning_effort' not in cfg


def test_fast_handles_none_config():
    """快速档在 llm_config=None 时不得崩溃（前端未配置模型的场景）。"""
    prep = aa._prepare_agent_run(_MSG, None, None, fast=True)
    assert isinstance(prep['eff_cfg'], dict)
    assert prep['max_iter'] == aa._FAST_AGENT_MAX_ITER


# ======================================================================
# 2. 流式分流：快速模式必须把 fast=True 传下去，且不触发管线
# ======================================================================
def test_stream_fast_passes_flag_to_agent(_isolate, monkeypatch):
    import llm_client as lc
    monkeypatch.setattr(lc, 'chat', lambda *a, **k: {'content': '[stub]'})
    monkeypatch.setattr(lc, 'chat_stream', lambda *a, **k: iter(['[stub]']))

    seen = {}
    monkeypatch.setattr(aa, '_stream_agent_analysis', _stub_agent_factory(seen))

    done, _, _ev = _collect(_isolate.stream_chat_message(_MSG, _CFG,
                                                         current_file=None, mode='fast'))
    assert seen.get('fast') is True, '快速模式必须把 fast=True 传给 agent'
    assert done and done.get('source') == 'agent'


def test_stream_think_keeps_fast_false(_isolate, monkeypatch):
    import llm_client as lc
    monkeypatch.setattr(lc, 'chat', lambda *a, **k: {'content': '[stub]'})
    monkeypatch.setattr(lc, 'chat_stream', lambda *a, **k: iter(['[stub]']))

    seen = {}
    monkeypatch.setattr(aa, '_stream_agent_analysis', _stub_agent_factory(seen))

    _collect(_isolate.stream_chat_message(_MSG, _CFG, current_file=None, mode='think'))
    assert seen.get('fast') is False, '深度思考不得被误标为快速档'


def test_stream_fast_skips_multi_agent_pipeline(_isolate, monkeypatch):
    """含管线触发词时，快速模式也必须跳过 3 阶段管线（管线是为深度模式准备的）。"""
    import llm_client as lc
    import agent_pipeline as ap
    monkeypatch.setattr(lc, 'chat', lambda *a, **k: {'content': '[stub]'})
    monkeypatch.setattr(lc, 'chat_stream', lambda *a, **k: iter(['[stub]']))

    called = {'pipe': 0}

    def _fake_pipe(*a, **k):
        called['pipe'] += 1
        yield {'type': 'done', 'data': {'ok': True, 'reply': '[stub-pipe]',
                                        'source': 'agent_pipeline'}}
    monkeypatch.setattr(ap, 'run_pipeline_stream', _fake_pipe)

    seen = {}
    monkeypatch.setattr(aa, '_stream_agent_analysis', _stub_agent_factory(seen))

    _collect(_isolate.stream_chat_message('全面分析一下中国的新能源汽车行业', _CFG,
                                          current_file=None, mode='fast'))
    assert called['pipe'] == 0, '快速模式不得触发多 Agent 管线'
    assert seen.get('fast') is True


# ======================================================================
# 3. 回归：快速模式 + 带文件分析不得返回空回复（修复前 reply_len=0）
# ======================================================================
def test_fast_with_file_never_returns_empty_reply(_isolate, monkeypatch):
    import llm_client as lc
    monkeypatch.setattr(lc, 'chat', lambda *a, **k: {'content': '[stub]'})
    monkeypatch.setattr(lc, 'chat_stream', lambda *a, **k: iter(['[stub]']))

    seen = {}
    monkeypatch.setattr(aa, '_stream_agent_analysis', _stub_agent_factory(seen))

    done, deltas, _ev = _collect(_isolate.stream_chat_message('帮我分析当前文件', _CFG,
                                                              current_file=_DATA, mode='fast'))
    reply = ((done or {}).get('reply') or ''.join(deltas)).strip()
    assert reply, '快速模式带文件分析不得返回空回复（修复前穿透为无 reply 的 rules 结果）'
    assert seen.get('fast') is True, '带文件分析也必须走快速档'


# ======================================================================
# 4. agent_loop 快速档：终稿预算收紧（终稿占 LLM 累计耗时一半以上）
# ======================================================================
def test_agent_loop_accepts_fast_and_defaults_false():
    """agent_loop 必须接受 fast 参数且默认 False（深度思考与多 Agent 管线零回归）。"""
    import inspect
    import ai_agent
    sig = inspect.signature(ai_agent.agent_loop)
    assert 'fast' in sig.parameters, 'agent_loop 必须暴露 fast 形参'
    assert sig.parameters['fast'].default is False, 'fast 默认必须为 False'


def test_final_token_budget_by_mode():
    """快速档终稿预算必须收紧，非快速档与缺省值必须保持 4800。"""
    import ai_agent
    assert ai_agent._final_token_budget({'fast': True}) == ai_agent._FAST_FINAL_MAX_TOKENS
    assert ai_agent._final_token_budget({'fast': False}) == ai_agent._FINAL_MAX_TOKENS
    assert ai_agent._final_token_budget({}) == ai_agent._FINAL_MAX_TOKENS
    assert ai_agent._final_token_budget(None) == ai_agent._FINAL_MAX_TOKENS
    assert ai_agent._FAST_FINAL_MAX_TOKENS < ai_agent._FINAL_MAX_TOKENS


def test_agent_loop_writes_fast_into_cfg():
    """fast 必须落进 cfg —— _lg_finish 靠 cfg['fast'] 决定预算与输出风格。"""
    import os
    import ai_agent
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'ai_agent.py'), encoding='utf-8').read()
    assert "'fast': bool(fast)" in src, 'fast 必须写入 cfg（否则终稿预算不生效）'


def test_finalize_skips_self_correction_in_fast_mode():
    """快速档必须跳过双 Pass 自校验（额外一次 LLM 调用，与"快"相悖）。"""
    import os
    import ai_assistant
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'ai_assistant.py'), encoding='utf-8').read()
    assert 'if (not fast and _depth == ' in src, '快速档必须跳过终稿自校验'
