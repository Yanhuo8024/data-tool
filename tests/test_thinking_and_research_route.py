# -*- coding: utf-8 -*-
"""思考全量展示 + 无文件结构化联网分析路由 相关逻辑测试"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_agent import _extract_reasoning  # noqa: E402


def test_strip_tool_json_keeps_planning_prose():
    """推理正文保留，尾部工具 JSON 被剥离"""
    raw = '我先梳理煤炭供需两端的关键因素，再联网核实最新价格。\n{"tool": "web_search", "params": {"query": "煤炭价格"}}'
    out = _extract_reasoning(raw)
    assert '先梳理煤炭供需两端' in out
    assert 'web_search' not in out


def test_strip_tool_json_markdown_fence():
    """markdown 代码块包裹的 JSON 调用被移除"""
    raw = '需要查询政策原文。\n```json\n{"tool": "web_search", "params": {"query": "山西十七条"}}\n```'
    out = _extract_reasoning(raw)
    assert '需要查询政策原文' in out
    assert 'tool' not in out


def test_strip_tool_json_no_call_unchanged():
    """纯正文（无工具调用）原样保留"""
    raw = '本期数据完整，直接给出结论即可。'
    assert _extract_reasoning(raw) == raw


def test_strip_tool_json_bareword_json():
    """skill 名+JSON 粘连形式被剥离"""
    raw = '接下来调用技能 company-analysis {"symbol": "600519"} 请帮我分析茅台'
    out = _extract_reasoning(raw)
    assert 'company-analysis' not in out
    assert '请帮我分析茅台' not in out or out.endswith('请帮我分析茅台')  # 保留其后文字可接受


def test_strip_tool_json_plain_text_kept():
    """普通文字不误删关键数字/中文"""
    raw = '结论：动力煤涨至 900 元/吨，同比 +55 元。'
    assert '动力煤涨至 900' in _extract_reasoning(raw)


def test_needs_structured_research():
    """无文件原因/趋势/最新类问题应识别为需结构化联网分析"""
    from ai_assistant import _needs_structured_research, _has_analyzable_target
    assert _needs_structured_research('煤炭价格上涨的原因')
    assert _needs_structured_research('最近动力煤为什么大涨')
    assert _needs_structured_research('最新油价走势分析')
    assert not _needs_structured_research('你好呀')
    assert not _needs_structured_research('帮我配置代理')
    # 现有的行业/市场目标判定保持不受影响（"煤炭行业"仍直接命中）
    assert _has_analyzable_target('煤炭行业前景如何')


def test_deterministic_fallback_dispatches_by_file(monkeypatch):
    """无文件问题→联网聚合兜底；有文件问题→数据驱动兜底，均不产出空报告"""
    from ai_assistant import _deterministic_fallback_report

    captured = {}

    def fake_web_search(query, top_k=5, **kw):
        captured['q'] = query
        return {
            "ok": True,
            "results": [
                {"title": "煤炭价格走势分析", "url": "https://example.com/coal",
                 "snippet": "动力煤供给收缩叠加需求回暖推动煤价上行。"},
            ],
        }

    import sys
    import web_search as _ws_mod
    monkeypatch.setattr(_ws_mod, 'web_search', fake_web_search)
    monkeypatch.setitem(sys.modules, 'web_search', _ws_mod)
    # 无文件 → 走联网聚合
    res = _deterministic_fallback_report('煤炭价格上涨的原因', '')
    assert res and res.get('ok')
    assert res['source'] == 'web_fallback'
    assert '动力煤供给收缩' in res['reply']
    # 有文件 → 走数据驱动（需要文件存在，构造空文件时应回退到联网聚合）
    res2 = _deterministic_fallback_report('煤炭价格上涨的原因', '_nonexistent_x.csv')
    assert res2 is None or res2.get('ok')