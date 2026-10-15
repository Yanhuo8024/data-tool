# -*- coding: utf-8 -*-
"""分部加总 / 多模型交叉 / 可解释性回归（2026-10-08 用户第 1、2、5 项）。

- 第 1 项：分业务估值（手机 PE·EV/EBITDA、IoT PE·PS、互联网 DCF、汽车 PS·EV-Sales），
  合计扣净负债；汽车单独深析六项指标。
- 第 2 项：六模型交叉，**DCF 与其他模型差异 > 50% 自动标记「模型冲突」**。
- 第 5 项：每个结论附依据与置信度、估值给区间不给单点、关键假设可调、报告版本化含数据截止日。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import multi_model_valuation as mmv      # noqa: E402
import report_evidence as ev             # noqa: E402
import sotp_valuation as sotp            # noqa: E402


# ==================== 第 1 项：分业务估值 ====================

class Test业务线方法匹配:
    """用户逐条指定了各业务该用什么方法，必须按此匹配。"""

    @pytest.mark.parametrize('name,expect', [
        ('智能手机', ('PE', 'EV/EBITDA')),
        ('IoT与生活消费产品', ('PE', 'PS')),
        ('互联网服务', ('DCF',)),
        ('智能电动汽车及创新业务', ('PS', 'EV/Sales')),
    ])
    def test_用户指定的方法(self, name, expect):
        assert tuple(sotp.classify_segment(name)) == expect

    def test_汽车同时支持PS与EV_Sales(self):
        assert 'PS' in sotp.classify_segment('汽车') and 'EV/Sales' in sotp.classify_segment('汽车')

    def test_互联网服务只走DCF(self):
        """高毛利、现金流稳定 —— 用利润类倍数会低估其现金流价值。"""
        assert sotp.classify_segment('互联网服务·广告') == ['DCF']

    def test_未知业务有兜底方法(self):
        assert sotp.classify_segment('某新业务') == ['PE', 'PS']

    def test_汽车识别(self):
        assert sotp.is_car_segment('智能电动汽车及创新业务')
        assert not sotp.is_car_segment('智能手机')


class Test分部加总:

    SEGS = [
        {'name': '智能手机', 'revenue': 2000, 'net_profit': 120, 'target_pe': 15},
        {'name': 'IoT与生活消费产品', 'revenue': 1000, 'net_profit': 40, 'target_pe': 20},
        {'name': '互联网服务', 'revenue': 350, 'fcf': 300, 'wacc': 9, 'g1': 8, 'g2': 2.5},
        {'name': '智能电动汽车及创新业务', 'revenue': 1000, 'target_ps': 2.0},
    ]

    def test_分部加总扣净负债(self):
        r = sotp.value_sotp(self.SEGS, net_debt=-800, shares=250)
        assert r['total_segment_value'] == pytest.approx(10576.92, abs=1)
        assert r['equity_value'] == pytest.approx(r['total_segment_value'] + 800, abs=1)
        assert r['per_share'] == pytest.approx(r['equity_value'] / 250, abs=0.01)

    def test_EV类方法先扣分部净负债(self):
        seg = [{'name': '汽车', 'revenue': 1000, 'target_ev_sales': 1.5, 'net_debt': 100}]
        r = sotp.value_sotp(seg, net_debt=0, shares=100)
        assert r['rows'][0]['value'] == pytest.approx(1400)
        assert 'EV/Sales' in r['rows'][0]['formula']

    def test_PE方法直接给权益价值(self):
        r = sotp.value_sotp([{'name': '手机', 'net_profit': 120, 'target_pe': 15}],
                            net_debt=0, shares=100)
        assert r['rows'][0]['value'] == pytest.approx(1800)

    def test_缺输入进缺口而非静默跳过(self):
        r = sotp.value_sotp([{'name': '手机', 'revenue': 2000}], net_debt=0, shares=100)
        assert r['gaps'], '缺输入必须显式披露'
        assert 'target_pe' in ' '.join(r['gaps']) or 'net_profit' in ' '.join(r['gaps'])

    def test_缺口压低置信度(self):
        good = sotp.value_sotp(self.SEGS, net_debt=0, shares=250)
        bad = sotp.value_sotp([{'name': '手机', 'revenue': 1000}], net_debt=0, shares=100)
        assert good['confidence']['score'] > bad['confidence']['score']

    def test_结论带依据与区间(self):
        r = sotp.value_sotp(self.SEGS, net_debt=-800, shares=250)
        c = r['claim']
        assert c['evidence'] and c['range'] and c['confidence']
        assert '分部加总' in c['method']

    def test_收入覆盖率可核算(self):
        r = sotp.value_sotp(self.SEGS, net_debt=0, shares=250)
        assert r['revenue_coverage_pct'] == 100.0

    def test_缺目标倍数时按缺口最小挑选(self):
        seg = [{'name': 'IoT', 'revenue': 1000, 'target_ps': 1.2}]
        r = sotp.value_sotp(seg, net_debt=0, shares=100)
        assert r['rows'][0]['method'] == 'PS'      # PS 输入齐备，优先于缺 net_profit 的 PE


class Test汽车业务专项:
    """用户点名六项：毛利率趋势 / 单车毛利 / 经营利润率 / 研发分摊 / 现金消耗 / 盈亏平衡点。"""

    CAR = {'gross_margin_trend': [('2024', 18.5), ('2025Q4', 20.4)], 'gross_margin': 20.4,
           'asp': 23.4, 'revenue': 1000, 'operating_profit': 30,
           'rd_total': 300, 'rd_share_pct': 25, 'ocf': 80, 'capex': 120,
           'fixed_cost': 200, 'deliveries': 35}

    def test_六项指标齐全(self):
        m = sotp.car_deep_dive(self.CAR)['metrics']
        for k in ('gross_margin_trend', 'per_unit_gross_profit', 'operating_margin_pct',
                  'rd_allocation', 'cash_burn', 'breakeven_units'):
            assert k in m, k

    def test_毛利率趋势判上行(self):
        t = sotp.car_deep_dive(self.CAR)['metrics']['gross_margin_trend']
        assert t['direction'] == '上行' and t['change_pp'] == pytest.approx(1.9)

    def test_毛利率下行触发警示(self):
        car = dict(self.CAR, gross_margin_trend=[('2024', 20.0), ('2025', 15.0)])
        assert 'gross_margin_warning' in sotp.car_deep_dive(car)['metrics']

    def test_单车毛利(self):
        assert sotp.car_deep_dive(self.CAR)['metrics']['per_unit_gross_profit'] == pytest.approx(4.776, abs=0.01)

    def test_经营利润率(self):
        assert sotp.car_deep_dive(self.CAR)['metrics']['operating_margin_pct'] == pytest.approx(3.0)

    def test_经营亏损被标出(self):
        car = dict(self.CAR, operating_profit=-50)
        assert 'operating_warning' in sotp.car_deep_dive(car)['metrics']

    def test_研发分摊(self):
        assert sotp.car_deep_dive(self.CAR)['metrics']['rd_allocation'] == pytest.approx(75.0)

    def test_研发比例记为可调假设(self):
        a = sotp.car_deep_dive(self.CAR)['assumptions']
        assert any(x['name'] == '汽车占研发比例' and x['adjustable'] for x in a)

    def test_现金消耗(self):
        m = sotp.car_deep_dive(self.CAR)['metrics']
        assert m['cash_burn'] == pytest.approx(-40.0) and m['cash_state'] == '净消耗'

    def test_自我造血(self):
        car = dict(self.CAR, capex=30)
        assert sotp.car_deep_dive(car)['metrics']['cash_state'] == '自我造血'

    def test_盈亏平衡点与差距(self):
        m = sotp.car_deep_dive(self.CAR)['metrics']
        assert m['breakeven_units'] == pytest.approx(200 / 4.776, abs=0.1)
        assert m['breakeven_gap_units'] > 0

    def test_缺项不编造(self):
        r = sotp.car_deep_dive({'revenue': 100})
        assert r['missing'] and 'per_unit_gross_profit' not in r['metrics']

    def test_结论给出售价方法建议(self):
        c = sotp.car_deep_dive(self.CAR)['claim']
        assert 'PS' in c['text'] and c['evidence']


# ==================== 第 2 项：多模型交叉验证 ====================

SNAP = {'eps': 1.62, 'bps': 10.5, 'revenue_per_share': 18.2, 'ebitda': 300,
        'shares': 253, 'net_debt': -800,
        'target_pe': 20, 'target_pb': 2.2, 'target_ps': 1.4, 'target_ev_ebitda': 8}
DCF = {'per_share': 68.08, 'base_mode': 'net_profit', 'g1': 25.0, 'r': 9.0}
SOTP_R = {'per_share': 45.51}


class Test六模型:

    def test_六个模型都定义(self):
        assert mmv.MODELS == ('DCF', 'PE', 'PB', 'PS', 'EV/EBITDA', 'SOTP')

    def test_全部可算(self):
        b = mmv.run_all(SNAP, dcf=DCF, sotp=SOTP_R)
        assert len(b['results']) == 6 and b['skipped'] == []

    def test_缺输入记入skipped不静默省略(self):
        b = mmv.run_all({'shares': 100}, dcf=None, sotp=None)
        assert set(b['skipped']) == set(mmv.MODELS)

    def test_各模型给出公式(self):
        b = mmv.run_all(SNAP, dcf=DCF, sotp=SOTP_R)
        for n, r in b['results'].items():
            assert r['formula'], n

    def test_EV_EBITDA扣净负债(self):
        b = mmv.run_all(SNAP, dcf=DCF)
        # (300*8 - (-800)) / 253
        assert b['results']['EV/EBITDA']['value'] == pytest.approx(3200 / 253, abs=0.01)

    def test_PE用目标倍数而非现价(self):
        b = mmv.run_all(SNAP, dcf=DCF)
        assert b['results']['PE']['value'] == pytest.approx(1.62 * 20, abs=0.01)


class Test模型冲突:

    def test_DCF与4个模型冲突超50标记冲突(self):
        r = mmv.evaluate(SNAP, dcf=DCF, sotp=SOTP_R, price=21.85)
        assert r['model_conflict'] is True
        assert len(r['conflicts']) >= 3
        assert r['threshold_pct'] == 50.0

    def test_冲突阈值可调(self):
        loose = mmv.cross_validate(mmv.run_all(SNAP, dcf=DCF), price=21.85, conflict_pct=95)
        assert loose['model_conflict'] is False

    def test_模型一致时不标冲突(self):
        """构造一组彼此接近的估值：各模型每股价值都在现价附近，且 DCF 不孤立。"""
        sn = dict(SNAP, target_pe=10, target_pb=1.5, target_ps=0.9)
        dcf = {'per_share': 16.5}
        r = mmv.cross_validate(mmv.run_all(sn, dcf=dcf), price=16.0)
        assert r['model_conflict'] is False
        assert all(c['deviation_pct'] <= 50 for c in r['comparisons'])

    def test_冲突时置信度降级(self):
        r = mmv.evaluate(SNAP, dcf=DCF, sotp=SOTP_R, price=21.85)
        assert r['confidence']['level'] == 'low'
        assert any('冲突' in x for x in r['confidence']['reasons'])

    def test_冲突写进结论警告(self):
        r = mmv.evaluate(SNAP, dcf=DCF, sotp=SOTP_R, price=21.85)
        assert any('模型冲突' in w for w in r['claim']['caveats'])

    def test_共识区间与离散度(self):
        r = mmv.evaluate(SNAP, dcf=DCF, sotp=SOTP_R, price=21.85)
        c = r['consensus']
        assert c['low'] < c['median'] < c['high']
        assert c['span_pct'] > 100 and c['models'] == 6

    def test_区间覆盖现价可判断(self):
        r = mmv.evaluate(SNAP, dcf=DCF, sotp=SOTP_R, price=21.85)
        assert r['consensus']['covers_price'] is True

    def test_结论带依据与区间(self):
        r = mmv.evaluate(SNAP, dcf=DCF, sotp=SOTP_R, price=21.85)
        assert len(r['claim']['evidence']) >= 6
        assert r['claim']['range'] is not None

    def test_缺失模型列出未纳入原因(self):
        r = mmv.evaluate(SNAP, dcf=None, sotp=None, price=21.85)
        assert 'DCF' in r['skipped']
        assert any('未纳入' in e for e in r['claim']['evidence'])


# ==================== 第 5 项：可解释性 ====================

class Test置信度推导:
    """置信度必须是可推导的客观量，不能是模型随口给的。"""

    def test_输入齐全多模型给高(self):
        c = ev.confidence(inputs_total=6, inputs_missing=0, method_count=3, source_tier=2)
        assert c['level'] == 'high'

    def test_缺输入降级(self):
        c = ev.confidence(inputs_total=6, inputs_missing=4, method_count=3)
        assert c['level'] != 'high'

    def test_离散度大降级(self):
        c = ev.confidence(inputs_total=6, inputs_missing=0, dispersion_pct=80, method_count=3)
        assert c['level'] == 'low'
        assert any('区间跨度' in r for r in c['reasons'])

    def test_来源层级压低上限(self):
        c = ev.confidence(inputs_total=6, inputs_missing=0, method_count=3, source_tier=5)
        assert c['level'] == 'low'

    def test_单模型不给高(self):
        c = ev.confidence(inputs_total=6, inputs_missing=0, method_count=1, source_tier=1)
        assert c['level'] != 'high'
        assert any('单一模型' in r for r in c['reasons'])

    def test_每次都给理由(self):
        for kw in ({}, {'inputs_total': 3, 'inputs_missing': 1}, {'method_count': 4}):
            assert ev.confidence(**kw)['reasons']


class Test结论结构:

    def test_结论带依据与置信度(self):
        c = ev.claim('每股内在价值 20 元', evidence=['DCF 计算：FCF 100 亿 × ...'],
                     method='DCF', value_range=(18, 22))
        assert c['evidence'] and c['confidence'] and c['range']['low'] == 18

    def test_无依据要标警告(self):
        c = ev.claim('这只股票会涨', evidence=[])
        assert any('缺少依据' in w for w in c['caveats'])

    def test_估值无区间要标警告(self):
        c = ev.claim('每股 20 元', evidence=['x'], method='DCF')
        assert any('区间' in w for w in c['caveats'])

    def test_区间自动排序并算跨度(self):
        c = ev.claim('x', evidence=['y'], value_range=(22, 18))
        assert c['range']['low'] == 18 and c['range']['high'] == 22
        assert c['range']['span_pct'] == pytest.approx(22.2, abs=0.1)

    def test_单点区间视为无区间(self):
        assert ev._norm_range(20) == {'low': 20.0, 'high': 20.0, 'mid': 20.0, 'span_pct': 0.0}


class Test假设与版本:

    def test_假设可调标记(self):
        a = ev.assumption('WACC', 9.0, unit='%', basis='行业平均')
        assert a['adjustable'] is True and a['unit'] == '%'

    def test_报告元信息含截止日与版本(self):
        m = ev.report_meta(as_of='2025-12-31', generated_at='2026-10-08 23:00:00')
        assert m['as_of'] == '2025-12-31'
        assert m['version'].startswith('v')
        assert m['generated_at'] == '2026-10-08 23:00:00'

    def test_未标注截止日显式说明(self):
        assert ev.report_meta()['as_of'] == '未标注'


class Test渲染与闸门:

    def test_渲染含区间依据与假设表(self):
        c = ev.claim('每股 20 元', evidence=['DCF'], method='DCF', value_range=(18, 22))
        out = ev.render([c], [ev.assumption('WACC', 9.0, unit='%')],
                        ev.report_meta(as_of='2025-12-31'))
        assert '区间：18' in out and '依据：DCF' in out
        assert '关键假设' in out and '数据截止日' in out

    def test_空输入不产出空标题(self):
        assert ev.render([], [], None) == ''

    def test_闸门拦住无依据结论(self):
        g = ev.gate([ev.claim('涨', evidence=[])])
        assert g['ok'] is False and g['no_evidence']

    def test_闸门拦住低置信度结论(self):
        bad = ev.claim('x', evidence=['y'], conf=ev.confidence(inputs_total=6, inputs_missing=5))
        assert ev.gate([bad])['ok'] is False

    def test_合格结论放行(self):
        good = ev.claim('每股 20 元', evidence=['DCF'],
                        conf=ev.confidence(inputs_total=6, inputs_missing=0,
                                           method_count=3, source_tier=2),
                        method='DCF', value_range=(18, 22))
        assert ev.gate([good])['ok'] is True