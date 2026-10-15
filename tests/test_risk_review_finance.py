# -*- coding: utf-8 -*-
"""金融风控检查回归（2026-10-08）。

## 修复的真实故障

报告里明明写着「本报告…**不构成任何形式的投资建议**或买卖要约…**股市有风险**，投资需谨慎」，
风控却报「🟡 缺少免责声明」。

根因：原实现是**精确子串匹配**固定短语表（`'不构成投资建议'`、`'投资有风险'`），
而真实文本在中间插了字（"任何**形式的**"）或换了搭配（"**股市**有风险"）→ 匹配失败。

另按用户要求补齐：币种/单位一致性、数据时效、单年高增速外推、极端估值结论、
风险提示段落，并把裁定明确为三级 🟢可发布 / 🟡需人工复核 / 🔴禁止发布。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import risk_review as rr                              # noqa: E402
from risk_checks_finance import (                     # noqa: E402
    check_currency_consistency,
    check_data_staleness,
    check_disclaimer_flexible,
    check_extreme_valuation,
    check_growth_extrapolation,
    check_missing_risk_section,
    find_disclaimer,
)

# 线上被误报的报告片段（免责声明部分逐字取自用户贴的真实输出）
XIAOMI_DISCLAIMER = (
    "## 免责声明\n"
    "本研究报告基于市场分析师阶段产出及公开信息编制，所引用的核心财务数据与经营指标均来源于"
    "小米集团2025年全年财报及相关公开披露信息（数据基准日：2025年12月31日）。\n"
    "本报告中的分析和观点仅代表分析师个人基于截至报告出具日的行业及公司状况所作的判断，"
    "不构成任何形式的投资建议或买卖要约。投资者应根据自身的财务状况、投资目标及风险承受能力"
    "做出独立决策。股市有风险，投资需谨慎。"
)
XIAOMI_BODY = (
    "# 小米集团-W（HK1810）最终研究报告\n\n## 一、投资结论与核心观点\n"
    "2025年全年营收达**4572.87亿元**（同比+24.97%），净利润**416.43亿元**（同比+76.02%）。\n\n"
    "## 四、风险提示\n1. 汽车业务盈利持续性风险；2. 存货减值风险。\n\n"
) + XIAOMI_DISCLAIMER


class Test免责声明弹性匹配:

    @pytest.mark.parametrize('phrase', [
        '不构成任何形式的投资建议或买卖要约',   # 本案误报原文
        '不构成投资建议',
        '不构成任何投资建议',
        '不构成投资依据',
        '本内容不作为投资决策依据',
        '股市有风险，投资需谨慎',               # 本案误报原文
        '市场有风险，入市需谨慎',
        '投资有风险，请谨慎决策',
        '风险自担',
        '仅供参考',
        '不保证收益',
        '据此操作，风险自负',
    ])
    def test_各类写法都能识别(self, phrase):
        assert find_disclaimer('前缀文字' + phrase + '后缀文字') is not None, phrase

    def test_线上被误报的报告不再报缺少免责声明(self):
        assert check_disclaimer_flexible(XIAOMI_BODY) is None

    def test_review_对真实报告不报缺免责声明(self):
        res = rr.review(XIAOMI_BODY)
        rules = [f['rule'] for f in res['findings']]
        assert 'missing_disclaimer' not in rules, '误报复现：报告含免责声明却被判缺失'

    def test_真缺失时仍会报出(self):
        body = '贵州茅台当前估值合理，PE 25 倍，建议买入。' * 4
        assert check_disclaimer_flexible(body) is not None

    def test_非金融内容不报(self):
        body = '今天天气不错，适合出门散步。' * 4
        assert check_disclaimer_flexible(body) is None


class Test币种单位一致性:

    def test_港元与人民币混用且无汇率口径被标记(self):
        body = '小米集团现价 24.26 港元，净利润 416.43 亿元人民币，每股内在价值 313.59 元。'
        r = check_currency_consistency(body)
        assert r is not None and r['rule'] == 'currency_unit'

    def test_注明汇率即放行(self):
        body = ('小米集团现价 24.26 港元，按汇率 0.91 折算为 22.08 元人民币，'
                '净利润 416.43 亿元人民币。')
        assert check_currency_consistency(body) is None

    def test_单一币种不报(self):
        assert check_currency_consistency('现价 1500 元，净利润 800 亿元。') is None


class Test数据时效:

    def test_报告期过旧被标记(self):
        body = '数据基准：2023年12月31日。该公司营收 100 亿元。'
        r = check_data_staleness(body, now_year=2026)
        assert r is not None and r['rule'] == 'data_staleness'

    def test_近期报告期不报(self):
        body = '数据基准：2025年12月31日。该公司营收 100 亿元。'
        assert check_data_staleness(body, now_year=2026) is None

    def test_无报告期不报(self):
        assert check_data_staleness('该公司营收 100 亿元。', now_year=2026) is None


class Test单年高增速外推:
    """本案 1209.9% 偏差的直接成因：把 +106.77% 的单年净利同比当 5 年恒定增速。"""

    def test_高增速用于长期假设被标记(self):
        body = '假设未来 5 年净利润增长率 106.77%，永续增长率 2.5%。'
        r = check_growth_extrapolation(body)
        assert r is not None and r['rule'] == 'growth_extrapolation'

    def test_温和增速不报(self):
        assert check_growth_extrapolation('假设未来 5 年营收增长率 12%，永续增长 2.5%。') is None

    def test_高增速但未外推不报(self):
        """单年同比本身是事实陈述，没有外推就不该报。"""
        assert check_growth_extrapolation('2025年净利润同比 +106.77%，主要系低基数修复。') is None

    def test_历史年份加同比不误报(self):
        """回归：早期版本把 '2025年' 当长期信号，含年份的事实句被大批误报。"""
        s = ('2025年全年营收达4572.87亿元（同比+24.97%），'
             '净利润416.43亿元（同比+76.02%），经调整净利润392亿元（同比+43.8%）。')
        assert check_growth_extrapolation(s) is None

    def test_预测语境才报(self):
        assert check_growth_extrapolation('预计 2026 年增长率将达 76.02%。') is not None

    def test_建议中给出截断上限(self):
        r = check_growth_extrapolation('预计长期增长率 80%')
        assert '上限' in r['advice'] and '三档' in r['advice']


class Test极端估值结论:

    def _body(self, tail):
        return ('小米集团-W 研究报告。' + '分析显示公司基本面稳健。' * 12 + tail)

    def test_极端偏差加方向性结论判high(self):
        body = self._body('DCF 内在价值 313.59 元，现价 23.94 元，偏差 1209.9%，结论：低估。')
        r = check_extreme_valuation(body)
        assert r is not None and r['level'] == 'high'

    def test_附带复核提示则放行(self):
        body = self._body('偏差 1209.9%，假设极端，需人工复核，不输出低估/高估判断。')
        assert check_extreme_valuation(body) is None

    def test_正常偏差不报(self):
        body = self._body('DCF 偏差 +35%，结论：低估。')
        assert check_extreme_valuation(body) is None

    def test_触发后裁定为禁止发布(self):
        body = self._body('DCF 内在价值 313.59 元，现价 23.94 元，偏差 1209.9%，结论：低估。')
        res = rr.review(body)
        assert res['verdict'] == 'block'
        assert res['level'] == 'high'


class Test风险提示段落:

    def test_缺风险段落的涉金融长文被标记(self):
        body = ('本报告分析该公司估值水平与盈利能力。' * 30)
        r = check_missing_risk_section(body)
        assert r is not None and r['rule'] == 'missing_risk_section'

    def test_有风险段落不报(self):
        body = ('本报告分析该公司估值水平与盈利能力。' * 20 + '\n## 风险提示\n估值假设风险。')
        assert check_missing_risk_section(body) is None


class Test三级裁定:

    @pytest.mark.parametrize('verdict,icon,text', [
        ('pass', '🟢', '可发布'),
        ('needs_review', '🟡', '需人工复核'),
        ('block', '🔴', '禁止发布'),
    ])
    def test_图标与文案(self, verdict, icon, text):
        assert rr._VERDICT_ICON[verdict] == icon
        assert text in rr._VERDICT_TEXT[verdict]

    def test_pass可渲染可发布(self):
        res = {'verdict': 'pass', 'summary': 'ok', 'findings': [
            {'rule': 'x', 'label': 'y', 'level': 'mid', 'hits': []}], 'handoff': False}
        assert '🟢' in rr.render_review(res) and '可发布' in rr.render_review(res)

    def test_block渲染禁止发布并提示转人工(self):
        res = {'verdict': 'block', 'summary': '有红线', 'findings': [
            {'rule': 'x', 'label': 'y', 'level': 'high', 'hits': []}], 'handoff': True}
        out = rr.render_review(res)
        assert '🔴' in out and '禁止发布' in out and '转人工' in out


class Test与既有规则共存:

    def test_收益承诺仍判high并拦截(self):
        body = '本产品保本保收益，年化 8%。' * 6
        res = rr.review(body)
        assert res['verdict'] == 'block'

    def test_荐股话术仍被拦(self):
        body = '建议立即买入该股票，目标价翻倍。' * 6
        res = rr.review(body)
        assert res['verdict'] == 'block'

    def test_清白的金融概述判可发布(self):
        body = ('本文将介绍市盈率与市净率的定义及适用场景。'
                '市盈率反映股价与每股收益的关系，市净率反映股价与每股净资产的关系。'
                '投资者应结合行业特征选择合适指标。仅供参考，不构成投资建议。')
        res = rr.review(body)
        assert res['verdict'] == 'pass'
