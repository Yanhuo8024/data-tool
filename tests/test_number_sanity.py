# -*- coding: utf-8 -*-
"""数字体检（单位冲突/倍数反推/量级冲突/多源交叉/衍生数字）与黑名单。"""

import fact_triplet as ft
import number_sanity as ns


class Test单位口径冲突:

    def test_月之暗面现场(self):
        """「300 亿美元」与「300 亿元」并存 → 单位/口径冲突。"""
        hits = ns.unit_conflict('最新融资 300 亿美元估值。此前报道融资 300 亿元。')
        assert len(hits) == 1 and hits[0]['value'] == '300'

    def test_纯美元不报(self):
        assert ns.unit_conflict('融资 300 亿美元，上一轮 200 亿美元。') == []

    def test_无量纲单位不猜(self):
        """「300 亿」没有币种后缀，无法判定 → 不报（不猜）。"""
        assert ns.unit_conflict('融资 300 亿。') == []


class Test倍数反推一致性:

    def test_PS反推一致(self):
        r = ns.check_multiple_consistency(38.0, 7.0, 266.0, label='PS')
        assert r['ok'] is True and r['deviation_pct'] == 0.0

    def test_PS反推不一致须核实口径(self):
        r = ns.check_multiple_consistency(38.0, 7.0, 900.0, label='PS')
        assert r['ok'] is False
        assert '请核实单位/口径' in r['reason']

    def test_缺参不猜(self):
        r = ns.check_multiple_consistency(38.0, None, 266.0, label='PS')
        assert r['ok'] is False and 'denominator' in r['reason']


class Test量级冲突:

    def test_同指标差10倍报警(self):
        rows = [{'metric': '营收', 'value': '7 亿元', 'source': 'a'},
                {'metric': '营收', 'value': '70 亿元', 'source': 'b'}]
        hits = ns.magnitude_conflict(rows)
        assert len(hits) == 1
        assert hits[0]['metric'] == '营收' and hits[0]['ratio'] == 10.0
        assert '核实单位/口径' in hits[0]['warning']

    def test_正常波动不报(self):
        rows = [{'metric': '营收', 'value': '7 亿元', 'source': 'a'},
                {'metric': '营收', 'value': '8.5 亿元', 'source': 'b'}]
        assert ns.magnitude_conflict(rows) == []

    def test_不同指标各自比较(self):
        rows = [{'metric': '营收', 'value': '7', 'source': 'a'},
                {'metric': '利润', 'value': '70', 'source': 'b'}]
        assert ns.magnitude_conflict(rows) == []


class Test多源交叉:

    def test_三源印证两源双源一源单源(self):
        rows = [{'metric': '营收', 'value': '300', 'source': 'web_search'},
                {'metric': '营收', 'value': '300', 'source': 'company_analysis'},
                {'metric': '营收', 'value': '300', 'source': 'file_rag'},
                {'metric': '亏损', 'value': '20', 'source': 'web_search'},
                {'metric': '亏损', 'value': '20', 'source': 'news_api'},
                {'metric': '客户数', 'value': '5000', 'source': 'web_search'}]
        r = ns.multi_source_score(rows)
        levels = {g['value']: g['level'] for g in r['rows']}
        assert levels['300'] == 'corroborated'
        assert levels['20'] == 'dual'
        assert levels['5000'] == 'single'
        assert r['counts'] == {'corroborated': 1, 'dual': 1, 'single': 1}

    def test_同一工具重复只算一个来源(self):
        rows = [{'metric': '营收', 'value': '300', 'source': 'web_search'},
                {'metric': '营收', 'value': '300', 'source': 'web_search'}]
        r = ns.multi_source_score(rows)
        assert r['rows'][0]['level'] == 'single'

    def test_单一来源在渲染中标注(self):
        rows = [{'metric': '客户数', 'value': '5000', 'source': 'web_search'}]
        txt = ns.multi_source_score(rows)['text']
        assert '单一来源' in txt and 'web_search' in txt


class Test衍生数字:

    def test_PS区间推导(self):
        """用户现场：已核验 240~300 亿 ÷ 7 亿 → 34-43 倍，标注「本系统计算」。"""
        d = ns.derive_range((240, 300), (7, 7), label='隐含 PS 倍数')
        assert d['value_low'] == 34.29 and d['value_high'] == 42.86
        assert d['computed_by'] == 'system'
        assert '输入已核验' in d['note']

    def test_负分母不推导(self):
        assert ns.derive_range((100, 200), (-5, 5), label='x') is None

    def test_衍生值从未核验清单剔除(self):
        d = ns.derive_range((240, 300), (7, 7), label='隐含 PS 倍数')
        stats = {'total': 2, 'verified': 2, 'unverified': 1, 'ratio': 0.5,
                 'verified_data': [{'value': '240', 'source': 'web_search'}],
                 'unverified_data': [{'value': '34.3', 'reason': 'x'}],
                 'derived_data': [d], 'sources': ['web_search']}
        rep = ft.build_report('估值 240 亿元，隐含 PS 34.3 倍。', stats)
        assert rep['unverified'] == []
        assert '本系统计算' in rep['note']

    def test_约数也能对上精确值(self):
        """正文写约数 34.3，系统算的是 34.29 —— 数值近似（2% 容差）必须能对上。"""
        assert ft._matches_derived('34.3', {'34.29'}) is True
        assert ft._matches_derived('42.9', {'42.86'}) is True
        assert ft._matches_derived('99', {'34.29'}) is False


class Test数字提取黑名单:

    def test_型号与法规编号排除(self):
        text = ('GLM-4 与 SU7 对标 S800，站上 MA20，备案 18C，依据第 18 条。')
        toks = ft.__dict__  # noqa: F841 占位避免误删
        got = _extract(text)
        assert '4' not in got and '800' not in got and '20' not in got
        assert '7' not in got and '18' not in got

    def test_股票代码排除(self):
        got = _extract('股票代码 600660 涨停，000550 跟涨，港股 HK1810 回落。')
        assert '600660' not in got and '000550' not in got and 'HK1810' not in got

    def test_正常财务数字不误伤(self):
        got = _extract('股价 23.94 元，营收 1,340 亿元（同比 154.6%），总股本 2.53 亿股。')
        # token 自带单位后缀是 _NUM_TOKEN_RE 的既有行为，这里只验「数值仍在」
        joined = ' | '.join(got)
        for tok in ('23.94', '1,340', '154.6', '2.53'):
            assert tok in joined, tok

    def test_is_noise_token_单点(self):
        assert ft.is_noise_token('4', 'GLM-4 发布', 3) is True
        assert ft.is_noise_token('800', '对标 S800', 4) is True
        assert ft.is_noise_token('18', '第 18 条规定', 2) is True
        assert ft.is_noise_token('800', 'OCF 800 亿', 4) is False


def _extract(text):
    """走 agent_fact_audit 的生产提取入口（含黑名单）。"""
    from agent_fact_audit import _extract_numeric_tokens
    return _extract_numeric_tokens(text)
