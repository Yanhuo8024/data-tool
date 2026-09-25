# -*- coding: utf-8 -*-
"""ai_agent.py 工具缓存/调用解析 与 ai_assistant_routes 配置清洗 单测"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_agent import ToolCache, parse_calls, _clean_final_output, _ensure_final_text


# ==================== 终稿工具 JSON 清理（防裸 JSON 泄漏给用户）====================

def test_clean_final_output_nested_json_removed():
    """嵌套 params 的工具 JSON 必须整体移除，不留孤儿右括号（历史缺陷）"""
    raw = ('{"tool": "web_search", "params": {"query": "伽马数据 2025年1-6月 中国游戏市场 实际销售收入 1472.67亿元"}}\n\n'
           '# 游戏行业分析\n\n2025年上半年实际销售收入**1472.67亿元**。')
    cleaned = _clean_final_output(raw)
    assert '"tool"' not in cleaned
    assert '{' not in cleaned and '}' not in cleaned, f"残留孤儿括号: {cleaned!r}"
    assert '游戏行业分析' in cleaned and '1472.67' in cleaned


def test_clean_final_output_multiline_json_removed():
    raw = ('{\n  "tool": "web_search",\n  "params": {"query": "腾讯 2025 Q3 游戏业务收入 网易 财报"}\n}\n\n'
           '分析正文：行业景气度回升。')
    cleaned = _clean_final_output(raw)
    assert cleaned.strip() == '分析正文：行业景气度回升。'


def test_clean_final_output_multiple_calls_removed():
    raw = (
        '{"tool": "web_search", "params": {"query": "2025 游戏版号 发放数量 国产 进口 过审"}}\n'
        '{"tool": "web_search", "params": {"query": "2025 小游戏市场规模 增速 出海 自研游戏 海外收入"}}\n\n'
        '以下是综合分析……'
    )
    cleaned = _clean_final_output(raw)
    assert '"tool"' not in cleaned and 'query' not in cleaned
    assert '以下是综合分析' in cleaned


def test_clean_final_output_keeps_normal_text():
    """普通文本（含非工具花括号、非 tool 的 JSON）不能误伤"""
    t = '正常分析{花括号}保留，另外 {"not_tool": 1} 也保留'
    assert _clean_final_output(t) == t


def test_ensure_final_text_fallback_when_only_tool_calls():
    """模型到上限只输出工具调用 → 清理后为空 → 用已执行查询生成诚实兜底（非裸 JSON）"""
    calls = [
        {'tool': 'web_search', 'params': {'query': '游戏版号 2025 过审'}},
        {'tool': 'web_search', 'params': {'query': '小游戏出海'}, 'cached': True},
    ]
    out = _ensure_final_text('{"tool": "web_search", "params": {"query": "小游戏出海"}}', calls)
    assert '已完成的资料查询' in out
    assert 'web_search(游戏版号 2025 过审)' in out
    assert '[缓存]' in out
    assert '"tool"' not in out


def test_ensure_final_text_passes_clean_analysis():
    out = _ensure_final_text('## 结论\n行业景气度回升。', [])
    assert out == '## 结论\n行业景气度回升。'


# ==================== 无文件分析：重试提示与终稿模板分模式 ====================

def test_no_file_prompts_defined():
    """无文件模式的三段提示词必须存在且语义正确（行业研究简报，非 DID 面板模板）"""
    from prompts import get_prompt
    assert 'web_search' in get_prompt('no_tools_no_file')
    p = get_prompt('refinement_no_file')
    assert '行业规模与增长' in p and '竞争格局' in p
    assert '不得编造' in p
    ff = get_prompt('force_final_no_file')
    assert '不要输出任何 JSON' in ff and '待补充' in ff


def test_no_file_refinement_template_has_no_action_table():
    """无文件终稿模板不应包含数据文件的【建议操作】ACTION 行要求（那是文件分析模板的内容）"""
    from prompts import get_prompt
    p = get_prompt('refinement_no_file')
    # 明确要求"不要输出 ACTION 行"
    assert '不要输出 [ACTION' in p or '无文件模式下不要输出' in p


# ==================== 简写技能调用（"skill名 {json}"）====================

def test_parse_calls_bare_skill_shorthand():
    """『industry-research {"keyword": "游戏行业"}』简写必须被识别为工具调用，
    而不是当成正文返回给用户（历史泄漏缺陷）"""
    calls = parse_calls('industry-research {"keyword": "游戏行业"}')
    assert calls == [{'tool': 'industry-research', 'params': {'keyword': '游戏行业'}}]


def test_parse_calls_shorthand_glued_with_text():
    """简写与用户消息粘连在同一行（实测泄漏格式）也必须被识别"""
    raw = 'company-analysis {"symbol": "002594.SZ"}请帮我分析宁德时代'
    calls = parse_calls(raw)
    assert calls == [{'tool': 'company-analysis', 'params': {'symbol': '002594.SZ'}}]


def test_clean_final_output_strips_glued_shorthand():
    """终稿里与文字粘连的简写调用必须被剥离，正文保留"""
    raw = 'company-analysis {"symbol": "002594.SZ"}请帮我分析宁德时代'
    assert _clean_final_output(raw) == '请帮我分析宁德时代'


def test_parse_calls_salvages_truncated_json():
    """免费模型铺垫后 JSON 写到一半被截断 → 必须从残缺文本抢救出工具调用"""
    truncated = (
        '好的，我来为您系统地分析游戏行业。首先查询市场规模：\n'
        '{"tool": "web_search", "params": {"query": "2025 中国游戏市场 实际销售收入"'
    )  # JSON 未闭合
    calls = parse_calls(truncated)
    assert calls and calls[0]['tool'] == 'web_search'
    assert '游戏市场' in calls[0]['params'].get('query', '')


def test_parse_calls_no_false_positive_on_prose():
    """普通分析正文（无 "tool" 键）不能被抢救逻辑误判成工具调用"""
    assert parse_calls('## 游戏行业分析\n2024年市场规模约3000亿元，"收益"："良好"。') == []


def test_clean_final_output_strips_bare_skill_shorthand():
    """终稿里的裸简写行必须剥离；只剩简写时走诚实兜底"""
    cleaned = _clean_final_output('industry-research {"keyword": "游戏行业"}')
    assert cleaned == ''
    calls = [{'tool': 'run_skill', 'params': {'skill_name': 'industry-research'}}]
    out = _ensure_final_text('industry-research {"keyword": "游戏行业"}', calls)
    assert '已完成的资料查询' in out and '"keyword"' not in out


def test_agent_loop_executes_bare_skill_call(monkeypatch):
    """集成验证：模型简写调用技能 → 归一为 run_skill 真实执行（加载技能工作流），不落空"""
    import llm_client
    import ai_agent

    monkeypatch.setattr(ai_agent, 'MIN_SUCCESS_CALLS', 1)

    def fake_chat(messages, cfg, max_tokens=None, **kw):
        sys_text = messages[0]['content'] if messages else ''
        user_texts = [m['content'] for m in messages if m['role'] == 'user']
        last_user = user_texts[-1]
        if '规划器' in sys_text:
            return {'content': '{"steps": ["加载行业研究技能", "检索行业数据"]}', 'usage': {'total_tokens': 1}}
        if '停止调用工具' in last_user:
            return {'content': '## 一、行业规模与增长\n（基于技能工作流的分析正文）', 'usage': {'total_tokens': 1}}
        if not any('Tool:' in t for t in user_texts):
            # 模型用简写格式调用技能（非标准 JSON）
            return {'content': 'industry-research {"keyword": "游戏行业"}', 'usage': {'total_tokens': 1}}
        return {'content': '## 继续分析', 'usage': {'total_tokens': 1}}

    monkeypatch.setattr(llm_client, 'chat', fake_chat)

    result = ai_agent.agent_loop(user_message='请帮我分析游戏行业',
                                 llm_config={'provider': 'ollama'},
                                 max_iterations=2, file_name=None)

    assert result.get('ok') is True, result.get('output', '')[:200]
    calls = result.get('calls', [])
    assert calls and calls[0]['tool'] == 'run_skill', f"简写技能调用未被归一执行: {calls[:1]}"
    assert calls[0]['params'].get('skill_name') == 'industry-research'
    assert calls[0].get('status') == 'ok'
    assert '游戏行业' in calls[0]['params'].get('task', '')

def test_step_discipline_prompt_defined():
    from prompts import get_prompt
    p = get_prompt('step_discipline')
    assert '严禁跳过' in p and '资料获取' in p and '禁止事项' in p


def test_step_guard_message_content():
    from ai_agent import _step_guard_message
    msg = _step_guard_message(1, 2, plan=['查行业规模', '查竞争格局'])
    assert '严禁跳过' in msg
    assert '已成功执行工具 1 次' in msg
    assert '1. 查行业规模' in msg and '2. 查竞争格局' in msg
    # 无计划时给出分步指引
    msg2 = _step_guard_message(0, 2, plan=None)
    assert 'web_search' in msg2 or 'columns/overview' in msg2


def test_agent_loop_blocks_skipping_to_conclusion(monkeypatch):
    """集成验证：模型第 1 次取数后就想写结论 → 步骤守卫打回继续取数 → 达标后才能终稿。
    全程 mock LLM 与联网，不打真实接口。"""
    import llm_client
    import web_search
    import ai_agent

    monkeypatch.setattr(ai_agent, 'MIN_SUCCESS_CALLS', 2)

    # 屏蔽真实联网搜索
    monkeypatch.setattr(web_search, 'web_search',
                        lambda q, top_k=5: {'ok': True, 'results': [
                            {'title': 't', 'url': 'u', 'snippet': f'关于{q}的数据'}]})

    chat_calls = []

    def fake_chat_with_skip(messages, cfg, max_tokens=None, **kw):
        sys_text = messages[0]['content'] if messages else ''
        user_texts = [m['content'] for m in messages if m['role'] == 'user']
        last_user = user_texts[-1]
        # 规划阶段
        if '规划器' in sys_text:
            chat_calls.append(messages)
            return {'content': '{"steps": ["查询行业规模", "查询竞争格局"]}', 'usage': {'total_tokens': 1}}
        # 强制收敛终稿
        if '停止调用工具' in last_user:
            chat_calls.append(messages)
            return {'content': '## 一、行业规模与增长\n2024年市场规模约3000亿元（来源：工具查询）。', 'usage': {'total_tokens': 1}}
        # 已执行的工具轮数（user 消息里含 Tool: 结果）
        tool_rounds = sum(1 for t in user_texts if 'Tool:' in t)
        chat_calls.append(messages)
        if tool_rounds == 0:
            return {'content': '{"tool": "web_search", "params": {"query": "游戏行业 市场规模"}}',
                    'usage': {'total_tokens': 1}}
        if tool_rounds == 1 and '步骤守卫' in last_user:
            # 守卫打回后服从：继续调用工具补齐第二个维度
            return {'content': '{"tool": "web_search", "params": {"query": "游戏行业 竞争格局"}}',
                    'usage': {'total_tokens': 1}}
        if tool_rounds == 1:
            # 跳步尝试：不调工具，直接写结论
            return {'content': '## 结论\n（跳步写出的结论）', 'usage': {'total_tokens': 1}}
        return {'content': '{"tool": "web_search", "params": {"query": "游戏行业 政策"}}',
                'usage': {'total_tokens': 1}}

    monkeypatch.setattr(llm_client, 'chat', fake_chat_with_skip)

    result = ai_agent.agent_loop(user_message='请帮我分析游戏行业',
                                 llm_config={'provider': 'ollama'},
                                 max_iterations=3, file_name=None)

    assert result.get('ok') is True, result.get('output', '')[:200]
    # 守卫生效：终稿前成功执行了 2 次工具调用
    ok_calls = [c for c in result.get('calls', []) if c.get('status') == 'ok']
    assert len(ok_calls) >= 2, f"守卫未拦截跳步，仅 {len(ok_calls)} 次成功调用"
    # 对话历史中出现过守卫拦截消息
    guard_seen = any('步骤守卫' in (m.get('content') or '')
                     for msgs in chat_calls for m in msgs if m.get('role') == 'user')
    assert guard_seen, "未观察到步骤守卫拦截"
    # 最终输出是干净文字（无工具 JSON）
    assert '"tool"' not in result['output']
    assert '行业规模' in result['output']


@pytest.fixture
def cache():
    return ToolCache(maxsize=3)


def test_tool_cache_roundtrip(cache):
    cache.put('describe', {'file': 'a.csv'}, 'result-1')
    assert cache.get('describe', {'file': 'a.csv'}) == 'result-1'
    assert cache.get('describe', {'file': 'b.csv'}) is None


def test_tool_cache_put_same_key_no_error(cache):
    """重复 put 相同 key 不应抛异常（并发下 remove 已移除的情况）"""
    cache.put('describe', {'file': 'a.csv'}, 'r1')
    cache.put('describe', {'file': 'a.csv'}, 'r2')  # 覆盖，不抛错
    assert cache.get('describe', {'file': 'a.csv'}) == 'r2'


def test_tool_cache_eviction(cache):
    cache.put('t1', {'a': 1}, 'x')
    cache.put('t2', {'a': 1}, 'x')
    cache.put('t3', {'a': 1}, 'x')
    cache.put('t4', {'a': 1}, 'x')  # 超过 maxsize=3，淘汰最久未用的
    assert cache.get('t1', {'a': 1}) is None
    assert cache.get('t4', {'a': 1}) == 'x'


def test_parse_calls_json():
    text = '{"tool": "describe", "params": {"file": "a.csv"}}'
    calls = parse_calls(text)
    assert len(calls) == 1 and calls[0]['tool'] == 'describe'


def test_parse_calls_nested_params():
    """嵌套参数对象（内层 {}）应被完整解析"""
    text = '{"tool": "chart", "params": {"file": "a.csv", "chart_type": "scatter", "x": ["a"]}}'
    calls = parse_calls(text)
    assert len(calls) == 1
    assert calls[0]['params']['chart_type'] == 'scatter'
    assert calls[0]['params']['x'] == ['a']


def test_parse_calls_multiple():
    text = ('{"tool": "columns", "params": {"file": "a.csv"}}\n'
            '{"tool": "describe", "params": {"file": "a.csv"}}')
    calls = parse_calls(text)
    assert [c['tool'] for c in calls] == ['columns', 'describe']


def test_parse_calls_bracket():
    text = '需要先看数据 [CALL:describe file=a.csv] 再分析'
    calls = parse_calls(text)
    assert len(calls) == 1 and calls[0]['tool'] == 'describe'
    assert calls[0]['params'].get('file') == 'a.csv'


def test_parse_calls_none():
    assert parse_calls('没有任何工具调用，直接回答') == []


def test_parse_calls_malformed():
    # 损坏 JSON 应被跳过而不崩溃
    assert parse_calls('{"tool": broken json}') == []


# ==================== 路由配置清洗 ====================

def test_sanitize_provider_whitelist():
    from routes.ai_assistant_routes import _sanitize_llm_config
    cfg = _sanitize_llm_config({'provider': 'evil-proxy', 'apiUrl': 'http://bad.example.com'})
    assert cfg['provider'] == 'sensenova'  # 非白名单回退默认免费 provider（日日新，2026-09-17 起）
    cfg2 = _sanitize_llm_config({'provider': 'deepseek', 'apiUrl': 'http://bad.example.com'})
    assert 'apiUrl' not in cfg2  # 云端 provider 禁止自定义 URL
    cfg3 = _sanitize_llm_config({'provider': 'custom', 'apiUrl': 'http://my-llm.example.com/v1'})
    assert cfg3['apiUrl'] == 'http://my-llm.example.com/v1'  # custom 允许


def test_sanitize_none_and_non_dict():
    from routes.ai_assistant_routes import _sanitize_llm_config
    assert _sanitize_llm_config(None) is None
    assert _sanitize_llm_config('not-a-dict') is None


# ==================== report_helpers ====================

def test_report_helpers():
    from report_helpers import (build_autonomous_report_html,
                                build_correlation_html, build_group_html)
    html = build_autonomous_report_html(
        {'overview': {'rows': 100, 'cols': 5}, 'key_findings': ['发现1'],
         'anomalies': {'total_outliers': 2, 'affected_cols': ['x']},
         'recommendations': ['建议1']}, 'a.csv')
    assert '100' in html and '发现1' in html
    assert '异常' in html and '建议1' in html
    corr_html = build_correlation_html({'top_pairs': [('a', 'b', 0.9)],
                                        'causal_hints': ['因果提示']}, 'a.csv')
    assert '最强相关对' in corr_html and '因果提示' in corr_html
    group_html = build_group_html({'group_col': 'city', 'group_count': 3,
                                   'top_group': ('北京', 10), 'bottom_group': ('上海', 2)}, 'a.csv')
    assert '分组统计' in group_html and '北京' in group_html and '上海' in group_html


# ==================== Agent 框架注入（DID / 面板场景） ====================

def test_agent_loop_injects_did_framework():
    """DID/政策评估问题应注入因果推断框架到 system prompt"""
    from unittest import mock
    from ai_agent import agent_loop

    captured = {}

    def fake_chat(messages, config, **kwargs):
        captured['system'] = messages[0]['content']
        # 第一轮返回无工具调用的最终报告（避免继续循环）
        return {'content': '## 一、数据概况\n分析完成'}

    with mock.patch('llm_client.chat', side_effect=fake_chat):
        agent_loop('用31省面板数据研究长护险试点对家庭消费结构的影响（DID 分析）',
                   llm_config={'provider': 'mock', 'apiKey': 'k', 'model': 'm'},
                   file_name='panel.csv', max_iterations=1)

    sys_prompt = captured.get('system', '')
    assert '因果推断研究框架' in sys_prompt          # did_framework 已注入
    assert '双向固定效应' in sys_prompt
    assert '面板数据识别与处理' in sys_prompt          # panel_data_hint 已注入
    assert '{tools_prompt}' not in sys_prompt          # 工具列表占位符已替换


def test_agent_loop_no_framework_for_plain_question():
    """普通问题不应注入 DID 框架"""
    from unittest import mock
    from ai_agent import agent_loop

    captured = {}

    def fake_chat(messages, config, **kwargs):
        captured['system'] = messages[0]['content']
        return {'content': '简单回答'}

    with mock.patch('llm_client.chat', side_effect=fake_chat):
        agent_loop('这个文件有多少行',
                   llm_config={'provider': 'mock', 'apiKey': 'k', 'model': 'm'},
                   file_name='a.csv', max_iterations=1)

    sys_prompt = captured.get('system', '')
    assert '因果推断研究框架' not in sys_prompt


# ==================== 分析建议系统（覆盖网站全部分析模块） ====================

def test_action_map_covers_main_modules():
    """建议类型应覆盖网站主要分析功能模块"""
    from ai_assistant import _ACTION_API_MAP, _ACTION_LABEL
    for action in ('describe', 'correlation', 'group', 'clean', 'chart',
                   'pivot', 'did', 'ht', 'ts', 'ml', 'nlp', 'fund'):
        assert action in _ACTION_API_MAP, f'缺少 API 映射: {action}'
        assert action in _ACTION_LABEL, f'缺少标签: {action}'


def test_fund_suggestion_maps_to_metrics_api():
    """基金指标建议应映射到 /api/finance/fund-metrics 并透传 nav_col"""
    from ai_assistant import _parse_suggestions, _suggestion_to_step
    output = '分析完成\n\n【建议操作】\n[ACTION:fund file=基金净值.csv nav_col=单位净值]'
    reply, suggestions = _parse_suggestions(output)
    assert len(suggestions) == 1 and suggestions[0]['action'] == 'fund'
    assert suggestions[0]['params']['nav_col'] == '单位净值'
    step = _suggestion_to_step(suggestions[0], '基金净值.csv')
    assert step['params']['url'] == '/api/finance/fund-metrics'
    assert step['params']['body']['file'] == '基金净值.csv'
    assert step['params']['body']['nav_col'] == '单位净值'


def test_parse_extended_suggestions():
    from ai_assistant import _parse_suggestions, _suggestion_to_step
    output = ('分析完成，核心结论如下。\n\n【建议操作】\n'
              '[ACTION:did file=a.csv dep_var=upgr treat_col=treatrate time_col=year unit_col=province]\n'
              '[ACTION:correlation file=a.csv]\n'
              '[ACTION:ml file=a.csv task_type=regression target_col=upgr feature_cols=x1,x2]\n'
              '[ACTION:pivot file=a.csv index=province values=upgr]')
    reply, suggestions = _parse_suggestions(output)
    assert len(suggestions) == 4
    actions = [s['action'] for s in suggestions]
    assert actions == ['did', 'correlation', 'ml', 'pivot']
    # 建议正文中 [ACTION] 已被剥离
    assert '[ACTION' not in reply

    # DID → /api/did/full-analysis，参数透传
    step = _suggestion_to_step(suggestions[0], 'a.csv')
    assert step['params']['url'] == '/api/did/full-analysis'
    assert step['params']['body']['dep_var'] == 'upgr'
    assert step['params']['body']['treat_col'] == 'treatrate'

    # ML → /api/ml/train，feature_cols 转 list、file 映射到 file_name
    step_ml = _suggestion_to_step(suggestions[2], 'a.csv')
    assert step_ml['params']['url'] == '/api/ml/train'
    assert step_ml['params']['body']['feature_cols'] == ['x1', 'x2']
    assert step_ml['params']['body']['file_name'] == 'a.csv'

    # 未知 action 兜底为 describe（真实执行，不静默丢弃）
    step_unknown = _suggestion_to_step({'action': 'unknown', 'params': {}}, 'a.csv')
    assert step_unknown['params']['url'] == '/api/describe'


# ==================== 本轮优化：概念知识保护 / 数字计算 / 指代澄清 ====================

def test_finance_subintent_does_not_suppress_concept_knowledge():
    """'什么是市盈率' 即使 router 命中 stock_valuation 子意图，也应保留知识回答路径"""
    from ai_assistant import _maybe_clarify
    router = {
        'intent': 'knowledge', 'action': 'knowledge', 'confidence': 0.91,
        'finance': {'is_finance': True, 'subintent': 'stock_valuation'},
        'needs_clarification': False,
    }
    assert _maybe_clarify('什么是市盈率', router, None) is None
    from knowledge_rag import search_knowledge, is_knowledge_query
    hits = search_knowledge('什么是市盈率', top_k=2)
    assert hits and is_knowledge_query('什么是市盈率', hits)


def test_real_compute_numbers_mean():
    """纯数字列表平均值应本地算出（无需文件）"""
    from real_compute import compute_and_render
    r = compute_and_render('帮我算一下这些数字的平均值 1,2,3,4,5', '')
    assert r['kind'] == 'numbers'
    assert r['computed'] and abs(r['computed']['result'] - 3.0) < 1e-6
    assert r['text'] and '3.0' in r['text']


def test_real_compute_numbers_sum_and_median():
    from real_compute import compute_and_render
    r = compute_and_render('求和：10 20 30', '')
    assert r['kind'] == 'numbers' and r['computed']['result'] == 60.0
    r2 = compute_and_render('中位数是多少 5,1,9,3', '')
    assert r2['kind'] == 'numbers' and r2['computed']['result'] == 4.0


def test_dangling_reference_detected(monkeypatch):
    """无历史标的的指代追问应触发澄清（_looks_like_dangling_reference）"""
    import ai_assistant
    monkeypatch.setattr(ai_assistant, '_chat_manager', type('CM', (), {'context': {}})())
    assert ai_assistant._looks_like_dangling_reference('那它现在贵不贵') is True
    assert ai_assistant._looks_like_dangling_reference('分析兆易创新贵不贵') is False  # 自带新标的


def test_dangling_reference_with_history_is_real(monkeypatch):
    """有历史标的时不算 dangling（交给真指代处理）"""
    import ai_assistant
    cm = type('CM', (), {'context': {'last_stock': {'code': '603986', 'name': '兆易创新'}}})()
    monkeypatch.setattr(ai_assistant, '_chat_manager', cm)
    assert ai_assistant._looks_like_dangling_reference('那它现在贵不贵') is False
    assert ai_assistant._references_recent_target('那它现在贵不贵') is True


def test_router_cache_reuses_result(monkeypatch):
    """_route_intent 同消息短时间重复调用应命中缓存（避免重复路由）"""
    from unittest import mock
    import ai_assistant
    import intent_router
    calls = {'n': 0}
    real_route = intent_router.route

    def counting_route(text, history=None, llm_config=None):
        calls['n'] += 1
        return real_route(text, history=history, llm_config=llm_config)

    ai_assistant._router_cache.clear()
    with mock.patch.object(intent_router, 'route', side_effect=counting_route):
        r1 = ai_assistant._route_intent('你好')
        r2 = ai_assistant._route_intent('你好')
        assert r1 is r2          # 命中缓存同一对象
        assert calls['n'] == 1   # 只路由一次
    ai_assistant._router_cache.clear()


# ============ 事实溯源审计（P2-10）匹配增强与 ratio 语义（2026-09-18）============

def _audit(output, calls):
    from ai_agent import _audit_fact_sources
    return _audit_fact_sources(output, calls)


def test_fact_audit_thousand_separator_verified():
    """千分位差异不算证据缺口：报告 1234567 ↔ 来源 1,234,567"""
    calls = [{'tool': 'web_search', 'result': '2024年营收 1,234,567 万元，同比高增'}]
    note, stats = _audit('全年营收 1234567 万元，行业景气。', calls)
    assert stats['unverified'] == 0


def test_fact_audit_rounding_variant_verified():
    """约数不算证据缺口：报告 2846 ↔ 来源 2845.6；12.3% ↔ 来源 12.34%"""
    calls = [{'tool': 'web_search', 'result': '市场规模 2845.6亿元，同比增速 12.34%'}]
    note, stats = _audit('市场规模约 2846亿元，增速约 12.3%。', calls)
    assert stats['unverified'] == 0


def test_fact_audit_unit_suffix_verified():
    """带单位数字与来源裸数字可对上：2845.6亿元 ↔ 2845.6"""
    calls = [{'tool': 'web_search', 'result': '收入为 2845.6（单位：亿元）'}]
    note, stats = _audit('收入 2845.6亿元，环比改善。', calls)
    assert stats['unverified'] == 0


def test_fact_audit_genuinely_missing_still_flagged():
    """真正无源的数字仍必须被点名（防审计被放水）"""
    calls = [{'tool': 'web_search', 'result': '行业保持增长，规模约 3000亿元'}]
    note, stats = _audit('该公司市占率达 87.65%，遥遥领先。', calls)
    assert stats['unverified'] >= 1
    assert '87.65' in note


def test_fact_audit_ratio_is_unverified_ratio():
    """stats.ratio = 未核验率（missing/total）——下游 agent_flow 按此语义报警"""
    calls = [{'tool': 'web_search', 'result': '无数字来源'}]
    note, stats = _audit('数字A为 111，数字B为 222，数字C为 333。', calls)
    assert stats['total'] == 3
    assert stats['unverified'] == 3
    assert abs(stats['ratio'] - 1.0) < 1e-6
