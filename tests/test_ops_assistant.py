#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""智能运营 Agent 测试（ops_assistant）

覆盖：FAQ 召回与阈值、客户画像抽取、产品品类匹配的**硬约束**（风险超限排除）、
流程引导、以及最关键的**让路规则**（运营不得抢分析任务）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ops_assistant as OA


# ---------------------------- FAQ 召回 ----------------------------

def test_faq_hit_on_specific_question():
    e = OA.match_faq('基金申购费和赎回费怎么算')
    assert e and e['id'] == 'faq_fund_fee'


def test_faq_threshold_rejects_generic_word():
    """泛词"基金"（2 字）不得触发任何条目——否则每句提基金都会命中。"""
    assert OA.match_faq('基金') is None
    assert OA.match_faq('我想了解净值') is None


def test_faq_miss_returns_none():
    assert OA.match_faq('今天北京天气如何') is None
    assert OA.match_faq('') is None


def test_faq_library_loads_and_unique_ids():
    faq = OA.load_faq()
    assert len(faq) >= 15
    ids = [e['id'] for e in faq]
    assert len(set(ids)) == len(ids)
    for e in faq:
        assert e.get('q') and e.get('answer')


def test_render_faq_adds_disclaimer_for_risk_entries():
    risky = next(e for e in OA.load_faq() if e.get('risk'))
    out = OA.render_faq(risky)
    assert '不构成投资建议' in out
    safe = next((e for e in OA.load_faq() if not e.get('risk')), None)
    if safe:
        assert '不构成投资建议' not in OA.render_faq(safe)


# ---------------------------- 客户画像 ----------------------------

def test_parse_profile_extracts_risk_horizon_amount():
    p = OA.parse_profile('我是保守型，有 5 万想放一年，最好能随时取')
    assert p['risk_cap'] == 2
    assert p['horizon_months'] == 12
    assert p['amount'] == 50000
    assert p['liquidity'] is True
    assert p['signals'] >= 4


def test_parse_profile_empty_message():
    p = OA.parse_profile('')
    assert p['risk_cap'] is None and p['signals'] == 0


def test_parse_profile_amount_units():
    assert OA.parse_profile('有20万')['amount'] == 200000
    assert OA.parse_profile('只有5000元')['amount'] == 5000


# ---------------------------- 产品匹配硬约束 ----------------------------

def test_risk_cap_hard_excludes_high_risk_categories():
    """保守型（R2）绝不能拿到股票型/指数型——这是排除，不是降权。"""
    p = OA.parse_profile('我很保守，5 万放一年')
    res = OA.match_products(p)
    names = [it['prod']['name'] for it in res['matched']]
    assert not any('股票型' in n or '指数基金' in n for n in names)
    excluded_names = [pr['name'] for pr, _ in res['excluded']]
    assert any('股票型' in n for n in excluded_names)


def test_min_amount_excludes_products_above_budget():
    """可投金额低于起投门槛的品类必须排除（如大额存单 20 万）。"""
    p = OA.parse_profile('稳健型，1 万，一年')
    res = OA.match_products(p)
    assert not any(it['prod']['id'] == 'cd' for it in res['matched'])
    assert OA.match_products(OA.parse_profile('稳健，100 万，一年'))['matched']


def test_liquidity_preference_ranks_liquid_first():
    res = OA.match_products(OA.parse_profile('稳健，随时要能取，5 万'))
    assert res['matched']
    assert res['matched'][0]['prod']['liquidity'] >= 4


def test_render_product_match_carries_compliance_notice():
    p = OA.parse_profile('稳健，5 万，一年')
    out = OA.render_product_match(p, OA.match_products(p))
    assert '不推荐任何具体产品' in out and '不构成投资建议' in out


# ---------------------------- 流程引导 ----------------------------

def test_guide_hit_for_account_opening():
    g = OA.guide_steps('基金账户怎么开户')
    assert g and g['title'].startswith('开立')
    assert len(g['steps']) >= 3
    assert '平台功能' in OA.render_guide(g)


def test_guide_miss():
    assert OA.guide_steps('什么是久期') is None


# ---------------------------- 统一入口与让路规则 ----------------------------

def test_try_handle_returns_structured_reply():
    out = OA.try_handle('基金申购费和赎回费怎么算')
    assert out and out['ok'] is True
    assert out['source'] == 'ops_faq'
    assert out['intent']['action'] == 'operation'
    assert '下一步可以做' in out['reply']


def test_try_handle_yields_when_file_attached():
    """挂了数据文件 → 运营必须让路，否则会把"分析这份数据"答成产品推荐。"""
    assert OA.try_handle('基金申购费怎么算', current_file='clean.csv') is None


def test_try_handle_yields_on_analysis_verb():
    assert OA.try_handle('帮我分析一下基金净值') is None
    assert OA.try_handle('训练一个模型') is None


def test_try_handle_yields_on_long_message():
    assert OA.try_handle('基' * 220) is None


def test_try_handle_product_match_requires_two_signals():
    """只有"稳健"一个信号时不做匹配——无依据的推荐比不推荐更糟。"""
    assert OA.try_handle('我是稳健型') is None
    out = OA.try_handle('我是稳健型，有 10 万，放两年')
    assert out and out['source'] == 'ops_product_match'


def test_try_handle_guide_priority_over_faq():
    out = OA.try_handle('基金账户怎么开户')
    assert out and out['source'] == 'ops_guide'
