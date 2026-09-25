# -*- coding: utf-8 -*-
"""LLM 失败兜底报告（_file_data_fallback_report）必须真正可用。

背景（2026-09-20）：该函数从 `ai_analysis_engine` 导入的类名写成 `AiAnalysisEngine`，
而真实类名是 `EnhancedAnalysisEngine`（ai_analysis_engine.py:25）。ImportError 被函数内
`except Exception` 静默吞掉 → 该函数**恒返回 None**，即"深度分析不再产出空报告"的兜底
是一段死代码。本测试锁死类名正确性，防止回归。
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

import ai_assistant as aa


def test_enhanced_analysis_engine_symbol_is_correct():
    """真实类必须叫 EnhancedAnalysisEngine，且 AiAnalysisEngine 不存在。"""
    import ai_analysis_engine as m
    assert hasattr(m, 'EnhancedAnalysisEngine'), "真实类名应为 EnhancedAnalysisEngine"
    assert not hasattr(m, 'AiAnalysisEngine'), "不得凭空存在 AiAnalysisEngine（历史笔误）"


def test_fallback_report_returns_report_on_valid_df(monkeypatch, tmp_path):
    """给定可读 DataFrame，兜底报告必须返回非 None 且 reply 非空（source=agent_fallback）。"""
    import core_config

    df = pd.DataFrame({
        'year': [2018, 2019, 2020, 2021, 2022, 2023],
        'province': ['A', 'B', 'C', 'A', 'B', 'C'],
        'treat': [0, 0, 0, 1, 1, 1],
        'consume': [10.2, 11.5, 12.1, 20.4, 21.9, 23.0],
        'income': [30.0, 31.5, 33.0, 45.0, 47.0, 49.0],
    })

    monkeypatch.setattr(core_config, 'load_dataframe', lambda *a, **k: df)
    monkeypatch.setattr(core_config, 'OUT_DIR', Path(tmp_path))

    rep = aa._file_data_fallback_report('分析这个面板数据的核心特征与风险', 'fake.csv')
    assert rep is not None, "兜底报告不应返回 None（ImportError 死代码已复发？）"
    assert rep.get('ok') is True
    assert rep.get('source') == 'agent_fallback'
    reply = rep.get('reply') or ''
    assert len(reply) > 80, f"兜底报告正文过短：{len(reply)}"
    assert '数据概览' in reply


def test_fallback_report_none_when_no_file():
    """无文件时按契约返回 None（不触发引擎）。"""
    assert aa._file_data_fallback_report('分析', '') is None


def test_fallback_report_none_on_empty_df(monkeypatch, tmp_path):
    """空 DataFrame 时按契约返回 None。"""
    import core_config
    monkeypatch.setattr(core_config, 'load_dataframe', lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(core_config, 'OUT_DIR', Path(tmp_path))
    assert aa._file_data_fallback_report('分析', 'empty.csv') is None
