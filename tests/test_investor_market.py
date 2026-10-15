# -*- coding: utf-8 -*-
"""个性化投研 + 多市场财报口径。"""

import investor_profile as ip
import market_adapter as ma


class Test个性化投研:

    def test_三档风险权重分化(self):
        c = ip.method_weights(ip.build_profile(risk='conservative'))
        a = ip.method_weights(ip.build_profile(risk='aggressive'))
        assert c['dcf'] > a['dcf']              # 保守更看 DCF
        assert a['ps'] > c['ps']                # 积极更看 PS

    def test_权重归一(self):
        for risk in ip.RISK_LEVELS:
            w = ip.method_weights(ip.build_profile(risk=risk))
            assert abs(sum(w.values()) - 1.0) < 1e-3      # 保留 4 位小数，允许舍入误差
            assert all(v >= 0 for v in w.values())

    def test_期限影响DCF权重(self):
        short = ip.method_weights(ip.build_profile(horizon='short'))
        long_ = ip.method_weights(ip.build_profile(horizon='long'))
        assert long_['dcf'] > short['dcf']

    def test_目标影响权重(self):
        pres = ip.method_weights(ip.build_profile(goal='preservation'))
        spec = ip.method_weights(ip.build_profile(goal='speculation'))
        assert pres['pb'] > spec['pb']

    def test_取档与安全边际(self):
        rng = {'pessimistic': 44.9, 'neutral': 68.08, 'optimistic': 122.33}
        p = ip.build_profile(risk='conservative')
        r = ip.apply_to_sensitivity(rng, p)
        assert r['pick'] == 'pessimistic' and r['base_value'] == 44.9
        assert abs(r['entry_value'] - 44.9 * 0.7) < 1e-6

    def test_安全边际缺失时不崩(self):
        assert ip.apply_to_sensitivity(None, ip.build_profile())['entry_value'] is None

    def test_共识参考值随档位移动(self):
        c = {'low': 10.0, 'high': 20.0, 'median': 15.0}
        lo = ip.pick_consensus(c, ip.build_profile(risk='conservative'))['reference_value']
        hi = ip.pick_consensus(c, ip.build_profile(risk='aggressive'))['reference_value']
        assert lo == 12.5 and hi == 17.5

    def test_共识缺区间回落中位(self):
        assert ip.pick_consensus({'median': 15.0}, ip.build_profile())['reference_value'] == 15.0

    def test_矛盾组合被检出(self):
        p = ip.build_profile(risk='conservative', horizon='short', goal='speculation')
        assert p['consistency']['ok'] is False
        assert len(p['consistency']['issues']) >= 1

    def test_一致组合无告警(self):
        assert ip.build_profile(risk='balanced', horizon='mid', goal='growth')['consistency']['ok']

    def test_非法取值回落并告警(self):
        p = ip.build_profile(risk='没这个', horizon='也没', goal='还是没')
        assert p['risk'] == 'balanced' and p['horizon'] == 'mid' and p['goal'] == 'growth'
        assert len(p['warnings']) == 3

    def test_提示词片段含关键口径(self):
        frag = ip.prompt_fragment(ip.build_profile(risk='conservative'))
        assert '投资者画像' in frag and '安全边际' in frag and 'dcf' in frag

    def test_保守型禁用措辞约束(self):
        p = ip.build_profile(risk='conservative')
        assert p['notice_level'] == 'strong' and '禁止' in p['notice']

    def test_从文本推断(self):
        p = ip.infer_profile('我是保守型，打算长期持有')
        assert p and p['risk'] == 'conservative' and p['horizon'] == 'long'

    def test_无关键词不推断(self):
        assert ip.infer_profile('小米集团怎么样') is None

    def test_描述非空(self):
        assert '安全边际' in ip.describe(ip.build_profile())


class Test多市场口径:

    def test_市场推断(self):
        assert ma.infer_market('600519') == 'a'
        assert ma.infer_market('01810') == 'hk'
        assert ma.infer_market('HK1810') == 'hk'
        assert ma.infer_market('AAPL') == 'us'

    def test_代码规范化(self):
        assert ma.normalize_symbol('1810', 'hk') == 'HK01810'
        assert ma.normalize_symbol('HK1810', 'hk') == 'HK01810'
        assert ma.normalize_symbol('sh600519', 'a') == '600519'
        assert ma.normalize_symbol('aapl', 'us') == 'AAPL'

    def test_别名映射覆盖三地(self):
        assert ma.map_field('营业总收入') == 'revenue'
        assert ma.map_field('营业额') == 'revenue'
        assert ma.map_field('Total Revenue') == 'revenue'
        assert ma.map_field('股东应占溢利') == 'net_profit'
        assert ma.map_field('Net Income') == 'net_profit'

    def test_未知字段返回空(self):
        assert ma.map_field('香蕉价格') is None

    def test_字段映射表本地化优先(self):
        assert ma.financial_field_map('hk')['revenue'][0] == '营业额'
        assert ma.financial_field_map('us')['net_profit'][0] == 'Net Income'

    def test_港股币种不同需换算(self):
        r = ma.to_internal({'price': 23.94}, 'hk', fin_ccy='CNY')
        assert abs(r['snapshot']['price'] - 23.94 * ma.FX_TO_CNY['HKD']) < 1e-6
        assert any('币种' in w for w in r['warnings'])
        assert r['conversions'][0]['expr'].startswith('23.94 ×')

    def test_A股无需换算(self):
        r = ma.to_internal({'price': 1500.0, 'revenue': 1e11}, 'a')
        assert r['snapshot']['price'] == 1500.0 and r['warnings'] == []

    def test_美股双币种换算(self):
        r = ma.to_internal({'price': 230.0, 'revenue': 391035e6}, 'us', fin_ccy='USD')
        assert abs(r['snapshot']['price'] - 230.0 * ma.FX_TO_CNY['USD']) < 1e-6
        assert len(r['conversions']) >= 2

    def test_汇率覆盖生效(self):
        r = ma.to_internal({'price': 100.0}, 'us', fx_override={'USD': 7.0})
        assert r['snapshot']['price'] == 700.0

    def test_非法价格不崩只告警(self):
        r = ma.to_internal({'price': '不是数字'}, 'a')
        assert any('price' in w for w in r['warnings'])

    def test_非数值字段跳过换算(self):
        r = ma.to_internal({'revenue': None, 'note': '文本'}, 'us')
        assert r['snapshot'].get('note') == '文本'

    def test_美股财年提示(self):
        note = ma.period_align('FY2024', 'us')['note']
        assert '财年' in note and '不等于自然年' in note

    def test_规格含汇率日期(self):
        s = ma.market_spec('hk')
        assert s['fx_as_of'] == ma.FX_AS_OF and s['price_fin_same_ccy'] is False

    def test_覆盖记账币种(self):
        s = ma.market_spec('hk', fin_ccy='HKD')
        assert s['fin_ccy'] == 'HKD' and s['fin_ccy_overridden'] is True

    def test_对比表三行(self):
        rows = ma.caliber_diff()
        assert len(rows) == 3 and {r['市场'] for r in rows} == {'A 股', '港股', '美股'}

    def test_口径说明含汇率(self):
        assert '1 HKD' in ma.describe_market('hk')

    def test_取数失败不编造(self, monkeypatch):
        monkeypatch.setattr(ma, '_ak', lambda: None)
        r = ma.fetch_quote('600519', 'a')
        assert r['ok'] is False and 'akshare' in r['error']

    def test_取数异常如实上报(self, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError('网络超时')
        monkeypatch.setattr(ma, '_ak', lambda: type('AK', (), {'stock_zh_a_spot_em': staticmethod(boom)}))
        r = ma.fetch_quote('600519', 'a')
        assert r['ok'] is False and 'RuntimeError' in r['error']

    def test_港股财报如实说明缺口(self, monkeypatch):
        monkeypatch.setattr(ma, '_ak', lambda: type('AK', (), {})())
        r = ma.fetch_financials('01810', 'hk')
        assert r['ok'] is False and '披露易' in r['error']
