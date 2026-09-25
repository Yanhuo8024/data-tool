# -*- coding: utf-8 -*-
"""nlq 路由贯通 + 多Agent管线可观测性 回归（2026-09-17）。

修复背景（均为离线动态复现确认，非静态推断）：

- **P0**：`nlq`（自然语言查数）动作在 sync（`chat_actions`）与 stream（第 27xx 行元组）
  两条链路**都没有分派分支** → 直接穿透到函数末尾 `return result`，而 result 对 nlq
  不产生 reply → 用户拿到**空回复**。
  实证：`帮我看看收益率` / `前五名是谁` / `帮我统计一下缺失值的个数` 均 reply_len=0，
  且未调用 LLM（source=rules）。
- **P1**：`nlq` 不在 `_NO_WEB_INTENTS` 白名单，而 `_WEB_REQUIRED_KW` 含「统计/数据/对比/排名」
  等宽泛词 → `帮我统计一下缺失值的个数`（纯本地查询）被误判为需联网。
- **P1**：本地**可计算**的句子同样被「统计」误判联网 —— `帮我做个描述统计` 明明
  `real_compute.infer_compute_intent` 返回 `describe`，却走了 web_search 并联网失败。
- **P1**：`agent_pipeline` 的 `stages` 仅 `{stage, chars}`，阶段失败时前端只看到
  "本阶段未产出文字"，无从判断哪一步挂了；且 done 的 `ok` **恒为 True**（阶段全失败也报成功）。
- **P2**：`head` 工具 schema 漏声明 `file`（describe/overview/columns 等同类工具均已声明）。

约束：本文件所有 stub 均通过 `monkeypatch` 安装（禁止模块级 stub —— 否则污染整个
pytest session，表现为"单跑通过、全量失败"）；不创建也不删除任何临时文件。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """隔离外网 / LLM，并清空会话与路由缓存，避免用例间互相污染。"""
    import llm_client
    import web_search

    monkeypatch.setattr(
        llm_client, 'chat',
        lambda messages, config=None, tools=None, tool_choice='auto', max_tokens=None:
        {'content': '【MOCK-LLM】', 'usage': {}})

    def _fake_stream(messages, config=None, max_tokens=None):
        yield '【MOCK-LLM】'

    monkeypatch.setattr(llm_client, 'chat_stream', _fake_stream)
    monkeypatch.setattr(web_search, 'web_search', lambda q, top_k=5: {'ok': True, 'results': []})
    sys.modules['web_search'] = web_search

    import ai_assistant
    monkeypatch.setattr(ai_assistant, '_finance_live_context', lambda *a, **k: None)

    # 防难例回流写盘（生产闭环文件在 TESTING 下本应关闭，这里再显式兜一层）
    import intent_router
    monkeypatch.setattr(intent_router, '_CASES_REFLOW_ENABLED', False, raising=False)

    from conversation_memory import get_memory
    ai_assistant._chat_manager.history.clear()
    ai_assistant._chat_manager.context.clear()
    ai_assistant._router_cache.clear()
    get_memory().reset()
    yield ai_assistant
    ai_assistant._chat_manager.history.clear()
    ai_assistant._chat_manager.context.clear()
    ai_assistant._router_cache.clear()
    get_memory().reset()


_CFG = {'provider': 'zhipu', 'model': 'glm-4.7-flash'}


# ======================================================================
# P0：nlq 不得返回空回复
# ======================================================================
@pytest.mark.parametrize('msg', [
    '帮我看看收益率',
    '前五名是谁',
    '帮我统计一下缺失值的个数',
    '数据里有多少条记录',
])
def test_nlq_sync_never_returns_empty_reply(_isolate, monkeypatch, msg):
    """nlq 消息在同步链路必须给出非空回复（修复前 reply_len=0）。"""
    ai_assistant = _isolate
    # 无数据载体 → 应给明确引导（而不是空白）
    res = ai_assistant.process_chat_message(msg, _CFG, current_file=None, mode='think')
    reply = (res or {}).get('reply') or ''
    assert reply.strip(), f'{msg!r} 不得返回空回复（P0）'


def test_nlq_stream_never_returns_empty_reply(_isolate):
    """流式链路同样不得空回（修复前 stream 的 reply_len 也是 0）。"""
    ai_assistant = _isolate
    done, deltas = None, []
    for evt in ai_assistant.stream_chat_message('帮我看看收益率', _CFG,
                                                current_file=None, mode='think'):
        if evt['type'] == 'done':
            done = evt['data']
        elif evt['type'] == 'delta':
            deltas.append(evt.get('text', ''))
    reply = ((done or {}).get('reply') or ''.join(deltas)).strip()
    assert reply, 'nlq 流式链路不得返回空回复（P0）'


def test_nlq_without_file_guides_user(_isolate):
    """无数据载体的 nlq：应给出可执行引导（选文件 / 给金融标的），而不是静默或编造。"""
    ai_assistant = _isolate
    res = ai_assistant.process_chat_message('帮我看看收益率', _CFG, current_file=None, mode='think')
    reply = res.get('reply') or ''
    assert '数据' in reply or '文件' in reply, '无文件时应引导用户提供数据载体'


def test_nlq_with_file_injects_data_brief(_isolate, monkeypatch):
    """有文件且无专用计算器时，必须把「真实数据摘要」注入 LLM 上下文（防凭记忆编数字）。"""
    ai_assistant = _isolate
    monkeypatch.setattr(ai_assistant, '_nlq_data_brief',
                        lambda f, **k: '# 当前数据（真实结构）\n- 列名：销售额, 成本', raising=False)

    captured = []
    import llm_client
    monkeypatch.setattr(
        llm_client, 'chat',
        lambda messages, config=None, tools=None, tool_choice='auto', max_tokens=None:
        (captured.append(messages), {'content': '【MOCK-LLM】', 'usage': {}})[1])

    res = ai_assistant.process_chat_message('帮我看看收益率', _CFG,
                                            current_file='_not_exist_probe.csv', mode='think')
    joined = str(captured)
    assert (res.get('reply') or '').strip(), '有文件的 nlq 不得空回'
    assert '真实结构' in joined, '必须把数据摘要注入 LLM 上下文（P1）'


# ======================================================================
# P1：联网闸门（nlq 与可计算句不得误联网）
# ======================================================================
def test_nlq_local_query_is_not_web_required(_isolate):
    """nlq + 非金融 → 不该联网（修复前因 _WEB_REQUIRED_KW 的「统计」被误判联网）。"""
    ai_assistant = _isolate
    router = {'intent': 'nlq', 'finance': {'is_finance': False}}
    assert ai_assistant._needs_web_search('帮我统计一下缺失值的个数', router) is False


def test_nlq_finance_query_keeps_web_option(_isolate):
    """nlq + 金融属性（行情/估值）→ 仍保留联网判定（不能被一刀切禁掉）。"""
    ai_assistant = _isolate
    router = {'intent': 'nlq', 'finance': {'is_finance': True}}
    # '股价' 命中时效性关键词 → 应允许联网
    assert ai_assistant._needs_web_search('看看这只股票的股价', router) is True


def test_computable_query_is_not_web_required(_isolate):
    """本地可计算的句子必须走真实计算，不得联网。"""
    ai_assistant = _isolate
    import real_compute
    assert real_compute.infer_compute_intent('帮我做个描述统计') == 'describe'
    assert ai_assistant._needs_web_search('帮我做个描述统计', {}) is False


def test_intent_router_marks_nlq(_isolate):
    """确保这批句子确实被判为 nlq —— 否则上面的 nlq 断言会失去意义。"""
    import intent_router
    for msg in ('帮我看看收益率', '前五名是谁', '帮我统计一下缺失值的个数'):
        assert intent_router.classify_intent(msg)['intent'] == 'nlq', msg


# ======================================================================
# P1：多Agent管线阶段可观测性
# ======================================================================
def test_pipeline_stages_report_failure(monkeypatch):
    """阶段失败必须可见：stages 带 ok/error，且 done 不再是"假成功"。"""
    import llm_client
    import ai_agent
    import agent_pipeline

    monkeypatch.setattr(
        llm_client, 'chat',
        lambda messages, config=None, tools=None, tool_choice='auto', max_tokens=None:
        {'content': '【终稿】合成报告', 'usage': {'total_tokens': 10}})

    def fake_loop(user_message=None, llm_config=None, file_name=None,
                  max_iterations=4, session_id=None, progress_callback=None,
                  trace_run_id=None, **kw):
        # 三阶段并行（2026-09-18）：调用完成顺序不定，必须按阶段名判定失败目标
        if '量化建模师' in (user_message or ''):
            return {'ok': False, 'error': 'API error 429: rate limit exceeded', 'output': ''}
        return {'ok': True, 'output': '阶段产出内容', 'usage': {'total_tokens': 5}}

    monkeypatch.setattr(ai_agent, 'agent_loop', fake_loop, raising=False)

    done = None
    for evt in agent_pipeline.run_pipeline_stream('帮我做一份完整的研究报告',
                                                  {'provider': 'zhipu', 'model': 'glm-4.7-flash'},
                                                  None):
        if evt['type'] == 'done':
            done = evt['data']

    assert done is not None, '管线必须产出 done 事件'
    assert done['degraded'] is True, '有阶段失败必须标记 degraded（否则是假成功）'
    assert done['end_status'] == 'partial', '有阶段失败时 end_status 应为 partial'
    assert done['failed_stages'] == ['量化建模师'], '失败阶段必须被点名'

    by_stage = {s['stage']: s for s in done['stages']}
    assert by_stage['量化建模师']['ok'] is False
    assert '429' in by_stage['量化建模师']['error'], '失败原因必须随 stages 透传'
    assert by_stage['市场分析师']['ok'] is True
    assert 'provider' in by_stage['市场分析师'], 'stages 应带执行模型信息'


def test_pipeline_all_success_is_not_degraded(monkeypatch):
    """全部阶段成功时不得误报 degraded。"""
    import llm_client
    import ai_agent
    import agent_pipeline

    monkeypatch.setattr(
        llm_client, 'chat',
        lambda messages, config=None, tools=None, tool_choice='auto', max_tokens=None:
        {'content': '【终稿】', 'usage': {}})
    monkeypatch.setattr(
        ai_agent, 'agent_loop',
        lambda **kw: {'ok': True, 'output': '产出', 'usage': {}}, raising=False)

    done = None
    for evt in agent_pipeline.run_pipeline_stream('做一份完整研究报告',
                                                  {'provider': 'zhipu', 'model': 'm'}, None):
        if evt['type'] == 'done':
            done = evt['data']
    assert done['degraded'] is False
    assert done['end_status'] == 'ok'
    assert done['failed_stages'] == []


# ======================================================================
# P2：工具 schema 完整性
# ======================================================================
def test_head_tool_schema_declares_file():
    """head 的 schema 必须声明 file（此前漏了，同类工具都声明了）。"""
    import ai_agent
    schema = ai_agent._TOOL_PARAM_SCHEMA.get('head', {})
    assert 'file' in schema, 'head 的 schema 缺 file 声明（P2）'
    assert 'n' in schema, 'head 应保留 n 参数'
    assert schema['file'].get('required') is False, 'file 由后端注入，不应标必填'
