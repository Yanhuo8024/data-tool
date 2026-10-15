# -*- coding: utf-8 -*-
"""数据口径 / 溯源 / 分部数据回归（2026-10-08）。

用户反馈两件事：
  ① 数据来源只写「financial_ratios / web_search / company_analysis」——那是**内部函数名**，
     不是数据来源，用户既无法核对也无法判断可信度。
  ② 问「收入占比」时系统回「暂未包含」——**答非所问**，应直接查财报分部附注。
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import data_provenance as dp                      # noqa: E402
import segment_data as sd                         # noqa: E402
import segment_tool as st                         # noqa: E402


class Test来源分级:

    @pytest.mark.parametrize('source,tier', [
        ('港交所披露易 年报', 1),
        ('公司 IR 投资者关系', 2),
        ('akshare 东财', 3),
        ('news web_search', 4),
        ('百度百科', 5),
        ('financial_ratios', 3),      # 内部函数名归入数据终端层
    ])
    def test_归类(self, source, tier):
        assert dp.classify(source)[0] == tier, source

    def test_未标注来源归最低级(self):
        assert dp.classify('') == (6, '来源未标注')
        assert dp.classify('莫名其妙的东西')[0] == 6

    def test_优先级顺序即用户要求(self):
        labels = [dp.TIER_LABEL[i] for i in (1, 2, 3, 4, 5)]
        assert labels == ['交易所公告', '公司IR财报', '权威金融终端', '公开新闻', '百科及其他']


class Test溯源戳:
    """每个数字必须带：来源名称、报告期、页码/链接、抓取时间。"""

    def test_四要素齐全(self):
        s = dp.stamp('港交所披露易', metric='营业收入', value=4572.87,
                     period='2025-12-31', link='https://www1.hkexnews.hk/x')
        assert s['source'] == '港交所披露易'
        assert s['period'] == '2025-12-31'
        assert s['link'].startswith('https://')
        assert s['fetched_at'] and len(s['fetched_at']) == 19
        assert s['source_tier'] == 1

    def test_缺省时标未标注而非留空(self):
        s = dp.stamp('akshare', metric='PE')
        assert s['period'] == '未标注' and s['link'] == '未标注'

    def test_页码可替代链接(self):
        assert dp.stamp('年报', page='P.88')['link'] == 'P.88'

    def test_完整度统计(self):
        good = dp.stamp('akshare', metric='A', period='2025', link='x')
        bad = dp.stamp('akshare', metric='B')
        cov = dp.coverage([good, bad])
        assert cov['total'] == 2 and cov['complete'] == 1
        assert cov['missing'][0]['metric'] == 'B'

    def test_权威性判定(self):
        assert dp.is_authoritative([dp.stamp('交易所公告', metric='a')])
        assert not dp.is_authoritative([dp.stamp('百度百科', metric='a')])

    def test_最差层级(self):
        stamps = [dp.stamp('交易所公告', metric='a'), dp.stamp('百度百科', metric='b')]
        assert dp.worst_tier(stamps) == 5

    def test_渲染表格与缺项告警(self):
        out = dp.render([dp.stamp('akshare', metric='营收', value=100.0,
                                  period='2025', link='u'),
                         dp.stamp('news', metric='传闻')])
        assert '| 指标 |' in out and '营收' in out
        assert '溯源完整度 1/2' in out and '传闻' in out

    def test_空输入不产出空标题(self):
        assert dp.render([]) == ''
        assert dp.render(None) == ''


class Test分部数据字段口径:

    def test_字段即用户要求(self):
        assert sd.SEGMENT_FIELDS == ('收入', '同比', '占比', '毛利率', '经营利润')


class Test港股缺口可执行:
    """核心：不能回「暂未包含」，必须给出缺什么/去哪取/期望字段。"""

    def test_识别港股并给出缺口(self):
        d = sd.fetch_segments('01810', 'hk')
        assert d['ok'] is False
        gap = d['gap']
        assert gap['reason'] and gap['source'] and gap['link'] and gap['hint']
        assert gap['fields'] == list(sd.SEGMENT_FIELDS)

    def test_缺口含下一步动作(self):
        gap = sd.fetch_segments('HK01810')['gap']
        assert gap['next_actions'], '缺口必须给出可执行下一步'

    def test_渲染不出暂未包含(self):
        out = sd.render_segments(sd.fetch_segments('01810', 'hk'))
        assert '暂未包含' not in out and '未能自动获取' in out
        for kw in ('原因', '应查来源', '入口', '定位提示', '期望字段'):
            assert kw in out, kw

    def test_港股编码识别(self):
        assert sd._looks_hk('01810') and sd._looks_hk('HK01810')
        assert not sd._looks_hk('600519')


class TestA股真实取数解析:

    @pytest.fixture
    def segments_api(self, monkeypatch):  # 提供假 akshare 模块（非桩函数，故不用 fake_ 前缀）
        df = pd.DataFrame([
            # 本期
            {'分类类型': '按产品分类', '报告日期': '2025-12-31', '主营构成': '甲产品',
             '主营收入': 800.0, '收入比例': 0.8, '主营利润': 400.0, '毛利率': 0.5},
            {'分类类型': '按产品分类', '报告日期': '2025-12-31', '主营构成': '乙产品',
             '主营收入': 200.0, '收入比例': 0.2, '主营利润': 60.0, '毛利率': 0.3},
            # 去年同期 → 用于算同比
            {'分类类型': '按产品分类', '报告日期': '2024-12-31', '主营构成': '甲产品',
             '主营收入': 640.0, '收入比例': 0.8, '主营利润': 300.0, '毛利率': 0.47},
            {'分类类型': '按产品分类', '报告日期': '2024-12-31', '主营构成': '乙产品',
             '主营收入': 200.0, '收入比例': 0.2, '主营利润': 50.0, '毛利率': 0.25},
            {'分类类型': '按地区分类', '报告日期': '2025-12-31', '主营构成': '境内',
             '主营收入': 1000.0, '收入比例': 1.0, '主营利润': 460.0, '毛利率': 0.46},
        ])
        import types
        fake = types.SimpleNamespace(stock_zygc_em=lambda symbol=None, **kw: df)
        monkeypatch.setitem(sys.modules, 'akshare', fake)

        real_import = __import__

        def _imp(name, *a, **k):
            if name == 'akshare':
                return fake
            return real_import(name, *a, **k)
        monkeypatch.setattr('builtins.__import__', _imp)
        return fake

    def test_默认取产品分类并按收入排序(self, segments_api):
        d = sd.fetch_segments('600519', 'a')
        assert d['ok'] is True
        assert d['category'] == '按产品分类'
        assert [s['name'] for s in d['segments']] == ['甲产品', '乙产品']

    def test_同比按去年同期同分部计算(self, segments_api):
        d = sd.fetch_segments('600519', 'a')
        jia = d['segments'][0]
        assert jia['yoy'] == pytest.approx(25.0)      # 800/640-1
        assert jia['share'] == pytest.approx(80.0)

    def test_去年同期缺失时标未披露(self, segments_api):
        d = sd.fetch_segments('600519', 'a', category='按地区分类')
        assert d['segments'][0]['yoy'] is None

    def test_带来源戳与报告期(self, segments_api):
        d = sd.fetch_segments('600519', 'a')
        p = d['provenance'][0]
        assert d['period'] == '2025-12-31'
        assert p['period'] == '2025-12-31'
        assert p['source_tier'] == 3          # 东财 = 权威金融终端
        assert p['fetched_at']

    def test_显式说明经营利润未披露(self, segments_api):
        d = sd.fetch_segments('600519', 'a')
        assert '经营利润' in d['note']

    def test_渲染含五个口径(self, segments_api):
        out = sd.render_segments(sd.fetch_segments('600519', 'a'))
        assert '| 分部 | 收入 | 同比 | 占比 | 毛利率 |' in out
        assert '报告期：2025-12-31' in out and '亿元' in out

    def test_去年同期换算(self):
        assert sd._shift_year('2026-06-30') == '2025-06-30'
        assert sd._shift_year('bad') == ''


class Test工具接口:

    def test_缺代码时报错并列出字段(self):
        out = st.tool_segment_breakdown()
        assert out.startswith('Error') and '600519' in out

    def test_港股返回缺口且提示不要搪塞(self):
        out = st.tool_segment_breakdown('01810', 'hk')
        assert '分部数据缺口' in out
        assert '暂未包含' in out, '应显式指示模型不要回答「暂未包含」'

    def test_注册到注册表(self):
        class _R:
            def __init__(self):
                self.calls = []

            def register(self, *a):
                self.calls.append(a)
        r = _R()
        st.register_segment_tools(r)
        assert r.calls and r.calls[0][0] == 'segment_breakdown'

    def test_模块不反向依赖工具注册表(self):
        """避免 agent_tool_registry ⇄ segment_tool 循环 import。"""
        import re
        src = Path(st.__file__).read_text(encoding='utf-8')
        # 只看真正的 import 语句，别把 docstring 里说明"不 import"的文字也匹配上
        stmts = re.findall(r'^\s*(?:from|import)\s+\S+', src, re.M)
        assert not [s for s in stmts if 'agent_tool_registry' in s], stmts
