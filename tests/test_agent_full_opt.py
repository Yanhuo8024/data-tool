# -*- coding: utf-8 -*-
"""全面优化回归：白名单门禁 / 长尾 / 技能优先级 / 周环比 / 契约。"""
import inspect
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_verified_number_whitelist_in_finalize():
    from ai_agent import _verified_number_whitelist, _lg_finish
    calls = [{'tool': 'finance_data', 'status': 'ok', 'result': '价格 18.6 元，PE 22.3'}]
    wl = _verified_number_whitelist(calls)
    assert '18.6' in wl and '22.3' in wl
    src = inspect.getsource(_lg_finish)
    assert '_verified_number_whitelist' in src
    assert '数字白名单' in src


def test_skip_heavy_inject_for_simple():
    from ai_agent import _should_skip_heavy_inject
    assert _should_skip_heavy_inject('你好') is True
    assert _should_skip_heavy_inject('今天天气如何') is True
    assert _should_skip_heavy_inject('请做一份全面行业研究报告') is False
    assert _should_skip_heavy_inject('x' * 100) is False


def test_warmup_hook_present():
    from ai_agent import _maybe_warmup_retrieval, agent_loop
    assert callable(_maybe_warmup_retrieval)
    assert '_maybe_warmup_retrieval' in inspect.getsource(agent_loop)


def test_skill_priority_company_over_buffett():
    from skill_manager import match_skill
    m = match_skill('请对贵州茅台做全方位分析，包括基本面')
    assert m.get('name') == 'company-analysis', m.get('name')
    m2 = match_skill('用巴菲特的护城河框架评估苹果')
    assert m2.get('name') == 'buffett-oracle-analyzer', m2.get('name')
    m3 = match_skill('新能源汽车行业全景研究报告 竞争格局')
    assert m3.get('name') == 'industry-research', m3.get('name')


def test_problems_dashboard_has_week_over_week():
    import agent_flow as af
    src = inspect.getsource(af.problems_dashboard)
    assert 'week_over_week' in src
    d = af.problems_dashboard(5)
    assert 'week_over_week' in d


def test_frontend_skill_badge_and_wow():
    js = (ROOT / 'static' / 'js' / 'core.js').read_text(encoding='utf-8')
    assert '已使用技能' in js
    html = (ROOT / 'static' / 'agent-problems.html').read_text(encoding='utf-8')
    assert '本周 vs 上周' in html or 'renderWow' in html


def test_finalize_result_contract_still_has_fact_fields():
    from ai_assistant import _finalize_agent_result
    src = inspect.getsource(_finalize_agent_result)
    for k in ('fact_audit', 'no_tool_with_file', 'fact_unverified_high', 'degraded'):
        assert k in src
