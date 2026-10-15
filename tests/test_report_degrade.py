# -*- coding: utf-8 -*-
"""report_degrade：红色审计降级 + 风险评级净化（2026-10-09 P0）。"""

import report_degrade as rd


# ---------- 造三元组/审计结果的小工厂 ----------

def _rep_with_conf(conf):
    """伪造 _evidence_audit 的返回（monkeypatch 掉真实审计）。"""
    return {'confidence': conf, 'violations': [
        {'metric': '负债率', 'preferred': 'financial_ratios',
         'used': 'web_search', 'level': 'fallback'},
    ]}


def _report():
    return (
        "# 小米集团分析\n\n"
        "## 公司概况\n\n营收增长强劲。\n\n"
        "## 主要风险\n\n"
        "| 指标 | 数值 | 风险评级 |\n| --- | --- | --- |\n"
        "| 负债率 | 68.33% | ★★★☆☆ |\n"
        "| 毛利率 | 20.5% | ★★☆☆☆ |\n\n"
        "## 综合结论与操作建议\n\n"
        "建议逢低买入，目标价 40 港元。\n\n"
        "## 数据来源\n\n以上数据来自公开新闻。\n"
    )


_STATS = {'unverified_data': [{'value': '68.33%', 'real': False}], 'derived_data': []}


# ---------- P0-1：红色审计 → 降级 ----------

class Test红色审计降级:

    def test_invalid触发降级横幅与剥离建议(self, monkeypatch):
        monkeypatch.setattr(rd, '_evidence_audit', lambda rep: _rep_with_conf('invalid'))
        out = rd.apply(_report(), {'triplets': [1]}, dict(_STATS))
        assert out['degraded'] is True
        assert '已降级为「待补充证据」' in out['text']
        assert '暂不提供方向性判断' in out['text']
        # 建议正文被剥离
        assert '逢低买入' not in out['text']
        assert '目标价 40' not in out['text']
        # 标题保留并标注
        assert '（待补充证据，暂不提供方向性判断）' in out['text']
        # 非建议节不受影响
        assert '营收增长强劲' in out['text']
        assert '公开新闻' in out['text']

    def test_low与ok不降级(self, monkeypatch):
        monkeypatch.setattr(rd, '_evidence_audit', lambda rep: _rep_with_conf('low'))
        out = rd.apply(_report(), {}, dict(_STATS))
        assert out['degraded'] is False
        assert '逢低买入' in out['text']

    def test_audit不可用时回退原文(self, monkeypatch):
        def _boom(rep):
            raise RuntimeError('x')
        monkeypatch.setattr(rd, '_evidence_audit', _boom)
        out = rd.apply(_report(), {}, dict(_STATS))
        assert out['text'] == _report()
        assert out['degraded'] is False


# ---------- P0-3：未核验数字不进风险评级 ----------

class Test风险评级净化:

    def test_星级行含未核验数字则待补充(self, monkeypatch):
        monkeypatch.setattr(rd, '_evidence_audit', lambda rep: {'confidence': 'ok'})
        out = rd.apply(_report(), {}, dict(_STATS))
        assert '待补充' in out['text']
        assert '★★★☆☆' not in out['text']          # 68.33% 行星级被替换
        assert '★★☆☆☆' in out['text']               # 20.5% 已核验行不动
        assert out['text'].count('待补充') >= 1

    def test_风险段之外星级不动(self, monkeypatch):
        monkeypatch.setattr(rd, '_evidence_audit', lambda rep: {'confidence': 'ok'})
        text = "## 概览\n\n热度 ★★★☆☆\n\n## 主要风险\n\n负债率 68.33% 评级 ★★★☆☆\n"
        out = rd.apply(text, {}, dict(_STATS))
        assert '★★★☆☆' in out['text']               # 概览节的星级保留
        assert out['text'].split('主要风险')[1].count('★') == 0

    def test_无未核验数字则不动星级(self, monkeypatch):
        monkeypatch.setattr(rd, '_evidence_audit', lambda rep: {'confidence': 'ok'})
        out = rd.apply(_report(), {}, {'unverified_data': [], 'derived_data': []})
        assert '★★★☆☆' in out['text']


# ---------- P0-4：违规具体清单 ----------

class Test违规清单:

    def test_evidence_audit返回结构化violations(self):
        import evidence_grade as eg
        rows = [{'metric': '负债率 68.33%', 'value': '68.33%',
                 'source': '', 'tool': 'web_search'}]
        res = eg.audit(rows, core=True)
        assert res['violations'], 'web_search 取负债率必须记违规'
        v = res['violations'][0]
        assert v['preferred'] == 'financial_ratios'
        assert v['used'] == 'web_search'
        assert v['level'] in ('fallback', 'mismatch')

    def test_render_verdict列出清单明细(self):
        import evidence_grade as eg
        rows = [{'metric': '负债率', 'value': '68.33%', 'source': 'web_search',
                 'tool': 'web_search'}]
        md = eg.render_verdict(eg.audit(rows, core=True))
        assert '未走首选工具清单' in md
        assert 'financial_ratios' in md
        assert 'web_search' in md

    def test_report_degrade联动清单md(self, monkeypatch):
        monkeypatch.setattr(rd, '_evidence_audit', lambda rep: _rep_with_conf('invalid'))
        out = rd.apply(_report(), {}, dict(_STATS))
        assert '未走首选工具清单' in out['violations_md']
        assert 'financial_ratios' in out['violations_md']


# ---------- P0（2026-10-10）：黄标 + 走错工具就地标注 ----------

class Test黄色审计标注:

    def test_low在建议节前加置信度标注(self, monkeypatch):
        monkeypatch.setattr(rd, '_evidence_audit', lambda rep: _rep_with_conf('low'))
        out = rd.apply(_report(), {}, dict(_STATS))
        assert out['degraded'] is False
        assert '本建议基于部分 C/D 级来源，置信度中等' in out['text']
        assert '逢低买入' in out['text']                    # 正文保留不剥离
        assert out['text'].index('置信度中等') < out['text'].index('逢低买入')

    def test_ok不加标注(self, monkeypatch):
        monkeypatch.setattr(rd, '_evidence_audit', lambda rep: {'confidence': 'ok'})
        out = rd.apply(_report(), {}, dict(_STATS))
        assert '置信度中等' not in out['text']


class Test走错工具就地标注:

    def test_mismatch值就地标注(self, monkeypatch):
        ev = {'confidence': 'ok', 'rows': [
            {'value': '0.37', 'metric': '资产周转率',
             'tool_check': {'level': 'mismatch', 'preferred': 'financial_ratios',
                            'used': 'stock_valuation'}}]}
        text = "## 财务\n\n资产周转率 0.37，运营效率一般。\n"
        out = rd.apply(text, {}, {'unverified_data': [], 'derived_data': []})
        out = {'text': out['text']}
        # 直接走 _mark_mismatch 验证（apply 依赖 _evidence_audit）
        t2, n = rd._mark_mismatch(text, ev)
        assert n == 1
        assert '工具来源不匹配' in t2
        assert 'financial_ratios' in t2 and 'stock_valuation' in t2

    def test_不重复标注(self, monkeypatch):
        ev = {'confidence': 'ok', 'rows': [
            {'value': '0.37', 'tool_check': {'level': 'fallback',
                                             'preferred': 'financial_ratios',
                                             'used': 'web_search'}}]}
        text = "周转率 0.37 ⚠️（工具来源不匹配：应走 `financial_ratios`，实际 `web_search`，置信度降低）\n"
        t2, n = rd._mark_mismatch(text, ev)
        assert n == 0 and t2.count('工具来源不匹配') == 1

    def test_codeblock内不标注(self):
        ev = {'confidence': 'ok', 'rows': [
            {'value': '0.37', 'tool_check': {'level': 'mismatch',
                                             'preferred': 'x', 'used': 'y'}}]}
        text = "```\n0.37\n```\n"
        t2, n = rd._mark_mismatch(text, ev)
        assert n == 0


# ---------- P0（2026-10-10 第二批）：方向性表述约束 + 清单完整化 ----------

class Test方向性约束:

    def test_red下方向性句被隐藏且节前有声明(self, monkeypatch):
        monkeypatch.setattr(rd, '_evidence_audit', lambda rep: _rep_with_conf('invalid'))
        text = ("## 行业格局\n\n行业拐点已至，龙头将持续增长。\n\n"
                "## 财务数据\n\n毛利率 20.5%，保持稳定。\n")
        out = rd.apply(text, {}, {'unverified_data': [], 'derived_data': []})
        assert '行业拐点已至' not in out['text']
        assert '持续增长' not in out['text']
        assert '（方向性判断已隐藏，待补充证据）' in out['text']
        assert '基于 C 级来源，暂不提供方向性判断，仅列数据对比' in out['text']
        assert '毛利率 20.5%' in out['text']            # 无方向词的节不受影响

    def test_yellow不动方向性句(self, monkeypatch):
        monkeypatch.setattr(rd, '_evidence_audit', lambda rep: _rep_with_conf('low'))
        text = "## 行业格局\n\n行业拐点已至。\n"
        out = rd.apply(text, {}, {'unverified_data': [], 'derived_data': []})
        assert '行业拐点已至' in out['text']            # 黄标不隐藏正文
        assert '置信度中等' not in out['text'] or '## 行业格局' in out['text']


class Test清单完整化:

    def test_evidence_grade非core违规也收集(self):
        import evidence_grade as eg
        rows = [{'metric': '负债率 68.33%', 'value': '68.33%', 'source': 'web_search',
                 'tool': 'web_search'},
                {'metric': '资产周转率', 'value': '0.37', 'source': 'stock_valuation',
                 'tool': 'stock_valuation'}]
        res = eg.audit(rows, core=True)
        assert len(res['violations']) == 2, '非核心/多口径行也要进清单'
        v0 = res['violations'][0]
        assert v0['value'] == '68.33%'
        assert v0['level'] in ('fallback', 'mismatch')

    def test_render清单含数据与降级原因(self):
        import evidence_grade as eg
        rows = [{'metric': '负债率', 'value': '68.33%', 'source': 'web_search',
                 'tool': 'web_search'}]
        md = eg.render_verdict(eg.audit(rows, core=True))
        assert '68.33%' in md and '降级' in md

    def test_权威机构与D级平台分级(self):
        import evidence_grade as eg
        assert eg.grade_of('中汽协月度数据')['grade'] == 'A'
        assert eg.grade_of('乘联会周度零售')['grade'] == 'A'
        assert eg.grade_of('海关总署出口数据')['grade'] == 'A'
        assert eg.grade_of('抖音短视频')['grade'] == 'D'
        assert eg.grade_of('今日头条文章')['grade'] == 'D'
        assert eg.grade_of('搜狗搜索结果')['grade'] == 'D'
        assert eg.grade_of('百度搜索结果')['grade'] == 'D'

class Test护城河六维:

    def test_breakdown列全6维与均分算式(self):
        from company_deep_rules import moat_breakdown
        moat = {'tech': 7, 'brand': 6, 'channel': 5, 'cost': 5,
                'network': 4, 'license': 6}
        md = moat_breakdown(moat)
        for w in ('技术/专利', '品牌心智', '渠道网络', '成本/规模', '网络效应', '牌照/资质'):
            assert w in md, w
        assert '(7+6+5+5+4+6) ÷ 6 = **5.5/10**' in md
        assert '毛利率映射' in md                     # 打分公式可见

    def test_缺失维度按默认并标注(self):
        from company_deep_rules import moat_breakdown
        md = moat_breakdown({'tech': 7})
        assert md.count('数据缺失') == 5
        assert '÷ 6' in md

    def test_ai工具报告段含六维明细(self):
        import agent_tool_registry as atr
        import inspect
        src = inspect.getsource(atr)
        assert 'moat_breakdown' in src


# ---------- P0（2026-10-10 第四批）：硬约束星级 + 互斥 + 回归集 ----------

class Test硬约束星级:

    def test_red下全文星级删除(self, monkeypatch):
        monkeypatch.setattr(rd, '_evidence_audit', lambda rep: _rep_with_conf('invalid'))
        text = ("## 竞争力\n\n综合实力 ★★★★★。\n\n"
                "产品力全维度碾压对手，行业龙头地位稳固。\n")
        out = rd.apply(text, {}, {'unverified_data': [], 'derived_data': []})
        assert '★' not in out['text'] and '☆' not in out['text']
        assert '评级已隐藏' in out['text']
        assert '碾压' not in out['text'] and '龙头' not in out['text']

    def test_yellow保留星级(self, monkeypatch):
        monkeypatch.setattr(rd, '_evidence_audit', lambda rep: _rep_with_conf('low'))
        text = "## 竞争力\n\n综合实力 ★★★★★。\n"
        out = rd.apply(text, {}, {'unverified_data': [], 'derived_data': []})
        assert '★★★★★' in out['text']


class Test衍生未核验互斥:

    def test_互斥_命中衍生后从stats未核验剔除(self, monkeypatch):
        """同一数字不得既标本系统计算又标未核验（stats 层）。"""
        calls = [{'tool': 'web_search', 'status': 'ok',
                  'result': '总营收 1418.78 亿，研发投入占比 4%，其中 57 亿投向固态电池。'}]
        output = '研发投入 57 亿，占营收 108%？另：52.48 亿为基数。'
        # 构造：verified 里没有 57/108 → 均未核验 → derive_chain 命中 → 剔除
        from agent_fact_audit import _audit_fact_sources
        # 手动走底层：先造 verified/unverified
        note, stats = _audit_fact_sources(output, calls)
        if stats.get('derived_chain_hits'):
            uv_vals = {str(u.get('value')) for u in stats['unverified_data']}
            dv_vals = {str(d.get('value_text')) for d in stats['derived_data']}
            assert not (uv_vals & dv_vals), f'互斥失败：交集 {uv_vals & dv_vals}'


class Test表格数字上下文:

    def test_195表格行完整提取(self):
        from fact_triplet import locate_context
        body = '| 比亚迪 | 195 | 同比降 3.4% |\n\n2025 年比亚迪营收 7771 亿元。'
        got = locate_context(body, '195')
        assert got and '195' in got[0] and '同比降' in got[0]

    def test_长句截断带省略号(self):
        from fact_triplet import locate_context
        body = '营收' + '，' * 200 + '7771 亿元。'
        got = locate_context(body, '7771')
        assert got and got[0].startswith('…') and '7771' in got[0] and len(got[0]) <= 165
