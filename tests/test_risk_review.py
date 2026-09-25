#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""风控初审 Agent 测试（risk_review）

覆盖：五条确定性规则、裁定分级与转人工标记，以及最容易被写坏的一点——
**否定语境必须不计违规**（否则风控模块会把自己的免责声明判成违规）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import risk_review as RR


# ---------------------------- 规则 1：收益承诺 ----------------------------

def test_return_pledge_detected():
    r = RR.check_return_pledge('这款产品保本保息，稳赚不赔。')
    assert r and r['level'] == 'high'
    assert '保本保息' in r['hits']


def test_return_pledge_negation_not_flagged():
    """合规话术里的否定表述不得判违规（这是本模块最容易误报的地方）。"""
    assert RR.check_return_pledge('任何理财产品都不得承诺保本保收益。') is None
    assert RR.check_return_pledge('销售不得使用"稳赚不赔"这类表述。') is None
    assert RR.check_return_pledge('没有保本理财这一产品类型。') is None


def test_return_pledge_clean_text():
    assert RR.check_return_pledge('该基金近三年年化收益率 4.2%。') is None


# ---------------------------- 规则 2：买卖指令 ----------------------------

def test_trade_advice_detected():
    r = RR.check_trade_advice('建议买入贵州茅台，可以加仓至半仓。')
    assert r and r['level'] == 'high'


def test_trade_advice_negation_not_flagged():
    assert RR.check_trade_advice('本报告不构成投资建议，不推荐买入任何标的。') is None


def test_trade_advice_needs_imperative_context():
    """单纯出现"买入"是中性词（如描述资金流向），不构成荐股。"""
    assert RR.check_trade_advice('北向资金净买入 32 亿元。') is None


# ---------------------------- 规则 3：免责声明 ----------------------------

_LONG_FINANCE = ('该公司当前市盈率 18 倍，低于所处行业中位数 24 倍；净资产收益率 15%，'
                 '连续三年高于同业；经营性现金流稳健，负债结构以长期借款为主，'
                 '估值处于近五年历史中枢下方，具备一定安全边际。')


def test_missing_disclaimer_detected():
    r = RR.check_disclaimer(_LONG_FINANCE)
    assert r and r['rule'] == 'missing_disclaimer'


def test_disclaimer_present_not_flagged():
    assert RR.check_disclaimer(_LONG_FINANCE + '以上内容不构成投资建议，市场有风险。') is None


def test_disclaimer_skips_short_text():
    assert RR.check_disclaimer('估值偏低。') is None


def test_disclaimer_skips_non_finance_text():
    assert RR.check_disclaimer('今天天气不错，适合出门散步，顺便去公园跑跑步。' * 3) is None


# ---------------------------- 规则 4：资料完整性 ----------------------------

def test_data_incomplete_low_rows():
    r = RR.check_data_incomplete('a.csv', {'rows': 12})
    assert r and '样本' in r['hits'][0]


def test_data_incomplete_high_missing_rate():
    r = RR.check_data_incomplete('a.csv', {'rows': 500, 'missing_top': [('income', 0.72)]})
    assert r and any('income' in h for h in r['hits'])


def test_data_incomplete_clean():
    assert RR.check_data_incomplete('a.csv', {'rows': 500, 'missing_top': [('income', 0.02)]}) is None
    assert RR.check_data_incomplete() is None


# ---------------------------- 规则 5：数字溯源 ----------------------------

def test_unverified_numbers_high_ratio():
    r = RR.check_unverified_numbers({'total': 10, 'unverified': 8, 'ratio': 0.8,
                                     'unverified_data': [{'value': '12345'}]})
    assert r and '12345' in r['hits']


def test_unverified_numbers_ok_ratio():
    assert RR.check_unverified_numbers({'total': 10, 'unverified': 1, 'ratio': 0.1}) is None
    assert RR.check_unverified_numbers(None) is None
    assert RR.check_unverified_numbers({}) is None


# ---------------------------- 裁定分级 ----------------------------

def test_review_block_on_high_risk():
    r = RR.review('建议买入该股票，保本保息，稳赚不赔。')
    assert r['verdict'] == 'block' and r['level'] == 'high'
    assert r['handoff'] is True


def test_review_needs_review_on_mid_risk():
    r = RR.review(_LONG_FINANCE)
    assert r['verdict'] == 'needs_review' and r['handoff'] is True


def test_review_pass_on_clean_text():
    r = RR.review('今天天气不错，出门散步很舒服。')
    assert r['verdict'] == 'pass' and r['handoff'] is False
    assert r['findings'] == []


def test_review_collects_multiple_findings():
    r = RR.review(_LONG_FINANCE, current_file='a.csv', file_stats={'rows': 8},
                  fact_stats={'total': 10, 'unverified': 9, 'ratio': 0.9})
    rules = {f['rule'] for f in r['findings']}
    assert {'missing_disclaimer', 'data_incomplete', 'unverified_numbers'} <= rules


# ---------------------------- 出口挂载 ----------------------------

def test_attach_appends_block_and_sets_field():
    payload = {'ok': True, 'reply': ('建议买入贵州茅台，保证收益翻倍，稳赚不赔。' * 8)}
    out = RR.attach(payload)
    assert out['risk_review']['verdict'] == 'block'
    assert out['risk_review']['handoff'] is True
    assert '🛡️ 风控初审' in out['reply']
    assert '转人工' in out['reply']


def test_attach_is_idempotent():
    payload = {'ok': True, 'reply': ('建议买入贵州茅台，保证收益翻倍，稳赚不赔。' * 8)}
    once = RR.attach(payload)
    twice = RR.attach(once)
    assert twice['reply'] == once['reply']


def test_attach_skips_short_reply():
    short = {'ok': True, 'reply': '好的'}
    assert RR.attach(short) == short


def test_attach_skips_failed_response():
    failed = {'ok': False, 'reply': '建议买入贵州茅台，保证收益翻倍。' * 8}
    assert RR.attach(failed) is failed


def test_attach_handles_non_dict():
    assert RR.attach(None) is None
    assert RR.attach('文本') == '文本'


def test_attach_consumes_fact_stats():
    payload = {'ok': True, 'reply': '本期营收 12345 万元，同比增长显著。' * 8,
               'fact_audit': {'total': 10, 'unverified': 9, 'ratio': 0.9}}
    out = RR.attach(payload)
    assert any(f['rule'] == 'unverified_numbers' for f in out['risk_review']['findings'])


# ---------------------------- 渲染 ----------------------------

def test_render_review_empty_when_no_findings():
    assert RR.render_review(RR.review('今天天气不错。')) == ''
