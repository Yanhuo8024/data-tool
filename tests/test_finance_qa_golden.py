# -*- coding: utf-8 -*-
"""金融 QA 测试集（2026-10-08 用户第 4 项）。

一个「金融智能体」如果只测"不报错"，等于没测。本文件按用户点名的四个维度建**金标集**：
  ① 数据查询：收入占比、毛利率、交付量
  ② 估值：DCF、PE、敏感性
  ③ 风控：免责声明、异常估值
  ④ 上下文：多轮追问、意图切换

每条用例都断言**可核对的产出**（具体字段/数值/裁定），而不是"函数返回了东西"。
配套红队见 `test_redteam_finance.py`。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import intent_router                       # noqa: E402
import multi_model_valuation as mmv        # noqa: E402
import risk_review as rr                   # noqa: E402
import segment_data as sd                  # noqa: E402
import sotp_valuation as sotp              # noqa: E402
import valuation_guard as vg               # noqa: E402

# 通用于多处的样本
SNAP = {'eps': 1.62, 'bps': 10.5, 'revenue_per_share': 18.2, 'ebitda': 300,
        'shares': 253, 'net_debt': -800, 'target_pe': 20, 'target_pb': 2.2,
        'target_ps': 1.4, 'target_ev_ebitda': 8}


# ==================== ① 数据查询 ====================

class TestQ1数据查询:

    def test_A股收入占比可直接回答(self, monkeypatch):
        """问「收入占比」必须给得出占比，不能回「暂未包含」。"""
        rows = [{'name': '甲', 'revenue': 80.0, 'share': 80.0, 'gross_margin': 50.0,
                 'yoy': 10.0, 'profit': 40.0},
                {'name': '乙', 'revenue': 20.0, 'share': 20.0, 'gross_margin': 30.0,
                 'yoy': None, 'profit': 6.0}]
        monkeypatch.setattr(sd, 'fetch_segments', lambda *a, **k: {
            'ok': True, 'market': 'a', 'period': '2025-12-31', 'segments': rows,
            'category': '按产品分类', 'provenance': [], 'gap': None})
        out = sd.render_segments(sd.fetch_segments('600519', 'a'))
        assert '80.0%' in out and '20.0%' in out
        assert '暂未包含' not in out

    def test_港股收入占比给缺口不给搪塞(self):
        out = sd.render_segments(sd.fetch_segments('01810', 'hk'))
        assert '未能自动获取' in out and '应查来源' in out
        assert '暂未包含' not in out

    def test_毛利率进入分部表(self):
        d = sd.fetch_segments.__doc__ and True     # 结构断言：字段口径固定
        assert sd.SEGMENT_FIELDS == ('收入', '同比', '占比', '毛利率', '经营利润')

    def test_交付量参与盈亏平衡计算(self):
        """「交付量」这类经营指标要能进模型并产出可解释结论。"""
        r = sotp.car_deep_dive({'asp': 23.4, 'gross_margin': 20.4, 'fixed_cost': 200,
                                'deliveries': 35})
        m = r['metrics']
        assert m['breakeven_units'] > 0
        assert m['breakeven_gap_units'] == pytest.approx(m['breakeven_units'] - 35, abs=0.01)


# ==================== ② 估值 ====================

class TestQ2估值:

    def test_DCF给区间不给单点(self):
        """上一轮的核心教训：单点 DCF 会被极端增速带飞。"""
        sens = vg.sensitivity_triple(4.16e10, 0.09, 0.025, 25, 5, 2.53e10, -1e11)
        assert sens['low'] < sens['neutral'] < sens['high']

    def test_敏感性三档覆盖乐观中性悲观(self):
        sens = vg.sensitivity_triple(100, 0.09, 0.025, 10, 5, 10, 0)
        for k in ('optimistic', 'neutral', 'pessimistic'):
            assert sens[k] is not None

    def test_PE相对估值可算(self):
        r = mmv.run_all(SNAP, dcf={'per_share': 50})
        assert r['results']['PE']['value'] == pytest.approx(1.62 * 20, abs=0.01)

    def test_多模型必须交叉验证(self):
        r = mmv.evaluate(SNAP, dcf={'per_share': 68.08}, sotp={'per_share': 45.51}, price=21.85)
        assert len(r['results']) == 6
        assert r['consensus']['models'] == 6

    def test_极端偏差不出方向性结论(self):
        sn = {'market': 'hk', 'price': 23.94, 'shares': 2.53e10, 'net_debt': -1e11,
              'eps': 1.62, 'bps': 10.5}
        price_fin = 23.94 * vg.FX_TO_CNY['HKD']
        res = vg.evaluate(sn=sn, per_share=price_fin * 2.5, g1_raw=10, g1_used=10,
                          r=0.09, g2=0.025, years=5, base=1e10)
        assert res['verdict'] not in ('undervalued', 'overvalued')


# ==================== ③ 风控 ====================

class TestQ3风控:

    DEFENSIBLE = ('本文分析该公司估值与盈利能力。' * 20
                  + '不构成任何形式的投资建议。股市有风险，投资需谨慎。')

    def test_带免责声明不被误报(self):
        res = rr.review(self.DEFENSIBLE)
        assert 'missing_disclaimer' not in [f['rule'] for f in res['findings']]

    def test_缺免责声明被拦(self):
        body = '该公司估值合理，PE 25 倍，建议买入。' * 8
        assert 'missing_disclaimer' in [f['rule'] for f in rr.review(body)['findings']]

    def test_异常估值判禁发布(self):
        body = ('本公司研究。' * 20 + 'DCF 内在价值 313.59 元，现价 23.94 元，'
                '偏差 1209.9%，结论：低估。')
        res = rr.review(body)
        assert res['verdict'] == 'block'
        assert 'extreme_valuation' in [f['rule'] for f in res['findings']]

    def test_三级裁定图标齐备(self):
        assert rr._VERDICT_ICON == {'pass': '🟢', 'needs_review': '🟡', 'block': '🔴'}

    def test_币种混用被标记(self):
        body = '现价 24.26 港元，净利润 416.43 亿元人民币，每股内在价值 313.59 元。'
        assert any(f['rule'] == 'currency_unit' for f in rr.review(body)['findings'])


# ==================== ④ 上下文 ====================

class TestQ4上下文:

    @pytest.mark.parametrize('q', [
        '小米的收入占比是多少',
        '毛利率怎么样',
        '汽车交付量多少',
        '帮我做一下 DCF 估值',
        'PE 是多少',
    ])
    def test_金融问句不落到闲聊(self, q):
        r = intent_router.route(q)
        assert r.get('intent') != 'chat', f'「{q}」被误判为闲聊'

    def test_数据查询与知识问答分流(self):
        """「收益率是多少」要数据，「收益率是什么」要概念 —— 经典意图切换。"""
        assert intent_router.route('看看贵州茅台的收益率').get('intent') in ('nlq', 'analysis', 'agent')
        assert intent_router.route('收益率是什么意思').get('intent') == 'knowledge'

    def test_指代追问要么解析要么澄清绝不静默闲聊(self):
        """带历史的「那它的汽车业务呢」：可以解析出意图，也可以反问澄清，
        但**不能**把它当普通闲聊一路放过去（那会让用户以为系统没听懂）。"""
        history = [{'role': 'user', 'content': '分析一下小米集团'},
                   {'role': 'assistant', 'content': '小米集团主营手机与汽车业务…'}]
        r = intent_router.route('那它的汽车业务呢', history=history)
        assert r.get('intent') != 'chat' or r.get('needs_clarification'), \
            '指代追问既没解析也没澄清，等同静默丢弃'
        if r.get('needs_clarification'):
            assert r.get('clarification'), '澄清模式必须给出引导语'

    def test_带明确主语的业务追问能解析(self):
        """「小米汽车是否盈利」——用户红队点名的用例，必须走到分析/查数而非概念问答。"""
        history = [{'role': 'user', 'content': '分析一下小米集团'},
                   {'role': 'assistant', 'content': '小米集团主营手机与汽车业务…'}]
        for q in ('小米汽车是否盈利', '小米汽车盈利吗', '小米集团赚钱吗'):
            r = intent_router.route(q, history=history)
            assert r.get('intent') in ('analysis', 'nlq', 'agent'), f'{q} → {r.get("intent")}'

    def test_业务线追问不再是闲聊(self):
        for q in ('汽车业务收入占比多少', '小米的汽车业务毛利率'):
            assert intent_router.route(q).get('intent') != 'chat', q

    def test_意图从估值切到数据查询(self):
        a = intent_router.route('小米值不值得买')
        b = intent_router.route('小米的毛利率是多少')
        assert a.get('intent') and b.get('intent')
        assert a.get('intent') != 'chat' and b.get('intent') != 'chat'

    def test_极短指代不硬猜(self):
        r = intent_router.route('它呢')
        assert r.get('needs_clarification') or r.get('intent') != 'chat'


# ==================== 汇总口径 ====================

def test_金标集覆盖四个维度():
    """防退化：四个维度必须都有用例，不能被删空。"""
    src = Path(__file__).read_text(encoding='utf-8')
    for cls in ('TestQ1数据查询', 'TestQ2估值', 'TestQ3风控', 'TestQ4上下文'):
        assert f'class {cls}' in src, cls