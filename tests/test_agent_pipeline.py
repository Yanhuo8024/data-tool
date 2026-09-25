"""多 Agent 协作管线 / 语义摘要 / 记忆接入测试"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


# ==================== 管线触发条件（成本护栏） ====================

def test_pipeline_trigger_keywords():
    from agent_pipeline import should_use_pipeline
    assert should_use_pipeline('帮我做一份宁德时代的研究报告') is True
    assert should_use_pipeline('全面分析电器行业') is True
    assert should_use_pipeline('做个尽调') is True
    assert should_use_pipeline('分析一下这个数据') is False   # 普通分析走单体 Agent
    assert should_use_pipeline('') is False


# ==================== 三阶段并行与终稿合成 ====================

def test_pipeline_three_stages_parallel(monkeypatch):
    """显式全域请求（"全面分析…"）→ 3 个阶段**并行**执行，全部完成后终稿汇总三阶段产出。

    注：2026-09-18 晚起阶段按需命中——用"全面分析"这类显式全域词才会三阶段全跑；
    普通"研究报告/深度研究"只跑命中的阶段（见 test_pipeline_stage_routing.py）。"""
    import threading
    import time as _time
    import llm_client
    import agent_pipeline
    import ai_assistant

    monkeypatch.setattr(ai_assistant, '_finance_live_context', lambda *a, **k: None, raising=False)
    monkeypatch.setattr(ai_assistant, '_compute_note', lambda *a, **k: '', raising=False)

    stage_calls = []
    recs = []          # (start, end) 每次模拟 LLM 调用的执行区间
    lock = threading.Lock()

    fast_kwargs = []   # 各阶段收到的提速开关（no_plan / tool_names / slim_prompt）

    def fake_agent_loop(user_message, llm_config=None, file_name=None,
                        max_iterations=3, session_id=None, progress_callback=None,
                        trace_run_id=None, **kw):
        with lock:
            stage_calls.append(user_message)
            n = len(stage_calls)
            fast_kwargs.append(dict(kw))
        _s, _e = _time.time(), None
        _time.sleep(0.15)   # 模拟 LLM 耗时，便于验证三阶段确实重叠执行
        _e = _time.time()
        with lock:
            recs.append((_s, _e))
        if progress_callback:
            progress_callback({'type': 'tool_end', 'tool': 'web_search', 'tool_status': 'ok',
                               'elapsed_ms': 100, 'params': {'query': 'x'}})
        return {'ok': True, 'output': f'【第{n}阶段产出】相关分析。',
                'calls': [], 'usage': {'total_tokens': 10}, 'elapsed_ms': 100}

    monkeypatch.setattr(ai_agent_module(), 'agent_loop', fake_agent_loop)

    final_inputs = []

    def fake_chat(messages, config=None, **kw):
        final_inputs.append(messages[-1]['content'])
        return {'content': '# 最终报告\n（首席合成版）'}

    monkeypatch.setattr(llm_client, 'chat', fake_chat)

    events, done = [], None
    for evt in agent_pipeline.run_pipeline_stream('全面分析宁德时代', None, None, session_id='t'):
        if evt['type'] == 'done':
            done = evt['data']
        else:
            events.append(evt)

    assert len(stage_calls) == 3, f"应执行 3 个阶段，实际 {len(stage_calls)}"
    # 并行语义：三阶段消息都只含各自职责 + 用户任务，不再包含前序阶段产出
    for msg in stage_calls:
        assert '宁德时代' in msg
    assert any('市场分析师' in m for m in stage_calls)
    assert any('量化建模师' in m for m in stage_calls)
    assert any('风控合规官' in m for m in stage_calls)
    # 并行提速证据（区间重叠度，排除 begin_run 首调的小时级轨迹清理等管线外噪声）：
    # 串行时 span ≈ work 之和；并行时 span 应明显小于 work 之和（3 × 0.15s）。
    work = sum(e - s for s, e in recs)
    span = max(e for _, e in recs) - min(s for _, s in recs)
    assert span < work * 0.8, \
        f'三阶段应并行执行：span={span:.2f}s, work={work:.2f}s（疑似串行）'
    # 终稿：合成调用汇总了全部三阶段产出
    assert done['source'] == 'agent_pipeline'
    assert '首席合成' in done['reply']
    assert final_inputs and all(f'【第{n}阶段产出】' in final_inputs[0] for n in (1, 2, 3)), \
        '终稿输入必须包含全部三阶段产出'
    assert done['elapsed_ms'] >= 0
    # 进度事件包含阶段状态与工具卡片
    statuses = [e.get('message', '') for e in events if e.get('type') == 'status']
    assert any('阶段 1/3' in s for s in statuses)
    assert any((e.get('data') or {}).get('type') == 'tool_end'
            for e in events if e.get('type') == 'progress')


def test_pipeline_stages_receive_speedup_switches(monkeypatch):
    """三阶段提速：每阶段都必须收到 no_plan / slim_prompt / 按职责裁剪的 tool_names。

    这三项是"三个中间阶段全面加速"的实现载体：
      no_plan    —— 省掉每阶段 1~2 次重复规划 LLM 往返；
      slim_prompt—— 省掉技能正文/知识点库/领域框架的每轮重注入；
      tool_names —— 只下发本阶段职责所需工具（32 → 8~17 个）。
    """
    import agent_pipeline
    import ai_agent
    import llm_client

    got = []

    def fake_agent_loop(user_message, llm_config=None, file_name=None,
                        max_iterations=3, session_id=None, progress_callback=None,
                        trace_run_id=None, **kw):
        got.append(kw)
        return {'ok': True, 'output': '产出', 'usage': {'total_tokens': 1}}

    monkeypatch.setattr(ai_agent, 'agent_loop', fake_agent_loop)
    monkeypatch.setattr(llm_client, 'chat', lambda *a, **k: {'content': '终稿'})

    for _ in agent_pipeline.run_pipeline_stream('全面分析这个行业', None, None, session_id='t'):
        pass

    assert len(got) == 3, got
    for kw in got:
        assert kw.get('no_plan') is True
        assert kw.get('slim_prompt') is True
        tools = kw.get('tool_names') or []
        assert tools, '每阶段必须有工具白名单'
        assert len(tools) < 32, f'必须裁剪工具集，实际 {len(tools)}'
    # 三阶段的白名单必须各不相同（按职责分工，而非同一份清单）
    assert len({tuple(kw['tool_names']) for kw in got}) == 3
    # 风控合规官只做核验，工具最少
    assert min(len(kw['tool_names']) for kw in got) <= 6


def test_pipeline_stage_fast_switch_can_be_disabled(monkeypatch):
    """PIPELINE_STAGE_FAST=0 → 回退到改造前行为（不传任何提速开关，零回归阀）"""
    import agent_pipeline
    import ai_agent
    import llm_client

    monkeypatch.setattr(agent_pipeline, '_STAGE_FAST', False)
    seen = []

    def fake_agent_loop(user_message, llm_config=None, file_name=None,
                        max_iterations=3, session_id=None, progress_callback=None,
                        trace_run_id=None, **kw):
        seen.append(kw)
        return {'ok': True, 'output': '产出', 'usage': {}}

    monkeypatch.setattr(ai_agent, 'agent_loop', fake_agent_loop)
    monkeypatch.setattr(llm_client, 'chat', lambda *a, **k: {'content': '终稿'})

    for _ in agent_pipeline.run_pipeline_stream('全面分析这个行业', None, None, session_id='t'):
        pass
    assert seen and all(kw == {} for kw in seen), seen


def ai_agent_module():
    import ai_agent
    return ai_agent


# ==================== 语义记忆摘要 ====================

def test_summary_llm_skipped_without_config():
    """无 llm_config → 不触发 LLM 摘要（零成本兜底）"""
    from conversation_memory import ConversationMemory
    mem = ConversationMemory()
    for i in range(4):
        mem.short_term.append({'message': f'q{i}', 'intent': 'analysis', 'entities': {}})
    assert mem.maybe_update_summary_llm('q', 'a', None) is False


def test_summary_llm_updates_when_turn_multiple(monkeypatch):
    """第 N 轮 + 有 llm_config → 异步 LLM 摘要更新"""
    import time
    import llm_client
    from conversation_memory import ConversationMemory

    mem = ConversationMemory()
    for i in range(4):
        mem.short_term.append({'message': f'分析标{i}', 'intent': 'analysis', 'entities': {}})

    class FakeMem(ConversationMemory):
        updated = None
        def update_summary(self, summary):
            FakeMem.updated = summary

    fmem = FakeMem()
    for i in range(4):
        fmem.short_term.append({'message': f'分析标{i}', 'intent': 'analysis', 'entities': {}})
    monkeypatch.setattr(llm_client, 'chat',
                        lambda *a, **kw: {'content': '已分析标0-标3，结论为正向'})

    assert fmem.maybe_update_summary_llm('q', 'a', {'provider': 'ollama'}) is True
    deadline = time.time() + 3
    while FakeMem.updated is None and time.time() < deadline:
        time.sleep(0.05)
    assert FakeMem.updated and '标0' in FakeMem.updated


def test_rule_based_summary_still_works():
    """规则式摘要兜底不受影响"""
    from conversation_memory import ConversationMemory
    mem = ConversationMemory()
    mem.short_term.append({'message': 'q1', 'intent': 'analysis', 'entities': {'target': ['茅台']}})
    mem.short_term.append({'message': 'q2', 'intent': 'nlq', 'entities': {}})
    s = mem.summarize_rule_based()
    assert 'analysis' in s and '茅台' in s


# ==================== 记忆接入主循环 ====================

def test_agent_prep_includes_recent_conclusions(monkeypatch):
    """会话内最近结论注入 Agent 准备阶段（记忆接进主循环，零额外 LLM 调用）"""
    import ai_assistant
    from ai_assistant import ChatMessage

    ai_assistant._chat_manager.history.append(
        ChatMessage(role='assistant', content='上一轮结论：兆易创新 ROE 23%，估值中性的测试结论'))
    monkeypatch.setattr(ai_assistant, '_finance_live_context', lambda *a, **k: None, raising=False)
    monkeypatch.setattr(ai_assistant, '_compute_note', lambda *a, **k: '', raising=False)
    monkeypatch.setattr(ai_assistant, '_build_expert_context', lambda *a, **k: '', raising=False)

    prep = ai_assistant._prepare_agent_run('继续分析', {'provider': 'ollama'}, None)
    assert '最近结论' in prep['agent_msg']
    assert '兆易创新' in prep['agent_msg']


# ==================== 终稿合成的 thinking 开关（2026-09-18 晚回归） ====================

def _run_pipeline_capturing_final(monkeypatch, base_cfg):
    """跑一遍管线（阶段用 stub），返回终稿 chat() 收到的 (config, max_tokens)。"""
    import llm_client
    import agent_pipeline

    monkeypatch.setattr(ai_agent_module(), 'agent_loop', lambda *a, **k: {
        'ok': True, 'output': '【阶段产出】', 'calls': [], 'usage': {}, 'elapsed_ms': 1})

    captured = {}

    def fake_chat(messages, config=None, **kw):
        captured['config'] = dict(config or {})
        captured['max_tokens'] = kw.get('max_tokens')
        return {'content': '# 最终报告'}

    monkeypatch.setattr(llm_client, 'chat', fake_chat)
    for _ in agent_pipeline.run_pipeline_stream('全面分析宁德时代', base_cfg, None):
        pass
    return captured


def test_final_synthesis_disables_thinking_for_sensenova(monkeypatch):
    """回归（2026-09-18 晚实测）：管线终稿改用 glm-5.2（reasoning 系）后，未关思考会让
    思考独占 max_tokens 预算 —— 正文只剩开头 29 字符（"# 宁德时代…\\n\\n**首席"）。
    必须与单体 agent 终稿同口径：sensenova 显式 reasoning_effort='none' 且预算 4800。"""
    cap = _run_pipeline_capturing_final(monkeypatch, {'provider': 'sensenova', 'model': 'glm-5.2'})
    assert cap['config'].get('reasoning_effort') == 'none', '终稿合成必须关闭 sensenova 思考'
    assert cap['max_tokens'] == 4800, '终稿预算应与单体 agent 口径一致（4800）'


def test_final_synthesis_keeps_non_sensenova_config(monkeypatch):
    """非 sensenova provider 不得被硬塞 reasoning_effort（部分 provider 收到该字段会 400）。"""
    cap = _run_pipeline_capturing_final(monkeypatch, {'provider': 'zhipu', 'model': 'glm-4-flash'})
    assert 'reasoning_effort' not in cap['config']
