#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多Agent管线 · 阶段按需命中 —— 单测（2026-09-18 晚）

用户诉求："三个阶段不要全部都用，根据用户的实际需求，命中了这三个阶段中的哪一个，
就用哪个分析"。

覆盖：
  1) select_pipeline_stages 纯规则判定（职责关键词 / 显式全域 / 空集保底 / 数据文件补量化）；
  2) 边界不误伤（"完整的财务数据" ≠ "完整分析"）；
  3) PIPELINE_STAGE_ROUTING=0 总回退为三阶段全跑；
  4) 端到端：单阶段任务真的只调 1 次 agent_loop，且阶段编号、终稿 prompt、
     done.stage_plan 都必须与实际命中数对齐（不得对模型谎称"第 1/3 阶段"）。
"""
import agent_pipeline
from agent_pipeline import STAGE_DEFS, select_pipeline_stages

ALL = [s['name'] for s in STAGE_DEFS]


# ==================== 1) 纯规则判定 ====================

class TestSelectStages:

    def test_market_only(self):
        assert select_pipeline_stages('深度研究一下这个行业的竞争格局') == ['市场分析师']

    def test_quant_only(self):
        assert select_pipeline_stages('帮我做一下相关性检验和回归测算') == ['量化建模师']

    def test_risk_only(self):
        assert select_pipeline_stages('这个策略有什么合规风险和监管问题') == ['风控合规官']

    def test_multi_stage_keeps_defs_order(self):
        # "公司/估值" → 市场；"测算/回归" → 量化；结果顺序恒按 STAGE_DEFS，不按命中先后
        got = select_pipeline_stages('帮我测算一下这家公司的估值回归')
        assert got == ['市场分析师', '量化建模师']

    def test_explicit_full_scope_runs_all(self):
        for msg in ('全面分析电器行业', '帮我做一份完整的研究报告',
                    '做一份完整研究报告', '做个尽调', '完整报告，谢谢'):
            assert select_pipeline_stages(msg) == ALL, msg

    def test_full_scope_needs_target_word(self):
        # "完整"必须搭配 分析/研究/报告 等作业词才算全域要求——否则"完整的财务数据"会误触发全跑
        assert select_pipeline_stages('完整的财务数据') != ALL
        assert select_pipeline_stages('我需要全面的信息') != ALL

    def test_empty_falls_back_to_market(self):
        assert select_pipeline_stages('') == ['市场分析师']
        assert select_pipeline_stages(None) == ['市场分析师']
        assert select_pipeline_stages('研究报告') == ['市场分析师']

    def test_data_file_adds_quant(self):
        # 挂了数据文件 → 量化阶段有分析对象，即使消息里没提量化
        assert select_pipeline_stages('帮我做一份研究报告', 'clean.csv') == ['量化建模师']
        assert select_pipeline_stages('这个行业的竞争格局', 'clean.csv') == \
            ['市场分析师', '量化建模师']
        # 文件不改变"全域"语义
        assert select_pipeline_stages('全面分析', 'clean.csv') == ALL

    def test_three_way_hit_runs_all(self):
        assert select_pipeline_stages('风险、合规、行业、测算都看看') == ALL

    def test_routing_can_be_disabled(self, monkeypatch):
        monkeypatch.setattr(agent_pipeline, '_STAGE_ROUTING', False)
        assert select_pipeline_stages('研究报告') == ALL
        assert select_pipeline_stages('') == ALL


# ==================== 2) 端到端：按需命中真的少跑阶段 ====================

def _drive(monkeypatch, message, current_file=None):
    """离线跑一次管线：stub 掉 agent_loop 与 chat，回收阶段调用与终稿输入。"""
    import ai_agent
    import llm_client

    stage_calls, final_inputs = [], []

    def fake_agent_loop(user_message, llm_config=None, file_name=None,
                        max_iterations=3, session_id=None, progress_callback=None,
                        trace_run_id=None, **kw):
        stage_calls.append(user_message)
        return {'ok': True, 'output': f'产出{len(stage_calls)}', 'calls': [],
                'usage': {'total_tokens': 1}, 'elapsed_ms': 1}

    def fake_chat(messages, config=None, **kw):
        final_inputs.append(messages)
        return {'content': '# 终稿\n完整报告'}

    monkeypatch.setattr(ai_agent, 'agent_loop', fake_agent_loop)
    monkeypatch.setattr(llm_client, 'chat', fake_chat)

    events, done = [], None
    for evt in agent_pipeline.run_pipeline_stream(message, None, current_file, session_id='unit'):
        if evt.get('type') == 'done':
            done = evt['data']
        else:
            events.append(evt)
    return stage_calls, final_inputs, events, done


def test_single_stage_task_runs_one_stage(monkeypatch):
    """风险合规类任务 → 只跑风控合规官（不再陪跑市场与量化）。"""
    calls, finals, events, done = _drive(monkeypatch, '这个策略有什么合规风险和监管问题')

    assert len(calls) == 1, f'按需命中应只跑 1 个阶段，实际 {len(calls)}'
    assert '风控合规官' in calls[0]
    # 阶段编号必须与实际命中数对齐——谎称 "1/3" 会诱导模型去补不存在的阶段
    assert '第 1/1 个执行阶段' in calls[0]

    statuses = [e.get('message', '') for e in events if e.get('type') == 'status']
    assert any('阶段 1/1：风控合规官' in s for s in statuses), statuses
    assert any('命中 1 个阶段' in s for s in statuses), statuses

    assert done['stage_plan']['selected'] == ['风控合规官']
    assert done['stage_plan']['n'] == 1
    assert [m['stage'] for m in done['stages']] == ['风控合规官']
    assert '单阶段深度分析' in done['reasoning']

    # 终稿 prompt 不得再说"三个协作阶段"，否则模型会去补并不存在的阶段（编造来源）
    sys_msg = finals[0][0]['content']
    assert '三个协作阶段' not in sys_msg
    assert '风控合规官' in sys_msg


def test_three_stage_task_keeps_full_run(monkeypatch):
    """显式全域请求 → 仍是三阶段全跑（语义未退化）。"""
    calls, finals, events, done = _drive(monkeypatch, '全面分析电器行业')

    assert len(calls) == 3
    statuses = [e.get('message', '') for e in events if e.get('type') == 'status']
    assert any('阶段 1/3' in s for s in statuses), statuses
    assert done['stage_plan']['n'] == 3
    assert '3 阶段并行协作' in done['reasoning']

    sys_msg = finals[0][0]['content']
    for n in ALL:
        assert n in sys_msg, f'终稿 prompt 应点名全部命中阶段：缺 {n}'


def test_two_stage_task(monkeypatch):
    """行业竞争格局 + 数据文件 → 市场 + 量化，风控不陪跑。"""
    calls, _finals, _events, done = _drive(monkeypatch, '这个行业的竞争格局', 'clean.csv')

    assert len(calls) == 2
    assert done['stage_plan']['selected'] == ['市场分析师', '量化建模师']
    assert any('阶段 2/2' in (e.get('message') or '') for e in _events
               if e.get('type') == 'status')


def test_routing_disabled_runs_all_stages(monkeypatch):
    """PIPELINE_STAGE_ROUTING 关闭 → 回退改造前行为（三阶段全跑）。"""
    monkeypatch.setattr(agent_pipeline, '_STAGE_ROUTING', False)
    calls, _finals, _events, done = _drive(monkeypatch, '这个策略有什么合规风险')
    assert len(calls) == 3
    assert done['stage_plan']['n'] == 3


def test_stage_failure_not_counted_as_success(monkeypatch):
    """429 连败（ok=False + 降级提示文本）不得被判为成功阶段。

    真实事故（2026-09-18 晚真实运行）：风控合规官撞 429，agent_loop 返回 ok=False 且
    output 是一段 133 字的"配额耗尽"提示；旧判据 _ok = bool(reply) 判成成功 → degraded
    漏报，且该提示被当成阶段产出喂给终稿，终稿被写成"因限流无法分析"的错误说明书。
    """
    import ai_agent
    import llm_client

    said = []

    def fake_agent_loop(user_message, llm_config=None, file_name=None, max_iterations=3,
                        session_id=None, progress_callback=None, trace_run_id=None, **kw):
        return {'ok': False,
                'output': '⚠️ 当前模型连续多次调用失败（可能已耗尽免费配额或触发限流）。',
                'calls': [], 'usage': {'total_tokens': 1}, 'elapsed_ms': 1,
                'error': 'API error 429: {"error":{"code":"429003"}}'}

    def fake_chat(messages, config=None, **kw):
        said.append(messages)
        return {'content': '# 终稿'}

    monkeypatch.setattr(ai_agent, 'agent_loop', fake_agent_loop)
    monkeypatch.setattr(llm_client, 'chat', fake_chat)

    done = None
    for evt in agent_pipeline.run_pipeline_stream('这个策略有什么合规风险', None, None,
                                                  session_id='unit'):
        if evt.get('type') == 'done':
            done = evt['data']

    assert done['stages'][0]['ok'] is False, 'ok=False 的阶段不得判为成功'
    assert done['degraded'] is True, '阶段失败必须报 degraded'
    assert done['failed_stages'] == ['风控合规官']

    # 降级提示不得作为"阶段产出"进入终稿输入
    user_msg = said[0][1]['content']
    assert '耗尽免费配额' not in user_msg, '失败阶段的降级提示被当成了产出喂给终稿'
    assert '本阶段未产出文字' in user_msg


def test_stage_failure_reports_reason_from_end_status(monkeypatch):
    """agent_loop 的 result 契约不含 error 时，阶段失败原因回落 end_status。

    真实事故（2026-09-18 晚）：风控合规官 finalize 阶段 LLM 调用失败（429），
    ok=False 但 result 无 error 字段 → 前端只看到"阶段失败"，看不到为什么。
    """
    import ai_agent
    import llm_client

    def fake_agent_loop(user_message, llm_config=None, file_name=None, max_iterations=3,
                        session_id=None, progress_callback=None, trace_run_id=None, **kw):
        # 模拟真实契约：终稿 LLM 失败 → ok=False、无 error 字段、带 end_status
        return {'ok': False, 'output': '（兜底拼接的工具返回）', 'calls': [],
                'usage': {'total_tokens': 1}, 'elapsed_ms': 1,
                'end_status': 'finalize_failed'}

    monkeypatch.setattr(ai_agent, 'agent_loop', fake_agent_loop)
    monkeypatch.setattr(llm_client, 'chat', lambda *a, **k: {'content': '# 终稿'})

    events, done = [], None
    for evt in agent_pipeline.run_pipeline_stream('这个策略有什么合规风险', None, None,
                                                  session_id='unit'):
        if evt.get('type') == 'done':
            done = evt['data']
        else:
            events.append(evt)

    assert done['stages'][0]['ok'] is False
    assert done['stages'][0]['error'] == 'finalize_failed', '失败原因必须随 stages 透传'

    statuses = [e.get('message', '') for e in events if e.get('type') == 'status']
    assert any('finalize_failed' in s for s in statuses), statuses


def test_successful_stage_has_no_error_text(monkeypatch):
    """成功阶段不得把 end_status='done' 当成错误文案透出。"""
    calls, _finals, events, done = _drive(monkeypatch, '全面分析电器行业')

    assert all(m['ok'] for m in done['stages']), done['stages']
    assert all(m['error'] == '' for m in done['stages']), done['stages']
    statuses = [e.get('message', '') for e in events if e.get('type') == 'status']
    assert not any('done' in s for s in statuses), statuses
