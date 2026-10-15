# -*- coding: utf-8 -*-
"""对话策略回归（2026-10-08 用户第 4 项要求）。

用户反馈的三个对话问题：
  ① 问「汽车盈利能力」，agent 却大段讲 DCF；问「收入占比」，又绕回估值 → **答非所问**。
  ② 缺失数据用「暂未包含」搪塞，不给获取路径。
  ③ 每轮重复讲估值，不记上下文。

对应改法：提示词里写死四条纪律（先答后补 / 意图分开 / 缺失明说 / 不重复 + 估值护栏），
并把分部数据接成可调用工具，让「收入占比」有真实数据可答。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import prompts                                     # noqa: E402
import segment_tool as st                          # noqa: E402


@pytest.fixture(scope='module')
def agent_prompt():
    return prompts.get_prompt('agent_system')


class Test提示词纪律:

    def test_含先答后补(self, agent_prompt):
        assert '先答后补' in agent_prompt
        assert '当前问的那个问题' in agent_prompt

    def test_点名用户举的两个反例(self, agent_prompt):
        """用户原话：问"汽车盈利能力"却讲 DCF；问"收入占比"却绕回估值。"""
        assert '汽车盈利' in agent_prompt
        assert '收入占比' in agent_prompt

    def test_意图分开(self, agent_prompt):
        for kw in ('数据查询', '估值方法', '业务分析', '投资建议'):
            assert kw in agent_prompt, kw

    def test_缺失数据必须明说且禁止搪塞(self, agent_prompt):
        assert '严禁' in agent_prompt and '暂未包含' in agent_prompt
        assert '缺什么' in agent_prompt and '去哪里取' in agent_prompt

    def test_上下文不重复(self, agent_prompt):
        assert '不重复' in agent_prompt

    def test_估值护栏写进提示词(self, agent_prompt):
        assert '假设极端' in agent_prompt and '模型异常' in agent_prompt
        assert '敏感性区间' in agent_prompt

    def test_投资建议不给买卖指令(self, agent_prompt):
        assert '不给买卖指令' in agent_prompt


class Test工具描述引导:

    def test_描述明确要求回答收入占比(self):
        d = st.DESCRIPTION
        assert '收入占比' in d and '收入结构' in d

    def test_描述禁止回答not_included(self):
        assert 'not included' in st.DESCRIPTION

    def test_描述覆盖分部场景(self):
        for kw in ('segment', 'revenue share', 'business line'):
            assert kw in st.DESCRIPTION, kw


class Test草稿收入结构接真实分部:

    def _ctx(self, code):
        return {
            'code': code, 'name': '测试公司',
            'profile': {'industry': '白酒'},
            'market': {'pe': 20, 'debt_ratio': 30},
            'financial_analysis': {'ratios': {
                'profitability': {'gross_margin': 60, 'net_margin': 30, 'roe': 25},
                'growth': {'revenue_growth': 10, 'profit_growth': 12},
                'solvency': {}, 'operational': {}}},
            'statements': {}, 'competitors': [], 'news': [], 'swot': {},
        }

    def test_港股给出可执行缺口而非需人工补充(self):
        import company_deep_draft as cdd
        out = cdd._rule_draft(self._ctx('HK01810'))
        mix = out['model']['revMix']
        assert '需人工补充' not in mix
        assert '未能自动获取' in mix
        assert '应查' in mix and '需补字段' in mix

    def test_港股缺口指出年报分部附注(self):
        import company_deep_draft as cdd
        mix = cdd._rule_draft(self._ctx('HK01810'))['model']['revMix']
        assert '分部' in mix

    def test_A股接真实分部数据(self, monkeypatch):
        """A 股有公开接口 → revMix 应直接给分部占比，而不是让用户自己去查。"""
        import company_deep_draft as cdd
        import segment_data as sd
        monkeypatch.setattr(sd, 'fetch_segments', lambda *a, **k: {
            'ok': True, 'period': '2025-12-31', 'gap': None,
            'segments': [{'name': '甲产品', 'share': 80.0},
                         {'name': '乙产品', 'share': 20.0}]})
        mix = cdd._rule_draft(self._ctx('600519'))['model']['revMix']
        assert '甲产品 80.0%' in mix
        assert '2025-12-31' in mix

    def test_取数异常时不崩且留兜底(self, monkeypatch):
        import company_deep_draft as cdd
        import segment_data as sd

        def _boom(*a, **k):
            raise RuntimeError('接口炸了')

        monkeypatch.setattr(sd, 'fetch_segments', _boom)
        mix = cdd._rule_draft(self._ctx('600519'))['model']['revMix']
        assert isinstance(mix, str) and mix
