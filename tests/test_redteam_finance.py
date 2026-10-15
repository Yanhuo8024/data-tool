# -*- coding: utf-8 -*-
"""金融红队测试（2026-10-08 用户第 4 项）。

用户点名的四类攻击：
  ① 故意给极端参数
  ② 混淆币种
  ③ 问「小米汽车是否盈利」
  ④ 问「DCF 准不准」

红队的目标不是"不报错"，而是**系统必须扛住而不得输出误导性结论**：
极端输入要被闸门拦住、币种混淆要被识破、业务问题要真答而不是绕、
对方法的质疑要如实承认局限。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import intent_router                     # noqa: E402
import multi_model_valuation as mmv      # noqa: E402
import report_evidence as ev             # noqa: E402
import risk_review as rr                 # noqa: E402
import segment_data as sd                # noqa: E402
import sotp_valuation as sotp            # noqa: E402
import valuation_guard as vg             # noqa: E402

# 小米集团-W 故障现场（港股：股价港元、财报人民币）
XIAOMI = {'market': 'hk', 'code': '01810', 'name': '小米集团-W',
          'price': 23.94, 'shares': 2.53e10, 'net_debt': -1.0e11,
          'eps': 1.62, 'bps': 10.5}
PRICE_FIN = 23.94 * vg.FX_TO_CNY['HKD']


# ==================== ① 极端参数 ====================

class Test红队极端参数:

    def test_增长率拉满被截断(self):
        g, flags = vg.normalize_growth(9999, years=5)
        assert g == vg.GROWTH_CAP and 'single_year_growth_capped' in flags

    def test_天价内在价值被拦(self):
        res = vg.evaluate(sn=XIAOMI, per_share=PRICE_FIN * 100, g1_raw=10, g1_used=10,
                          r=0.09, g2=0.025, years=5, base=1e10)
        assert res['verdict'] not in ('undervalued', 'overvalued')
        assert 'model_anomaly' in res['flags']

    def test_极低内在价值被拦(self):
        res = vg.evaluate(sn=XIAOMI, per_share=PRICE_FIN * 0.001, g1_raw=10, g1_used=10,
                          r=0.09, g2=0.025, years=5, base=1e10)
        assert res['verdict'] not in ('undervalued', 'overvalued')

    def test_折现率不合法直接拒绝(self):
        assert vg.sensitivity_triple(100, 0.02, 0.03, 10, 5, 10, 0) == {} or \
            vg.sensitivity_triple(100, 0.02, 0.03, 10, 5, 10, 0).get('neutral') is None

    def test_负增长归零(self):
        assert vg.normalize_growth(-500, years=5)[0] == 0.0

    def test_零股本不崩且不给方向性结论(self):
        """守卫层不需要股本（per_share 已算好），但必须不崩、且不误给方向。"""
        res = vg.evaluate(sn=dict(XIAOMI, shares=0), per_share=10, g1_raw=5, g1_used=5,
                          r=0.09, g2=0.025, years=5, base=1e10)
        assert res['verdict'] in ('model_anomaly', 'nodata', 'assumption_extreme')
        assert res['market_cap']['checked'] is False      # 无股本 → 市值反推不可核验

    def test_空输入不崩(self):
        assert vg.evaluate(sn={}, per_share=None, g1_raw=0, g1_used=0,
                           r=0.09, g2=0.025, years=5, base=None)['verdict'] == 'nodata'


# ==================== ② 混淆币种 ====================

class Test红队混淆币种:
    """港股「股价港元 / 财报人民币」是最容易出错也最容易被忽略的陷阱。"""

    def test_币种自动识别并换算(self):
        res = vg.evaluate(sn=XIAOMI, per_share=50.0, g1_raw=10, g1_used=10,
                          r=0.09, g2=0.025, years=5, base=1e10)
        cur = res['currency']
        assert cur['price_ccy'] == 'HKD' and cur['fin_ccy'] == 'CNY'
        assert cur['converted'] is True
        assert cur['fx_as_of'] and cur['fx_source']

    def test_不换算会低估偏差量级(self):
        """港元换人民币后现价变小、分母变小 → 偏差百分比反而更大。不换算会**低估**偏差。"""
        naive = (50.0 - XIAOMI['price']) / XIAOMI['price'] * 100
        correct = (50.0 - PRICE_FIN) / PRICE_FIN * 100
        assert correct > naive

    def test_复现故障现场的1209_9(self):
        """线上报出的「偏差 1209.9%」正是不换算的结果 —— 取证闭环。"""
        reported = 313.59
        naive = (reported - XIAOMI['price']) / XIAOMI['price'] * 100
        assert round(naive, 1) == pytest.approx(1209.9, abs=0.1)
        converted = (reported - PRICE_FIN) / PRICE_FIN * 100
        assert converted > naive      # 正确换算后偏差更大，说明币种不是唯一病因

    def test_A股不误换算(self):
        res = vg.evaluate(sn=dict(XIAOMI, market='a'), per_share=50.0, g1_raw=10, g1_used=10,
                          r=0.09, g2=0.025, years=5, base=1e10)
        assert res['currency']['converted'] is False

    def test_风险检查能识破混用币种(self):
        body = ('小米集团现价 24.26 港元，2025 年净利润 416.43 亿元人民币，'
                'DCF 内在价值 313.59 元，较现价高 1209.9%。' * 3)
        rules = [f['rule'] for f in rr.review(body)['findings']]
        assert 'currency_unit' in rules, '混用港元与人民币却未标记'

    def test_注明汇率则放行(self):
        body = ('小米集团现价 24.26 港元，按汇率 0.9126 折算为 22.14 元人民币，'
                '净利润 416.43 亿元人民币。仅供参考，不构成投资建议。') * 3
        rules = [f['rule'] for f in rr.review(body)['findings']]
        assert 'currency_unit' not in rules


# ==================== ③ 「小米汽车是否盈利」 ====================

class Test红队汽车盈利提问:
    """用户点名：问业务问题不能绕回估值，也不能搪塞。"""

    @pytest.mark.parametrize('q', [
        '小米汽车是否盈利', '小米汽车盈利吗', '小米集团赚钱吗', '小米汽车亏损多少',
    ])
    def test_路由不落到闲聊或纯概念(self, q):
        r = intent_router.route(q)
        assert r.get('intent') in ('analysis', 'nlq', 'agent'), f'{q} → {r.get("intent")}'

    def test_实体识别到公司(self):
        from finance_intent import extract_financial_instruments
        assert '小米' in extract_financial_instruments('小米汽车是否盈利')

    def test_汽车专项直接回答盈利与否(self):
        r = sotp.car_deep_dive({'revenue': 1000, 'operating_profit': 30})
        assert r['metrics']['operating_margin_pct'] == pytest.approx(3.0)
        assert '已实现盈利' in r['claim']['text']

    def test_亏损时如实说亏损(self):
        r = sotp.car_deep_dive({'revenue': 1000, 'operating_profit': -50})
        assert '尚未稳定盈利' in r['claim']['text']
        assert 'operating_warning' in r['metrics']

    def test_数据缺时不编造(self):
        r = sotp.car_deep_dive({})
        assert r['missing'], '无数据时必须列缺口'
        assert 'operating_margin_pct' not in r['metrics']

    def test_汽车用PS而非PE(self):
        """未稳定盈利的业务用利润类倍数会失真。"""
        assert 'PE' not in sotp.classify_segment('智能电动汽车及创新业务')

    def test_收入占比问句能取到分部数据(self):
        """港股无接口 → 给可执行缺口（不是「暂未包含」）。"""
        out = sd.render_segments(sd.fetch_segments('01810', 'hk'))
        assert '暂未包含' not in out and '应查来源' in out


# ==================== ④ 「DCF 准不准」 ====================

class Test红队质疑DCF:
    """用户点名：被问「DCF 准不准」时，系统必须如实承认局限，而不是自证权威。"""

    def test_单点DCF被标为粗算口径(self):
        res = vg.evaluate(sn=XIAOMI, per_share=50.0, g1_raw=10, g1_used=10,
                          r=0.09, g2=0.025, years=5, base=1e10, base_mode='net_profit')
        assert 'base_is_net_profit' in res['flags']
        assert any('自由现金流' in n for n in res['notes'])

    def test_必须给区间不给单点(self):
        sens = vg.sensitivity_triple(4.16e10, 0.09, 0.025, 25, 5, 2.53e10, -1e11)
        assert sens['high'] > sens['low']
        assert sens['span_pct'] > 0

    def test_单模型不给高置信度(self):
        c = ev.confidence(inputs_total=6, inputs_missing=0, method_count=1, source_tier=1)
        assert c['level'] != 'high'
        assert any('单一模型' in r for r in c['reasons'])

    def test_模型分歧过大自动标冲突(self):
        sn = {'eps': 1.62, 'bps': 10.5, 'revenue_per_share': 18.2, 'ebitda': 300,
              'shares': 253, 'net_debt': -800, 'target_pe': 20, 'target_pb': 2.2,
              'target_ps': 1.4, 'target_ev_ebitda': 8}
        r = mmv.evaluate(sn, dcf={'per_share': 68.08}, sotp={'per_share': 45.51}, price=21.85)
        assert r['model_conflict'] is True
        assert r['confidence']['level'] == 'low'

    def test_结论必须能自证依据(self):
        r = mmv.evaluate({'eps': 1.62, 'target_pe': 20, 'shares': 100},
                         dcf={'per_share': 25.0}, price=30.0)
        assert r['claim']['evidence'], '结论没有依据'
        assert all('：' in e for e in r['claim']['evidence'])

    def test_置信度闸门拦住低质结论(self):
        low = ev.claim('每股 50 元', evidence=[''],
                       conf=ev.confidence(inputs_total=6, inputs_missing=5, method_count=1))
        assert ev.gate([low])['ok'] is False

    def test_区间未覆盖现价要提醒(self):
        sn = {'eps': 1.0, 'shares': 100, 'target_pe': 10}
        r = mmv.evaluate(sn, dcf={'per_share': 10.0}, price=999.0)
        assert any('未覆盖现价' in w for w in r['claim']['caveats'])


# ==================== 交叉：红队输入不得产出方向性结论 ====================

class Test红队端到端:

    def test_极端参数全链路不产出低估(self):
        """把上一轮的故障现场与更极端的参数一起喂进去，任何一条都不能出「低估」。"""
        cases = [PRICE_FIN * 2.5, PRICE_FIN * 10, PRICE_FIN * 100]
        for ps in cases:
            res = vg.evaluate(sn=XIAOMI, per_share=ps, g1_raw=106.77, g1_used=25,
                              r=0.09, g2=0.025, years=5, base=4.16e10)
            assert res['verdict'] != 'undervalued', f'每股 {ps} 仍输出了低估'

    def test_红队报告被风控拦截(self):
        body = ('小米集团研究报告。' * 20
                + '现价 24.26 港元，净利润 416.43 亿元人民币，'
                  'DCF 内在价值 313.59 元，偏差 1209.9%，结论：严重低估，建议买入。')
        res = rr.review(body)
        assert res['verdict'] in ('block', 'needs_review')
        rules = [f['rule'] for f in res['findings']]
        assert 'currency_unit' in rules
        assert 'extreme_valuation' in rules or 'trade_advice' in rules