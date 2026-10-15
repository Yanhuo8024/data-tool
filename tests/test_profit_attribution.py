# -*- coding: utf-8 -*-
"""净利变动归因：必须给原因，取不到必须说清去哪找。"""

import profit_attribution as pa


class Test诊断:

    def test_下滑触发归因(self):
        d = pa.diagnose({'profit_growth': -35.0})
        assert d['needed'] is True and d['direction'] == 'down'

    def test_增长超阈值也触发(self):
        assert pa.diagnose({'profit_growth': 80.0})['direction'] == 'up'

    def test_平稳不触发(self):
        assert pa.diagnose({'profit_growth': 3.0})['needed'] is False

    def test_缺数据仍需归因且说明(self):
        d = pa.diagnose({})
        assert d['needed'] is True and d['direction'] == 'unknown'
        assert '缺少净利同比' in d['reason']

    def test_阈值可调(self):
        assert pa.diagnose({'profit_growth': -8.0}, threshold=5.0)['needed'] is True

    def test_嵌套结构也能读到同比(self):
        """净利同比常嵌在 financial_analysis.ratios.growth 下，必须递归读取，
        否则会因"读不到"而每次都触发归因（实测踩到）。"""
        d = pa.diagnose({'financial_analysis': {'ratios': {'growth': {'profit_growth': -35}}}})
        assert d['needed'] is True and d['direction'] == 'down'

    def test_嵌套平稳不触发(self):
        d = pa.diagnose({'financial_analysis': {'ratios': {'growth': {'profit_growth': 3}}}})
        assert d['needed'] is False

    def test_中文键也识别(self):
        assert pa.diagnose({'净利同比': -22})['direction'] == 'down'


class Test取数与缺口:

    def test_命中四个维度(self):
        snap = {'statements': {'财务费用': 8.6e8, '汇兑损失': 1.2e8,
                               '资产减值损失': 3.2e8, '境外收入': 50e8}}
        res = pa.collect(snap)
        assert len(res['hits']) == 4 and res['coverage'] == 1.0

    def test_缺科目记为缺口并给可执行建议(self):
        res = pa.collect({'statements': {}})
        assert not res['hits'] and len(res['gaps']) == 4
        g = res['gaps'][0]
        assert g['attempted'] and g['where'] and g['section']

    def test_不臆测页码(self):
        """无真实定位时不得给出页码，而要说明"需按目录核对、系统不臆测页码"。"""
        res = pa.collect({'statements': {}})
        for g in res['gaps']:
            assert '目录核对' in g['hint'] and '不臆测页码' in g['hint']
            assert not any(ch.isdigit() for ch in g['hint']), '提示里不应出现页码数字'

    def test_注入fetcher生效(self):
        def mine(key, snapshot, market, **kw):
            if key == 'finance_expense':
                return {'ok': True, 'value': 12.5, 'unit': '亿元',
                        'source': 'financial_statements', 'page': 88}
            return {'ok': False, 'error': '无此科目'}
        res = pa.collect({}, fetcher=mine)
        hit = res['hits'][0]
        assert hit['name'] == '财务费用' and hit['page'] == 88
        assert '第 88 页' in pa.render(res)

    def test_fetcher异常不中断其余维度(self):
        def boom(key, snapshot, market, **kw):
            raise RuntimeError('取数超时')
        res = pa.collect({}, fetcher=boom)
        assert len(res['gaps']) == 4
        assert any('RuntimeError' in (g.get('error') or '') for g in res['gaps'])

    def test_非数值字段不算命中(self):
        res = pa.collect({'statements': {'财务费用': None}})
        assert not res['hits']


class Test渲染与结论口径:

    def test_全缺口时结论降级为待补充(self):
        r = pa.analyze({'profit_growth': -30.0})
        assert r['verdict'] == 'pending_supplement'
        assert '不得写成' in r['text']

    def test_有命中时给出归因(self):
        r = pa.analyze({'profit_growth': -30.0,
                        'statements': {'财务费用': 8.6e8}})
        assert r['verdict'] == 'attributed'
        assert '已查证的影响项' in r['text']

    def test_平稳时不做归因(self):
        r = pa.analyze({'profit_growth': 2.0})
        assert r['verdict'] == 'not_needed' and r['hits'] == []

    def test_缺口文案含已尝试工具(self):
        r = pa.analyze({'profit_growth': -30.0})
        assert '已尝试' in r['text'] and '未获取' in r['text']
        assert '建议查' in r['text']


class TestMDNA:

    def test_抽取相关原句(self):
        txt = ('报告期内公司净利润同比下滑 35.2%，主要系汇兑损失增加所致。'
               '公司营业收入同比增长 4.1%。')
        pts = pa.extract_mdna_points(txt)
        assert len(pts) == 1 and '汇兑损失' in pts[0]['quote']

    def test_无关句子不抽(self):
        assert pa.extract_mdna_points('公司注册地址位于福建省。') == []

    def test_页码仅在有标记时给出(self):
        pts = pa.extract_mdna_points('第 42 页：净利润下降的主要原因是资产减值损失增加。')
        assert pts and pts[0]['page'] == 42

    def test_无原文时如实说明(self):
        assert '未从已获取文本中提取到' in pa.render_mdna([])

    def test_空文本返回空(self):
        assert pa.extract_mdna_points('') == []

    def test_analyze带mdna(self):
        r = pa.analyze({'profit_growth': -30.0},
                       mdna_text='净利润同比下降，主要系原材料价格上涨所致。')
        assert r['mdna_points'] and 'MD&A' in r['text']
