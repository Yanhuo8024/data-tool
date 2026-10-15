# -*- coding: utf-8 -*-
"""同业池自动替换 + 技术面/基本面联动 + 行业变量。"""

import decision_support as ds
import peer_pool as pp


class Test行业识别:

    def test_汽车玻璃(self):
        assert pp.classify_industry('汽车玻璃') == 'auto_glass'
        assert pp.classify_industry('福耀玻璃') == 'auto_glass'

    def test_其他行业(self):
        assert pp.classify_industry('白酒') == 'baijiu'
        assert pp.classify_industry('银行') == 'bank'

    def test_识别不出返回空(self):
        assert pp.classify_industry('某个看不懂的行业') is None

    def test_空输入返回空(self):
        assert pp.classify_industry() is None


class Test同业池:

    def test_汽车玻璃池含点名公司(self):
        names = [p['name'] for p in pp.pool_for_industry('auto_glass')]
        for kw in ('旭硝子', '圣戈班', '板硝子', '信义玻璃', '华域汽车', '均胜电子'):
            assert any(kw in n for n in names), kw

    def test_未知行业池为空(self):
        assert pp.pool_for_industry('nope') == []
        assert pp.pool_for_industry(None) == []

    def test_池可被外部修改不影响内部(self):
        p1 = pp.pool_for_industry('auto_glass')
        p1.append({'name': 'X'})
        assert len(pp.pool_for_industry('auto_glass')) == 6


class Test自动替换:

    def test_选错自动切换(self):
        d = pp.resolve_peers(code='600660', name='福耀玻璃', industry_hint='汽车玻璃',
                             chosen=[{'name': '贵州茅台', 'code': '600519'}])
        assert d['switched'] is True and d['source'] == 'auto_pool'
        assert any('旭硝子' in p['name'] for p in d['peers'])

    def test_正确同业保留(self):
        d = pp.resolve_peers(code='HK0868', name='信义玻璃', industry_hint='汽车玻璃',
                             chosen=[{'name': '华域汽车', 'code': '600741'}])
        assert d['switched'] is False and d['source'] == 'upstream'

    def test_代码前缀差异也能匹配(self):
        d = pp.resolve_peers(code='601398', name='工商银行', industry_hint='银行',
                             chosen=[{'code': 'HK0868'}])
        assert d['switched'] is True
        # by-name 匹配：池内成员用原名给也应命中
        d2 = pp.resolve_peers(code='601398', name='工商银行', industry_hint='银行',
                              chosen=[{'name': '招商银行'}])
        assert d2['switched'] is False

    def test_未提供同业时用池(self):
        d = pp.resolve_peers(code='600660', name='福耀玻璃', industry_hint='汽车玻璃')
        assert d['source'] == 'auto_pool' and len(d['peers']) == 6

    def test_行业未知且无同业不臆造(self):
        d = pp.resolve_peers(code='999999', name='某公司', industry_hint='')
        assert d['peers'] == [] and d['source'] == 'none'
        assert '不臆造' in d['reason']

    def test_行业未知但上游给了同业则保留(self):
        d = pp.resolve_peers(code='999999', name='某公司', industry_hint='',
                             chosen=[{'name': '甲公司'}])
        assert d['source'] == 'upstream' and len(d['peers']) == 1

    def test_校验比例计算(self):
        v = pp.validate_peers([{'name': '华域汽车'}, {'name': '贵州茅台'}], 'auto_glass')
        assert v['ratio'] == 0.5 and v['wrong'] is False

    def test_渲染含切换原因(self):
        d = pp.resolve_peers(code='600660', name='福耀玻璃', industry_hint='汽车玻璃',
                             chosen=[{'name': '贵州茅台'}])
        assert '已自动切换' in pp.render_decision(d)


class Test技术基本面联动:

    def test_九宫格全覆盖(self):
        for t in ('strong', 'weak', 'neutral'):
            for f in ('cheap', 'fair', 'expensive'):
                assert ds._COMBINATIONS.get((t, f)), (t, f)

    def test_技术面分级(self):
        assert ds.classify_tech({'price': 100, 'ma20': 90,
                                 'week_change': 3, 'month_change': 8}) == 'strong'
        assert ds.classify_tech({'price': 80, 'ma20': 90,
                                 'week_change': -3, 'month_change': -8}) == 'weak'
        assert ds.classify_tech({}) == 'neutral'

    def test_基本面分级(self):
        assert ds.classify_fundamental(verdict='undervalued') == 'cheap'
        assert ds.classify_fundamental(verdict='overvalued') == 'expensive'
        assert ds.classify_fundamental(pe=10, pe_peer=20) == 'cheap'
        assert ds.classify_fundamental(pe=30, pe_peer=20) == 'expensive'
        assert ds.classify_fundamental() == 'fair'

    def test_pe_peer为零不崩(self):
        assert ds.classify_fundamental(pe=10, pe_peer=0) == 'fair'

    def test_背离识别(self):
        r = ds.link({'price': 80, 'ma20': 90, 'week_change': -3, 'month_change': -8},
                    {'verdict': 'undervalued'})
        assert r['direction'] == 'divergent' and '背离' in r['text']

    def test_同向识别(self):
        r = ds.link({'price': 100, 'ma20': 90, 'week_change': 3, 'month_change': 8},
                    {'verdict': 'undervalued'})
        assert r['direction'] == 'aligned'

    def test_双弱给风险提示(self):
        r = ds.link({'price': 80, 'ma20': 90, 'week_change': -3, 'month_change': -8},
                    {'verdict': 'overvalued'})
        assert '双弱' in r['stance'] and r['risks']

    def test_输出含口径提示(self):
        r = ds.link({}, {}, horizon='long')
        assert '长期' in r['text']

    def test_数据不足也有输出(self):
        r = ds.link({}, {})
        assert r['text'] and r['stance']


class Test行业变量:

    def test_汽车玻璃含用户点名的变量(self):
        v = ds.variables_for('汽车玻璃')
        names = ' '.join(x['name'] for x in v['variables'])
        for kw in ('单车玻璃价值量', '新能源渗透率', '纯碱', '天然气', '关税'):
            assert kw in names, kw

    def test_每个变量都有来源与理由(self):
        for x in ds.variables_for('汽车玻璃')['variables']:
            assert x['why'] and x['source']

    def test_未识别给通用清单并说明(self):
        v = ds.variables_for('某个看不懂的行业')
        assert v['generic'] is True and v['variables']
        assert '通用变量清单' in v['note']

    def test_渲染成表格(self):
        md = ds.render_variables(ds.variables_for('汽车玻璃'))
        assert '行业跟踪变量' in md and '为什么关键' in md

    def test_行业键与同业池一致(self):
        for key in ds.INDUSTRY_VARIABLES:
            assert key in pp.INDUSTRY_POOLS, key
