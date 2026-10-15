# -*- coding: utf-8 -*-
"""核验三元组 + 正文内联标注。"""

import fact_triplet as ft


TEXT = ('公司经营现金流/净利润=1.15，质量良好。\n'
        '2025 年营业收入 1340 亿元，同比增长 154.6%。\n')


class Test三元组:

    def test_构建三元组含报告原文(self):
        tr = ft.build_triplets(TEXT, [{'value': '1.15', 'source': 'company_deep_analyze'}])
        assert len(tr) == 1
        t = tr[0]
        assert '1.15' in t['quote'] and t['tool_value'] == '1.15'
        assert t['source_name'] == 'company_deep_analyze' and t['complete'] is True

    def test_定位不到则标记不完整(self):
        tr = ft.build_triplets(TEXT, [{'value': '999', 'source': 'x'}])
        assert tr[0]['complete'] is False and '未定位到' in tr[0]['quote']

    def test_来源缺省时用兜底说明(self):
        tr = ft.build_triplets(TEXT, [{'value': '1.15'}], sources=['financial_ratios'])
        assert tr[0]['source_name'] == 'financial_ratios'

    def test_渲染成表格(self):
        tr = ft.build_triplets(TEXT, [{'value': '1340', 'source': 'fin'}])
        md = ft.render_triplets(tr)
        assert '报告原文表述' in md and '工具返回' in md and '来源' in md

    def test_核验表含证据等级列(self):
        """P0 要求：核验表加「证据等级」列（A=财报原文/接口 … D=百科）。"""
        tr = ft.build_triplets(TEXT, [{'value': '1340', 'source': 'segment_breakdown'}])
        md = ft.render_triplets(tr)
        assert '证据等级' in md and 'C/D 级数字不得支撑核心结论' in md
        assert tr[0]['grade'] == 'A'

    def test_新闻来源被判C级(self):
        tr = ft.build_triplets(TEXT, [{'value': '1340', 'source': 'web_search'}])
        assert tr[0]['grade'] == 'C'

    def test_核心结论依赖新闻时提示置信度低(self):
        stats = {'total': 1, 'verified': 1, 'unverified': 0, 'ratio': 0.0,
                 'verified_data': [{'value': '1340', 'source': 'web_search'}],
                 'unverified_data': [], 'sources': ['web_search']}
        note = ft.build_report(TEXT, stats)['note']
        assert '置信度低' in note

    def test_超限只显示前若干条(self):
        items = [{'value': '1.15', 'source': 's'} for _ in range(20)]
        md = ft.render_triplets(ft.build_triplets(TEXT, items), limit=5)
        assert '另有 15 处' in md

    def test_竖线被转义(self):
        tr = ft.build_triplets('a|b 1.15 c', [{'value': '1.15', 'source': 's'}])
        assert '\\|' in ft.render_triplets(tr)


class Test内联标注:

    def test_标注未核验数字(self):
        r = ft.annotate_inline(TEXT, ['154.6%'])
        assert r['marked'] == 1 and '154.6% ⚠️' in r['text']

    def test_已核验数字不动(self):
        r = ft.annotate_inline(TEXT, ['999'])
        assert r['marked'] == 0 and r['text'] == TEXT

    def test_代码块内不标注(self):
        txt = '普通 154.6%\n```\n原始 154.6%\n```\n'
        r = ft.annotate_inline(txt, ['154.6%'])
        assert r['marked'] == 1
        assert '原始 154.6%\n' in r['text']

    def test_前缀不误伤(self):
        r = ft.annotate_inline('值 1.150 与 1.15', ['1.15'])
        assert r['marked'] == 1 and '1.150 ⚠️' not in r['text']

    def test_已标注不重复(self):
        r = ft.annotate_inline('1340 亿元 ⚠️', ['1340'])
        assert r['marked'] == 0

    def test_单位后带标记也不重复(self):
        r = ft.annotate_inline('154.6% ⚠️ 增长', ['154.6%'])
        assert r['marked'] == 0

    def test_同段两个数字各自标注(self):
        r = ft.annotate_inline('1340 亿元，同比增长 154.6%', ['1340', '154.6%'])
        assert r['marked'] == 2

    def test_空文本安全(self):
        assert ft.annotate_inline('', ['1'])['marked'] == 0

    def test_空未核验列表不报错(self):
        assert ft.annotate_inline(TEXT, [])['text'] == TEXT


class Test汇总:

    def test_未核验清单提示已就地标注(self):
        md = ft.render_unverified([{'value': '154.6%', 'reason': '未找到'}],
                                  inline_marked=True)
        assert '就地标注' in md and '154.6%' in md

    def test_build_report产出标注与三元组(self):
        stats = {'total': 3, 'verified': 2, 'unverified': 1, 'ratio': 0.33,
                 'verified_data': [{'value': '1.15', 'source': 'tool_a'}],
                 'unverified_data': [{'value': '154.6%', 'reason': '未找到'}],
                 'sources': ['tool_a']}
        rep = ft.build_report(TEXT, stats)
        assert rep['marked'] == 1 and '154.6% ⚠️' in rep['text']
        assert rep['triplets'][0]['tool_value'] == '1.15'
        assert '三元组' in rep['note']

    def test_全部核验时不标注(self):
        stats = {'total': 1, 'verified': 1, 'unverified': 0, 'ratio': 0.0,
                 'verified_data': [{'value': '1.15', 'source': 't'}],
                 'unverified_data': [], 'sources': ['t']}
        rep = ft.build_report(TEXT, stats)
        assert rep['marked'] == 0 and '均已对上' in rep['note']

    def test_无数字返回空说明(self):
        assert ft.render_note({'total': 0}, [], [], 0) == ''

    def test_可关闭标注(self):
        stats = {'total': 1, 'verified': 0, 'unverified': 1, 'ratio': 1.0,
                 'verified_data': [], 'unverified_data': [{'value': '154.6%'}],
                 'sources': []}
        rep = ft.build_report(TEXT, stats, annotate=False)
        assert rep['marked'] == 0


class Test数字抽取:

    def test_抽取百分号与千分位(self):
        nums = ft.extract_numbers('1,340 亿元 与 154.6%')
        assert '1,340' in nums and '154.6%' in nums

    def test_空文本(self):
        assert ft.extract_numbers('') == set()
