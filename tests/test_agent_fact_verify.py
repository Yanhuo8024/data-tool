# -*- coding: utf-8 -*-
"""Agent 事实核查：核验数据必须来自成功工具的真实返回。"""
import inspect


def _audit(output, calls):
    from ai_agent import _audit_fact_sources
    return _audit_fact_sources(output, calls)


def test_verified_data_lists_real_sources():
    """已核验数字必须带上来源工具，且 policy=data_must_be_real。"""
    calls = [{
        'tool': 'web_search', 'status': 'ok',
        'result': '2024年营收 1,234,567 万元，同比 +12.3%',
    }]
    note, stats = _audit('全年营收 1234567 万元，增速 12.3%。', calls)
    assert stats['policy'] == 'data_must_be_real'
    assert stats['real_data_only'] is True
    assert stats['unverified'] == 0
    assert stats['verified'] >= 2
    vals = {x['value'] for x in stats['verified_data']}
    assert any('1234567' in v.replace(',', '') or '1,234,567' in v for v in vals)
    for item in stats['verified_data']:
        assert item['real'] is True
        assert item.get('source')
    assert '核验数据' in note
    assert '数据必须真实' in note or '可作为结论依据' in note


def test_failed_tool_result_not_counted_as_real():
    """失败工具的返回不得当作真实数据源。"""
    calls = [{
        'tool': 'web_search', 'status': 'error',
        'result': '行业规模 2845.6亿元（错误缓存）',
    }]
    note, stats = _audit('行业规模 2845.6亿元，空间广阔。', calls)
    assert stats['unverified'] >= 1
    assert stats['verified'] == 0
    assert stats['evidence_tools'] == 0
    assert any('2845.6' in x['value'] or '2846' in x['value']
               for x in stats['unverified_data'])
    assert '未核验' in note
    assert '不得视为真实数据' in note or '不得' in note


def test_unverified_numbers_flagged_as_not_real():
    """无源数字进入未核验清单，且明确 real=False。"""
    calls = [{'tool': 'finance_data', 'status': 'ok', 'result': '最新价 18.6 元'}]
    note, stats = _audit('最新价 18.6 元，市占率高达 87.65%。', calls)
    unv = {x['value']: x for x in stats['unverified_data']}
    assert any('87.65' in v for v in unv)
    for x in stats['unverified_data']:
        assert x['real'] is False
        assert x.get('reason')
    # 已核验的仍应标出
    assert any('18.6' in x['value'] for x in stats['verified_data'])
    assert '核验数据' in note


def test_file_analysis_tool_numbers_verified():
    """有文件场景：describe 等工具返回也应被核验（不仅限联网）。"""
    calls = [{
        'tool': 'describe', 'status': 'ok',
        'result_brief': 'rows=1200 cols=8 mean(revenue)=2845.6',
    }]
    note, stats = _audit('样本 1200 行，revenue 均值 2845.6。', calls)
    assert stats['unverified'] == 0
    assert stats['verified'] >= 2
    assert 'describe' in (stats.get('sources') or [])
    assert any(x.get('source') == 'describe' for x in stats['verified_data'])


def test_cached_ok_result_counts_as_evidence():
    """cached=True 且 status=ok 的结果属于真实读数，应可核验。"""
    calls = [{
        'tool': 'head', 'status': 'ok', 'cached': True,
        'result_preview': 'close 列前5行: 10.5, 11.2, 12.0',
    }]
    note, stats = _audit('收盘价样本含 11.2 元。', calls)
    assert stats['verified'] >= 1
    assert stats['unverified'] == 0


def test_agent_loop_attaches_fact_audit_contract():
    """agent_loop 结果契约必须带 fact_audit（含 verified_data）。"""
    import ai_agent
    src = inspect.getsource(ai_agent.agent_loop)
    assert "result['fact_audit']" in src
    assert 'data_must_be_real' in inspect.getsource(ai_agent._audit_fact_sources)


def test_prompts_enforce_real_data():
    from prompts import get_prompt
    refl = get_prompt('reflection')
    assert '数据必须真实' in refl or '真实' in refl
    assert '核验数据' in refl
    refi = get_prompt('refinement')
    assert '核验数据' in refi or '真实' in refi
    agent = get_prompt('agent_system')
    assert '真实' in agent


def test_agent_flow_rule_mentions_must_be_real():
    import agent_flow
    assert 'data_must_be_real' in inspect.getsource(agent_flow) or \
           '数据必须真实' in inspect.getsource(agent_flow._rule_fact_unverified)
