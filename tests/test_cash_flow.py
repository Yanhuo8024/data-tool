# -*- coding: utf-8 -*-
"""现金流补充：OCF 必展示 · FCF · 现金消耗速度与跑道。"""

import cash_flow as cf


class Test现金流体检:

    def test_造血正常(self):
        r = cf.analyze({'statements': {'ocf': 800, 'capex': 120,
                                       'cash': 2662.18, 'net_profit': 416.43}})
        assert r['ok'] is True
        assert r['fcf'] == 680.0
        assert r['ocf_ni'] == 1.921   # 800 / 416.43
        assert '造血正常' in r['verdict_cn']

    def test_烧钱给出现金跑道(self):
        r = cf.analyze({'statements': {'ocf': -10.0, 'capex': 120.0,
                                       'cash': 300.0, 'net_profit': -50.0}})
        assert r['burn'] == 130.0
        assert r['runway'] == 2.31
        assert '在烧钱' in r['verdict_cn']
        assert '还能撑' in r['verdict_cn']

    def test_只有OCF也必须展示(self):
        """🚨 用户核心要求：接口已获取就展示，不因缺 Capex 而整段隐藏。"""
        r = cf.analyze({'ocf': 250.0})
        assert r['ok'] is True
        assert r['fcf'] == 250.0
        assert r['fcf_is_approx'] is True
        assert 'Capex 未获取' in '；'.join(r['gaps'])
        assert '经营现金流净额（OCF）' in r['table_md']

    def test_capex缺失时FCF标注为近似(self):
        r = cf.analyze({'ocf': 100.0})
        assert '（近似）' in r['table_md']
        assert '偏乐观' in '；'.join(r['gaps'])

    def test_利润含金量存疑(self):
        r = cf.analyze({'ocf': 40, 'capex': 10, 'net_profit': 200})
        assert '含金量存疑' in r['verdict_cn']
        assert r['ocf_ni'] == 0.2

    def test_取不到OCF只说缺口不编造(self):
        r = cf.analyze({'foo': 'bar'})
        assert r['ok'] is False
        assert r['fcf'] is None
        assert 'financial_statements' in r['text']

    def test_嵌套结构可递归取值(self):
        r = cf.analyze({'a': {'b': {'c': [{'ocf': 88}, {'capex': 8}]}}})
        assert r['ok'] is True and r['fcf'] == 80.0

    def test_每股经营现金流不误抓(self):
        """`ocf_ps`（每股 OCF）不应被当成 OCF 总额。"""
        r = cf.analyze({'statements': {'ocf_ps': 5.0}})
        assert r['ok'] is False

    def test_中文别名可识别(self):
        r = cf.analyze({'经营活动产生的现金流量净额': 500,
                        '购建固定资产支付的现金': 100,
                        '货币资金': 900})
        assert r['ocf'] == 500 and r['capex'] == 100 and r['fcf'] == 400

    def test_净利润为零不崩(self):
        r = cf.analyze({'ocf': 10, 'capex': 5, 'net_profit': 0})
        assert r['ok'] is True and r['ocf_ni'] is None

    def test_渲染含表与结论(self):
        r = cf.analyze({'ocf': 800, 'capex': 120, 'net_profit': 400})
        assert '现金流体检' in r['text'] and r['verdict_cn'] in r['text']
