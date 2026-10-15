# -*- coding: utf-8 -*-
"""估值口径统一 + DCF 敏感性矩阵。"""

import valuation_caliber as vc


def _per_share(base, r, g2, g1, years, shares, net_debt):
    pv = sum(base * ((1 + g1 / 100.0) ** t) / ((1 + r) ** t) for t in range(1, years + 1))
    tv = base * ((1 + g1 / 100.0) ** years) * (1 + g2) / (r - g2)
    return (pv + tv / ((1 + r) ** years) + net_debt) / shares


class TestPE口径:

    def test_TTM识别(self):
        assert vc.classify_pe_caliber('pe_ttm') == vc.PE_TTM
        assert vc.classify_pe_caliber('滚动市盈率') == vc.PE_TTM

    def test_全年识别(self):
        assert vc.classify_pe_caliber('pe_annual') == vc.PE_FY
        assert vc.classify_pe_caliber('静态市盈率') == vc.PE_FY

    def test_无提示不猜(self):
        assert vc.classify_pe_caliber('pe') == vc.PE_UNKNOWN
        assert vc.classify_pe_caliber() == vc.PE_UNKNOWN

    def test_口径不明不可横向比较(self):
        r = vc.label_pe(20.0, 'pe')
        assert r['comparable'] is False and '不得' in r['note']

    def test_口径明确可比较(self):
        assert vc.label_pe(20.0, 'pe_ttm')['comparable'] is True

    def test_带报告期(self):
        assert vc.label_pe(20.0, 'pe_ttm', period='2025Q3')['period'] == '2025Q3'


class Test市值核验:

    def test_通过(self):
        r = vc.verify_market_cap(price=23.94, shares=2.53e10,
                                 reported_cap=23.94 * 2.53e10,
                                 shares_source='akshare stock_individual_info_em · 总股本')
        assert r['ok'] is True and abs(r['deviation_pct']) < 0.01
        assert r['traceable'] is True

    def test_偏差超容差不通过(self):
        r = vc.verify_market_cap(price=23.94, shares=2.53e10,
                                 reported_cap=23.94 * 2.53e10 * 0.8,
                                 shares_source='akshare · 总股本')
        assert r['ok'] is False and '待补充' in r['reason']

    def test_容差可调(self):
        r = vc.verify_market_cap(price=100.0, shares=1e6, reported_cap=1.06e8, tol=0.10,
                                 shares_source='公司年报 股本变动表')
        assert r['ok'] is True

    def test_总股本来源未标注则降级(self):
        """🚨 数值对得上但股本来源不明 → 不可追溯，仍须降级（用户 2026-10-09 要求）。"""
        r = vc.verify_market_cap(price=23.94, shares=2.53e10,
                                 reported_cap=23.94 * 2.53e10)
        assert r['ok'] is False
        assert r['traceable'] is False
        assert '总股本来源未标注' in r['reason']

    def test_缺参数给明确原因(self):
        r = vc.verify_market_cap(price=None, shares=1e6, reported_cap=1e8)
        assert r['ok'] is False and '缺少 price' in r['reason']

    def test_入参非数值(self):
        r = vc.verify_market_cap(price='abc', shares=1e6, reported_cap=1e8)
        assert r['ok'] is False and '非数值' in r['reason']

    def test_市值为零不崩(self):
        r = vc.verify_market_cap(price=10.0, shares=1e6, reported_cap=0)
        assert r['ok'] is False and '为 0' in r['reason']


class Test结论闸门:

    def _pe(self, ok=True):
        return vc.label_pe(20.0, 'pe_ttm' if ok else 'pe')

    def _cap(self, ok=True):
        return vc.verify_market_cap(price=10.0, shares=1e6,
                                    reported_cap=1e7 if ok else 8e6,
                                    shares_source='akshare · 总股本')

    def test_全通过保留原结论(self):
        r = vc.gate_conclusion('高估', pe=self._pe(), market_cap=self._cap())
        assert r['verdict'] == 'ok' and r['verdict_cn'] == '高估'

    def test_PE口径不明降级(self):
        r = vc.gate_conclusion('高估', pe=self._pe(False), market_cap=self._cap())
        assert r['verdict'] == 'pending_supplement' and 'PE 口径' in r['text']

    def test_市值未核验降级(self):
        r = vc.gate_conclusion('高估', pe=self._pe(), market_cap=self._cap(False))
        assert r['verdict'] == 'pending_supplement'

    def test_未提供市值降级(self):
        r = vc.gate_conclusion('高估', pe=self._pe(), market_cap=None)
        assert r['verdict'] == 'pending_supplement' and '未做交叉核验' in r['text']

    def test_多问题同时列出(self):
        r = vc.gate_conclusion('高估', pe=self._pe(False), market_cap=None)
        assert len(r['issues']) == 2


class Test敏感性矩阵:

    def test_三乘三矩阵(self):
        m = vc.sensitivity_matrix(_per_share, base=4.1643e10, shares=2.53e10,
                                  net_debt=-1.0e11, g1=25.0, years=5, price=23.94)
        assert len(m['cells']) == 3 and all(len(r) == 3 for r in m['cells'])
        assert m['low'] is not None and m['high'] >= m['low']

    def test_折现率越高估值越低(self):
        m = vc.sensitivity_matrix(_per_share, base=1e10, shares=1e8, net_debt=0,
                                  g1=5.0)
        assert m['cells'][0][1] > m['cells'][-1][1]

    def test_永续增长越高估值越高(self):
        m = vc.sensitivity_matrix(_per_share, base=1e10, shares=1e8, net_debt=0, g1=5.0)
        assert m['cells'][1][-1] > m['cells'][1][0]

    def test_自定义轴(self):
        m = vc.sensitivity_matrix(_per_share, base=1e10, shares=1e8, net_debt=0,
                                  g1=5.0, rates=(7.0, 9.0), growths=(2.0,))
        assert m['rates'] == [7.0, 9.0] and len(m['cells']) == 2

    def test_覆盖现价判定(self):
        m = vc.sensitivity_matrix(_per_share, base=1e10, shares=1e8, net_debt=0,
                                  g1=5.0, price=100.0)
        mid = m['cells'][1][1]
        assert m['covers_price'] == (m['low'] <= 100.0 <= m['high'])
        assert mid is not None

    def test_单格失败不影响整表(self):
        def flaky(base, r, g2, g1, years, shares, net_debt, **kw):
            if abs(r - 0.09) < 1e-9:
                raise ValueError('该参数下有除零')
            return 10.0
        m = vc.sensitivity_matrix(flaky, base=1e10, shares=1e8, net_debt=0, g1=5.0)
        assert m['cells'][1] == [None, None, None]
        assert m['cells'][0][0] == 10.0

    def test_渲染表头与加粗(self):
        m = vc.sensitivity_matrix(_per_share, base=1e10, shares=1e8, net_debt=0,
                                  g1=5.0, price=15.0)
        assert '折现率 \\ 永续增长' in m['table_md'] and '|' in m['table_md']

    def test_解读句含区间(self):
        m = vc.sensitivity_matrix(_per_share, base=1e10, shares=1e8, net_debt=0, g1=5.0)
        d = vc.describe_matrix(m)
        assert '每股价值区间' in d

    def test_无值时不崩(self):
        m = vc.sensitivity_matrix(lambda *a, **k: None, base=1, shares=1, net_debt=0, g1=0)
        assert m['low'] is None and '未产出' in vc.describe_matrix(m)

    def test_隐含有最优假设(self):
        m = vc.sensitivity_matrix(_per_share, base=1e10, shares=1e8, net_debt=0,
                                  g1=5.0, price=20.0)
        assert m['implied'] and 'rate_pct' in m['implied']
