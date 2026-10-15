# -*- coding: utf-8 -*-
"""情景估值（三档 / SOTP / 亏损企业模板）与商用车深挖、技术面事件联动。"""

import itertools

import business_deep as bd
import scenario_valuation as sv


class Test三档情景:

    def test_量纲乘法(self):
        """万辆 × 万元/辆 = 亿元（1e4 × 1e4 = 1e8）。"""
        c = sv.build_case('中性', volume=6.0, unit_profit=1.2)
        assert c['contribution'] == 7.2
        assert '6 万辆 × 1.2 万元/辆 = 7.2 亿元' == c['formula']

    def test_区间与概率加权中枢(self):
        cases = [sv.build_case('悲观', volume=2.0, unit_profit=0.5),
                 sv.build_case('中性', volume=6.0, unit_profit=1.2),
                 sv.build_case('乐观', volume=12.0, unit_profit=2.0)]
        t = sv.scenario_table('尊界', cases)
        assert t['low'] == 1.0 and t['high'] == 24.0
        assert t['expected_contribution'] == 9.85   # 0.25×1 + 0.5×7.2 + 0.25×24

    def test_缺销量不参与计算(self):
        cases = [sv.build_case('悲观', volume=None, unit_profit=1.0),
                 sv.build_case('中性', volume=5.0, unit_profit=1.0)]
        t = sv.scenario_table('X', cases)
        assert t['rows'][0]['contribution'] is None
        assert t['low'] == 5.0

    def test_倍数折算为估值贡献(self):
        cases = [sv.build_case('中性', volume=10.0, unit_profit=1.0)]
        t = sv.scenario_table('X', cases, multiple=18, multiple_name='PE')
        assert t['rows'][0]['valuation'] == 180.0

    def test_折现折算(self):
        cases = [sv.build_case('中性', volume=10.0, unit_profit=1.0)]
        t = sv.scenario_table('X', cases, discount_pct=10, years=2)
        assert t['rows'][0]['pv'] == 8.2645

    def test_单点数不呈现(self):
        cases = [sv.build_case('悲观', volume=1.0, unit_profit=1.0),
                 sv.build_case('乐观', volume=2.0, unit_profit=1.0)]
        md = sv.scenario_table('X', cases)['table_md']
        assert '区间' in md and '单点数不呈现' in md


class TestSOTP叠加:

    def test_每股区间(self):
        cases = [sv.build_case('悲观', volume=2.0, unit_profit=0.5),
                 sv.build_case('乐观', volume=12.0, unit_profit=2.0)]
        t = sv.scenario_table('尊界', cases)
        # 未给 multiple 时用「年贡献利润」直接叠加：600 + 1 − 180 = 421
        s = sv.sotp_add(base_value=600.0, project=t, net_debt=180.0, shares=21.9)
        assert s['rows'][0]['equity_value'] == 421.0
        assert s['per_share_low'] == 19.22
        assert s['per_share_high'] == 20.27

    def test_净现金给负数时增加估值(self):
        cases = [sv.build_case('中性', volume=1.0, unit_profit=1.0)]
        t = sv.scenario_table('X', cases)
        s = sv.sotp_add(base_value=100.0, project=t, net_debt=-50.0, shares=10.0)
        assert s['rows'][0]['equity_value'] == 151.0

    def test_渲染含基本盘与净负债(self):
        cases = [sv.build_case('中性', volume=1.0, unit_profit=1.0)]
        t = sv.scenario_table('尊界', cases)
        s = sv.sotp_add(base_value=600.0, project=t, net_debt=180.0, shares=20.0,
                        base_name='商用车基本盘')
        assert '商用车基本盘' in s['text'] and '净负债' in s['text']


class Test亏损企业方法链:

    def test_FCF为正推荐DCF(self):
        r = sv.loss_making_playbook(fcf_positive=True)
        assert r['recommended'] == 'DCF' and not r['discarded']

    def test_FCF为负弃用DCF(self):
        r = sv.loss_making_playbook(fcf_positive=False, profitable_segments=1)
        assert 'DCF' in r['discarded']
        assert any('DCF 失效' in x for x in r['reasons'])

    def test_有盈利分部推荐SOTP(self):
        r = sv.loss_making_playbook(fcf_positive=False, profitable_segments=2)
        assert r['recommended'] == 'SOTP'

    def test_无盈利分部且有期权特征推荐OPTION(self):
        r = sv.loss_making_playbook(fcf_positive=False, profitable_segments=0,
                                    option_like=True)
        assert r['recommended'] == 'OPTION'
        assert 'SOTP' in r['discarded']

    def test_SOTP与期权定价不互斥(self):
        """基本盘用 SOTP、新业务用期权定价，两者可以同时出现在链里。"""
        r = sv.loss_making_playbook(fcf_positive=False, profitable_segments=1,
                                    option_like=True)
        assert 'SOTP' in r['chain'] and '期权式定价' in r['chain']

    def test_情景分析始终在链尾(self):
        for kw in ({'fcf_positive': True},
                   {'fcf_positive': False, 'profitable_segments': 1},
                   {'fcf_positive': False, 'profitable_segments': 0, 'option_like': True},
                   {}):
            assert sv.loss_making_playbook(**kw)['chain'][-1] == '情景分析', kw

    def test_未提供条件不假设DCF可用(self):
        r = sv.loss_making_playbook()
        assert r['recommended'] == 'SCENARIO' and 'DCF' in r['discarded']

    def test_渲染标注期权定价为简化(self):
        txt = sv.loss_making_playbook(fcf_positive=False, profitable_segments=0,
                                      option_like=True)['text']
        assert '简化实现' in txt and '不假装精确' in txt


class Test商用车基本盘:

    def test_四项齐全(self):
        r = bd.commercial_vehicle({'light_truck_series': [18.0, 17.2, 16.5, 19.8],
                                   'trade_in_boost': 9.5, 'export_share': 22.0,
                                   'emission': '国六b'})
        assert r['trend']['direction'] == 'up'
        assert r['trend']['change_pct'] == 10.0
        assert r['export_share_pct'] == 22.0
        assert not r['gaps']

    def test_缺数给缺口不编造(self):
        r = bd.commercial_vehicle({})
        assert len(r['gaps']) >= 3
        assert '未获取' in r['text']
        assert r['trend']['direction'] == 'unknown'

    def test_趋势下行可识别(self):
        r = bd.commercial_vehicle({'light_truck_series': [20.0, 18.0, 15.0]})
        assert r['trend']['direction'] == 'down'

    def test_出口占比低给出提示(self):
        r = bd.commercial_vehicle({'export_share': 8.0})
        assert '国内周期影响更大' in r['text']

    def test_以旧换新提示需求前置(self):
        r = bd.commercial_vehicle({'trade_in_boost': 12.0})
        assert '需求前置' in r['text']


class Test技术面事件联动:

    def test_强势临近判预期充分(self):
        r = bd.tech_event_link({'price': 45, 'ma20': 40, 'month_change': 12.0},
                               [{'stage': 'upcoming'}], event_name='尊界上市')
        assert r['tech']['state'] == 'strong'
        assert '预期已较充分' in r['text']

    def test_弱势临近判弹性更大(self):
        r = bd.tech_event_link({'price': 35, 'ma20': 42, 'month_change': -14.0},
                               [{'stage': 'upcoming'}])
        assert r['tech']['state'] == 'weak'
        assert '预期偏低，弹性更大' in r['text']

    def test_弱势已落地判兑现失败(self):
        r = bd.tech_event_link({'price': 35, 'ma20': 42, 'month_change': -14.0},
                               ['landed'])
        assert r['stage'] == 'landed'
        assert '兑现失败' in r['text']

    def test_信息不足不做情景判断(self):
        r = bd.tech_event_link({}, [])
        assert r['combo'] is None
        assert '不做情景判断' in r['text']

    def test_六格全覆盖(self):
        missing = [k for k in itertools.product(
            ('strong', 'neutral', 'weak'), ('upcoming', 'landed'))
            if k not in bd._COMBO]
        assert not missing, f'组合矩阵缺格: {missing}'

    def test_事件阶段中文可识别(self):
        assert bd.classify_stage([{'stage': '临近'}]) == 'upcoming'
        assert bd.classify_stage(['已落地']) == 'landed'

    def test_渲染为报告段落(self):
        r = bd.tech_event_link({'price': 45, 'ma20': 40}, [{'stage': 'landed'}])
        assert '技术面 × 事件联动' in bd.render_link(r)
