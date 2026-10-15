# -*- coding: utf-8 -*-
"""证据等级（A/B/C/D）· 工具口径优先级 · 核心结论闸门。"""

import evidence_grade as eg


class Test等级映射:

    def test_交易所公告为A(self):
        assert eg.grade_of('交易所公告')['grade'] == 'A'

    def test_财报接口为A(self):
        """用户口径：A = 财报原文 / **接口**。"""
        for s in ('financial_statements', 'financial_ratios', 'segment_breakdown'):
            assert eg.grade_of(s)['grade'] == 'A', s

    def test_权威终端为B(self):
        for s in ('akshare 行情', '东方财富', 'stock_valuation', 'company_analysis'):
            assert eg.grade_of(s)['grade'] == 'B', s

    def test_公开新闻为C(self):
        assert eg.grade_of('web_search')['grade'] == 'C'
        assert eg.grade_of('财经新闻报道')['grade'] == 'C'

    def test_百科为D(self):
        assert eg.grade_of('百度百科')['grade'] == 'D'
        assert eg.grade_of('file_rag')['grade'] == 'D'

    def test_未知来源不臆测(self):
        g = eg.grade_of('某个不认识的东西')
        assert g['grade'] == eg.UNGRADED and g['supports_core'] is False

    def test_只有AB可支撑核心(self):
        assert eg.grade_of('交易所公告')['supports_core'] is True
        assert eg.grade_of('code_terminal')['supports_core'] is False
        assert eg.grade_of('百科')['supports_core'] is False

    def test_工具名精确优先于关键词(self):
        """'公告解读' 含「公告」，若只走关键词会被误判成 A；带工具名时应取工具映射。"""
        assert eg.grade_of('公告解读（web_search）')['grade'] == 'C'
        assert eg.grade_of('web_search')['grade'] == 'C'


class Test工具口径优先级:

    def test_四个口径的首选工具(self):
        assert eg.preferred_tool('分部收入占比') == 'segment_breakdown'
        assert eg.preferred_tool('毛利率') == 'financial_ratios'
        assert eg.preferred_tool('总资产') == 'financial_statements'
        assert eg.preferred_tool('PE 估值') == 'stock_valuation'

    def test_用对工具为ok(self):
        r = eg.resolve_tool('分部收入', 'segment_breakdown')
        assert r['level'] == 'ok' and r['ok'] is True

    def test_web_search取口径数字被判补充(self):
        """web_search 只能作补充，不能作为分部收入的首选来源。"""
        r = eg.resolve_tool('分部收入', 'web_search')
        assert r['level'] == 'fallback'
        assert '仅作补充' in r['note']

    def test_用错工具为mismatch(self):
        r = eg.resolve_tool('商誉', 'company_analysis')
        assert r['level'] == 'mismatch' and 'financial_statements' in r['note']

    def test_描述含多口径时不猜(self):
        """🚨 段落里同时出现「分部」与「毛利率」→ 不给出误导性建议。"""
        assert eg.preferred_tool('公司分部收入 1340 亿元，毛利率 22.26%') is None
        r = eg.resolve_tool('公司分部收入 1340 亿元，毛利率 22.26%', 'web_search')
        assert r['level'] == 'unknown' and r['ok'] is False

    def test_未登记口径不参与校验(self):
        r = eg.resolve_tool('行业政策', 'web_search')
        assert r['level'] == 'unknown'


class Test核心结论闸门:

    def _rows(self, srcs):
        return [{'metric': m, 'value': v, 'source': s, 'tool': s}
                for (m, v, s) in srcs]

    def test_全AB级判达标(self):
        rows = self._rows([('分部收入', '1340 亿元', 'segment_breakdown'),
                           ('毛利率', '22.26%', 'financial_ratios')])
        r = eg.audit(rows)
        assert r['confidence'] == 'ok' and r['core_weak_count'] == 0

    def test_混入C级判置信度低(self):
        rows = self._rows([('分部收入', '1340 亿元', 'segment_breakdown'),
                           ('销量', '8 万辆', 'web_search')])
        r = eg.audit(rows)
        assert r['confidence'] == 'low'
        assert '置信度低' in r['verdict_cn']
        assert [w['metric'] for w in r['core_weak']] == ['销量']

    def test_核心全是CD级判不可用(self):
        """P2 要求：核心结论**只依赖** C/D 级来源 → 明确标不可用。"""
        rows = self._rows([('销量', '8 万辆', 'web_search'),
                           ('排名', '第三', '百度百科')])
        r = eg.audit(rows)
        assert r['confidence'] == 'invalid'
        assert '不得作为方向性依据' in r['verdict_cn']

    def test_非核心行是CD级不影响(self):
        rows = self._rows([('分部收入', '1340 亿元', 'segment_breakdown')])
        rows[0]['is_core'] = True
        r = eg.audit(rows + [{'metric': '花絮', 'value': '9 月', 'source': '百度百科'}],
                     core_values=['1340 亿元'])
        assert r['confidence'] == 'ok'

    def test_无核心证据为unknown(self):
        r = eg.audit([], core_values=[])
        assert r['confidence'] == 'unknown'

    def test_表格含证据等级列(self):
        rows = self._rows([('分部收入', '1340 亿元', 'segment_breakdown')])
        md = eg.audit(rows)['table_md']
        assert '证据等级' in md and '| **A**（' in md

    def test_渲染给出建议来源(self):
        rows = self._rows([('分部收入', '1340 亿元', 'web_search')])
        txt = eg.render_verdict(eg.audit(rows))
        assert '需补权威来源' in txt and 'segment_breakdown' in txt

    def test_口径未走首选工具会提示(self):
        rows = [{'metric': '分部收入', 'value': '1340 亿元',
                 'source': 'segment_breakdown', 'tool': 'web_search'}]
        r = eg.audit(rows)
        assert r['tool_mismatch'], '应记录口径未走首选工具'


# ---------- P0（2026-10-10 第四批）：上市公司核心科目走接口 ----------

def test_上市公司核心科目首选接口():
    """比亚迪/长城/吉利的营收、净利润、研发投入必须走 financial_statements。"""
    assert eg.preferred_tool('比亚迪营收') == 'financial_statements'
    assert eg.preferred_tool('长城汽车净利润') == 'financial_statements'
    assert eg.preferred_tool('吉利研发投入') == 'financial_statements'
    assert eg.preferred_tool('营收同比') == 'financial_statements'   # 同比不再是歧义词
    res = eg.resolve_tool('比亚迪营收', 'web_search')
    assert res['level'] == 'fallback'
