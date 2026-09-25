#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_quality_regression.py - P2b 质量回归模块测试（离线，无需 LLM/联网）
验证：
1. 20 条黄金用例结构合法（必填字段、无空 expected/key_numbers 冲突等）
2. 确定性打分器的判分逻辑（正确用例得高分、跳步用例被扣分、误用工具被扣分）
3. 汇总报告与 CI 阈值断言
"""
import pytest
from quality_regression import (
    GOLDEN_CASES, score_trace, _aggregate,
)

REQUIRED_FIELDS = {"id", "query", "expected_tools", "forbidden_tools",
                   "key_numbers", "no_skip", "must_have_tool"}


def test_golden_cases_count_and_schema():
    assert len(GOLDEN_CASES) == 20, "应维护 20 条黄金用例"
    seen = set()
    for c in GOLDEN_CASES:
        assert c["id"] not in seen, f"用例 ID 重复: {c['id']}"
        seen.add(c["id"])
        missing = REQUIRED_FIELDS - set(c.keys())
        assert not missing, f"{c['id']} 缺少字段 {missing}"
        assert isinstance(c["expected_tools"], list)
        assert isinstance(c["forbidden_tools"], list)
        assert isinstance(c["key_numbers"], list)
        assert isinstance(c["no_skip"], bool)


def test_score_tool_selection_correct():
    """应调用期望工具，避免误调用 forbidden 工具，且得了高分"""
    case = {"id": "t", "query": "q", "expected_tools": ["describe", "clean"],
            "forbidden_tools": ["did"], "key_numbers": [], "no_skip": True,
            "must_have_tool": True}
    calls = [{"tool": "describe", "status": "ok"},
             {"tool": "clean", "status": "ok"}]
    sc = score_trace(case, calls, "这是一份较长的报告内容，包含足够的分析文字描述，足以证明没有跳步。")
    assert sc["tool_accuracy"] >= 90
    assert sc["skip_rate"] <= 20  # 取了 2 个工具 → 不判跳步


def test_score_forbidden_tool_penalized():
    """误用 forbidden 工具要显著扣分"""
    case = {"id": "t", "query": "q", "expected_tools": ["did"],
            "forbidden_tools": ["ml", "scrape"], "key_numbers": [],
            "no_skip": True, "must_have_tool": True}
    calls = [{"tool": "did", "status": "ok"}, {"tool": "ml", "status": "ok"}]
    sc = score_trace(case, calls, "正文足够长，说明没有跳步……" * 5)
    assert sc["forbidden_hit"] == ["ml"]
    assert sc["tool_accuracy"] <= 70
    expect = score_trace(case, [{"tool": "did", "status": "ok"}],
                         "正文足够长，说明没有跳步……" * 5)
    assert expect["tool_accuracy"] > sc["tool_accuracy"]


def test_score_skip_rate_when_no_tool():
    """no_skip=True 但成功工具调用为 0 却写了长篇结论 → 高跳步率"""
    case = {"id": "t", "query": "q", "expected_tools": ["describe"],
            "forbidden_tools": [], "key_numbers": [],
            "no_skip": True, "must_have_tool": True}
    sc = score_trace(case, [], "   " + ("完全没有调用工具就写下很长的结论，这是明显的跳步行为。" * 3))
    assert sc["skip_rate"] >= 50  # 未取数即写结论 → 至少中高跳步率
    assert sc["tool_accuracy"] <= 30  # must_have_tool=True 但没调用期望工具


def test_number_fidelity():
    """key_numbers 缺失 → number_fidelity 下调；全部出现 → 满分"""
    case = {"id": "t", "query": "q", "expected_tools": [],
            "forbidden_tools": [], "key_numbers": ["违约", "15.2%"],
            "no_skip": False, "must_have_tool": False}
    no = score_trace(case, [], "总样本1万行。")   # 缺 "违约" 与 "15.2%"
    with_data = score_trace(case, [], "违约样本占比正好是 15.2%。")
    assert no["number_fidelity"] < with_data["number_fidelity"]
    assert with_data["number_fidelity"] == 100.0


def test_aggregate_threshold():
    """汇总：良好结果应通过 CI 阈值；坏结果应不通过"""
    good = [{"overall": 90}, {"overall": 88}, {"overall": 95}]
    assert _aggregate(good)["thresh_passed"] is True
    bad = [{"overall": 40}, {"overall": 30}, {"overall": 55}]
    assert _aggregate(bad)["thresh_passed"] is False


def test_all_cases_scoreable_offline():
    """所有 20 条用例都应用确定性打分器离线路跑且不抛异常"""
    for c in GOLDEN_CASES:
        sc = score_trace(c, [], "")
        for k in ("tool_accuracy", "number_fidelity", "skip_rate", "overall"):
            assert isinstance(sc.get(k), (int, float)), f"{c['id']} 缺 {k}"