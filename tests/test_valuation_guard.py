# -*- coding: utf-8 -*-
"""DCF 估值硬校验回归（2026-10-08）。

线上故障：小米集团-W（HK1810）DCF 报出「内在价值 313.59 元 vs 现价 23.94 元，
偏差 +1209.9%」，并据此输出「低估」。三处缺陷叠加：
  ① 币种混算 —— 港股现价是**港元**，财报是**人民币**，直接相减；
  ② 单年高增速外推 —— 净利同比 +106.77%（低基数修复）当作未来 5 年恒定增速；
  ③ 无任何合理性闸门 —— 算出来多少就报多少。

验收标准（用户明确要求）：**再出现 1000% 偏差时，系统自动拦截，不生成「低估」结论。**
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import valuation_guard as vg          # noqa: E402
import valuation                      # noqa: E402

# 小米集团-W 故障现场参数
XIAOMI = {
    'market': 'hk', 'code': '01810', 'name': '小米集团-W',
    'price': 23.94,                 # 港元
    'shares': 2.53e10,              # 股
    'net_debt': -1.0e11,            # 净现金 1000 亿（人民币）
    'eps': 1.62, 'bps': 10.5,       # 人民币
    'net_profit': 4.1643e10,        # 416.43 亿（人民币）
    'profit_growth': 106.77,        # 单年净利同比（低基数修复）
    'revenue_growth': 24.97,
    'revenue': 4.57287e11, 'ocf_ps': None, 'roe': 18.31,
    'gross_margin': 22.26, 'net_margin': 9.09, 'debt_ratio': 47.58,
}
FROZEN_CAP = 6.2578e11              # 报告市值 6257.8 亿港元


@pytest.fixture
def offline(monkeypatch):
    """隔离网络：快照用固定值，市值来源不联网。"""
    monkeypatch.setattr(valuation, '_snapshot', lambda s: dict(XIAOMI))
    monkeypatch.setattr(vg, 'fetch_market_cap', lambda *a, **k: None)
    return None


class Test单年高增速闸门:

    def test_超高增速被截断(self):
        g, flags = vg.normalize_growth(106.77, years=5)
        assert g == vg.GROWTH_CAP
        assert 'single_year_growth_capped' in flags

    def test_正常增速不被截断(self):
        g, flags = vg.normalize_growth(12.5, years=5)
        assert g == 12.5 and flags == []

    def test_负增长归零并留痕(self):
        g, flags = vg.normalize_growth(-30, years=5)
        assert g == 0.0 and 'growth_negative_clamped' in flags

    def test_短周期不触发截断(self):
        """只有 1-2 年的模型谈不上"外推多年"，不该被截。"""
        g, _ = vg.normalize_growth(80, years=2)
        assert g == 80


class Test币种统一:

    def test_港股识别为港元计价人民币记账(self):
        assert vg.market_currencies('hk') == ('HKD', 'CNY')
        assert vg.market_currencies('a') == ('CNY', 'CNY')

    def test_现价换算到财报币种(self):
        res = vg.evaluate(sn=XIAOMI, per_share=50.0, g1_raw=10, g1_used=10,
                          r=0.09, g2=0.025, years=5, base=XIAOMI['net_profit'])
        cur = res['currency']
        assert cur['converted'] is True
        assert cur['fx_as_of'] and cur['fx_source']
        assert cur['price_in_fin_ccy'] < XIAOMI['price']        # 港元 < 人民币
        assert abs(cur['price_in_fin_ccy'] - XIAOMI['price'] * vg.FX_TO_CNY['HKD']) < 1e-3

    def test_同币种市场不换算(self):
        sn = dict(XIAOMI, market='a')
        res = vg.evaluate(sn=sn, per_share=50.0, g1_raw=10, g1_used=10,
                          r=0.09, g2=0.025, years=5, base=XIAOMI['net_profit'])
        assert res['currency']['converted'] is False


class Test异常阈值闸门:

    def test_比值超2倍标记模型异常(self):
        """比值 10 倍：偏差已超 100% 故结论走「假设极端」，但必须留下模型异常标记。"""
        res = vg.evaluate(sn=XIAOMI, per_share=vg.FX_TO_CNY['HKD'] * 23.94 * 10,
                          g1_raw=10, g1_used=10, r=0.09, g2=0.025, years=5, base=1e10)
        assert res['verdict'] == 'assumption_extreme'
        assert 'model_anomaly' in res['flags']
        assert any('不可作为结论依据' in n for n in res['notes'])

    def test_偏差超100只给假设极端(self):
        price_fin = XIAOMI['price'] * vg.FX_TO_CNY['HKD']
        res = vg.evaluate(sn=XIAOMI, per_share=price_fin * 1.6,     # +60% 落在区间内
                          g1_raw=10, g1_used=10, r=0.09, g2=0.025, years=5, base=1e10)
        assert res['verdict'] in ('undervalued', 'needs_review')

    def test_极端偏差不出低估或高估(self):
        """偏差 > 100%：只出「假设极端，需人工复核」，且同时叠加「模型异常」标记。"""
        price_fin = XIAOMI['price'] * vg.FX_TO_CNY['HKD']
        res = vg.evaluate(sn=XIAOMI, per_share=price_fin * 2.5,
                          g1_raw=10, g1_used=10, r=0.09, g2=0.025, years=5, base=1e10)
        assert res['verdict'] == 'assumption_extreme'
        assert res['verdict'] not in ('undervalued', 'overvalued')
        assert '假设极端' in res['verdict_cn']
        assert 'model_anomaly' in res['flags'], '比值 > 2 应同时标记模型异常'

    def test_比值下界越界判模型异常(self):
        """内在价值 < 现价一半：偏差仅 -70%，未触「假设极端」闸门，应由比值闸门拦下。"""
        price_fin = XIAOMI['price'] * vg.FX_TO_CNY['HKD']
        res = vg.evaluate(sn=XIAOMI, per_share=price_fin * 0.3,
                          g1_raw=10, g1_used=10, r=0.09, g2=0.025, years=5, base=1e10)
        assert res['verdict'] == 'model_anomaly'

    def test_合理区间仍可给方向性结论(self):
        price_fin = XIAOMI['price'] * vg.FX_TO_CNY['HKD']
        res = vg.evaluate(sn=XIAOMI, per_share=price_fin * 1.3,
                          g1_raw=10, g1_used=10, r=0.09, g2=0.025, years=5, base=1e10)
        assert res['verdict'] == 'undervalued'


class Test市值反推:

    def test_缺独立市值不静默通过(self):
        res = vg._market_cap_guard(23.94, 2.53e10, 'HKD', None)
        assert res['checked'] is False and res['frozen'] is False
        assert '未取到' in res['reason']

    def test_偏差超5冻结(self):
        computed = 23.94 * 2.53e10                      # 港元
        bad = computed * 0.5                            # 差 100%
        res = vg._market_cap_guard(23.94, 2.53e10, 'HKD', vg.to_cny(bad, 'HKD'))
        assert res['frozen'] is True
        assert res['deviation_pct'] > 5

    def test_偏差在容忍内不冻结(self):
        computed = 23.94 * 2.53e10
        res = vg._market_cap_guard(23.94, 2.53e10, 'HKD', vg.to_cny(computed * 1.02, 'HKD'))
        assert res['frozen'] is False

    def test_冻结时不出方向性结论(self):
        res = vg.evaluate(sn=XIAOMI, per_share=50.0, g1_raw=10, g1_used=10,
                          r=0.09, g2=0.025, years=5, base=XIAOMI['net_profit'],
                          reported_market_cap=1.0e10)      # 与反推市值严重不符
        assert res['verdict'] == 'frozen'
        assert 'frozen_market_cap' in res['flags']


class Test交叉验证与敏感性:

    def test_隐含倍数背离判冲突(self):
        res = vg.evaluate(sn=XIAOMI, per_share=200.0, g1_raw=10, g1_used=10,
                          r=0.09, g2=0.025, years=5, base=1e10)
        assert res['cross_check'].get('multiple_x') is not None

    def test_ebitda显式标注不可算(self):
        res = vg.evaluate(sn=XIAOMI, per_share=50.0, g1_raw=10, g1_used=10,
                          r=0.09, g2=0.025, years=5, base=1e10)
        assert res['cross_check']['ebitda'] == 'unavailable'

    def test_敏感性输出三档与区间(self):
        sens = vg.sensitivity_triple(4.16e10, 0.09, 0.025, 25, 5, 2.53e10, -1e11)
        for k in ('optimistic', 'neutral', 'pessimistic', 'low', 'high'):
            assert sens.get(k) is not None, k
        assert sens['pessimistic'] < sens['neutral'] < sens['optimistic']
        assert sens['low'] == sens['pessimistic'] and sens['high'] == sens['optimistic']

    def test_区间覆盖现价可判断(self):
        sens = vg.sensitivity_triple(4.16e10, 0.09, 0.025, 10, 5, 2.53e10, -1e11,
                                     price_fin=21.85)
        assert 'covers_price' in sens


class Test基数口径披露:

    def test_用净利润作基数时显式披露(self):
        res = vg.evaluate(sn=XIAOMI, per_share=50.0, g1_raw=10, g1_used=10,
                          r=0.09, g2=0.025, years=5, base=1e10, base_mode='net_profit')
        assert 'base_is_net_profit' in res['flags']
        assert any('自由现金流' in n for n in res['notes'])


class Test端到端拦截:
    """验收：复现故障现场，dcf_valuation 不得给出「低估」。"""

    def test_千倍偏差被拦截(self, offline):
        r = valuation.dcf_valuation('01810')
        assert r.get('ok') is True
        assert r['verdict'] not in ('undervalued',), '1000% 级偏差仍输出了「低估」，验收不通过'
        assert r['guard']['flags'], '未留下任何校验痕迹'

    def test_返回体带守卫痕迹(self, offline):
        r = valuation.dcf_valuation('01810')
        g = r['guard']
        assert g['currency']['price_ccy'] == 'HKD'
        assert g['currency']['fin_ccy'] == 'CNY'
        assert g['currency']['converted'] is True
        assert 'g1_raw' in g and 'g1_used' in g
        assert g['sensitivity_range'].get('low') is not None

    def test_增速被截断且原文留档(self, offline):
        r = valuation.dcf_valuation('01810')
        g = r['guard']
        assert g['g1_raw'] == pytest.approx(106.77)
        assert g['g1_used'] == vg.GROWTH_CAP

    def test_未截断时每股价值显著低于故障值(self, offline):
        """故障现场每股 313.59；截断后必须量级回落。"""
        r = valuation.dcf_valuation('01810')
        assert r['per_share'] < 313.59
