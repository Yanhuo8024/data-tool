# -*- coding: utf-8 -*-
"""benchmark.py 单测：年化收益/波动/最大回撤/夏普（合成序列，无需网络）
重点守护：最大回撤公式（此前出现过 cummin/cummax 错误导致 -222% 失真）"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchmark import compute_benchmark_metrics, align_benchmark_to_strategy


def test_empty_returns():
    assert compute_benchmark_metrics(pd.Series(dtype=float)) == {}


def test_zero_returns():
    r = pd.Series([0.0] * 100)
    m = compute_benchmark_metrics(r)
    assert m["annual_return_pct"] == 0.0
    assert m["volatility_pct"] == 0.0
    assert m["max_drawdown_pct"] == 0.0
    assert m["sharpe_ratio"] == 0.0


def test_annual_return_known():
    # 252 天每天 +0.1% → 累计 1.001^252-1，年化 = 累计收益（trading_days/n=1）≈ 28.64%
    r = pd.Series([0.001] * 252)
    m = compute_benchmark_metrics(r)
    assert abs(m["annual_return_pct"] - 28.64) < 0.05
    # 252 天每天 -0.1% → 年化 ≈ -22.29%
    m2 = compute_benchmark_metrics(pd.Series([-0.001] * 252))
    assert abs(m2["annual_return_pct"] - (-22.29)) < 0.05


def test_max_drawdown_known():
    # 净值 [1, 1.1, 1.05, 1.0]：从峰值 1.1 回落到 1.0 → 回撤 (1.0-1.1)/1.1 = -9.09%
    prices = pd.Series([1.0, 1.1, 1.05, 1.0])
    returns = prices.pct_change().dropna()
    m = compute_benchmark_metrics(returns)
    assert abs(m["max_drawdown_pct"] - (-9.09)) < 0.02


def test_max_drawdown_cummax_not_cummin():
    """守护：回撤分母必须是累计最高点(cummax)而非累计最低点(cummin)
    上涨后回撤场景，若用 cummin 会得出严重失真的回撤（如 -222%）"""
    np.random.seed(3)
    prices = pd.Series(100 * np.cumprod(1 + np.random.randn(600) * 0.01 + 0.001))
    returns = prices.pct_change().dropna()
    m = compute_benchmark_metrics(returns)
    dd = m["max_drawdown_pct"]
    # 回撤必须 ∈ (-100%, 0%]；失真 bug 会给出 < -100% 的值
    assert -100.0 < dd <= 0.0


def test_volatility_known():
    # 交替 ±1%：std = 0.01，年化波动 = 0.01*sqrt(252)
    r = pd.Series([0.01, -0.01] * 200)
    m = compute_benchmark_metrics(r)
    expect = 0.01 * np.sqrt(252) * 100
    assert abs(m["volatility_pct"] - expect) < 0.5


def test_sharpe_positive_for_positive_drift():
    np.random.seed(11)
    r = pd.Series(np.random.randn(300) * 0.01 + 0.0008)
    m = compute_benchmark_metrics(r)
    assert m["sharpe_ratio"] > 0


def test_align_benchmark_to_strategy():
    dates = pd.date_range('2024-01-01', periods=10, freq='B')
    bret = pd.Series(np.arange(10) * 0.01, index=dates)
    # 对齐到缺失部分日期 → ffill 填充
    aligned = align_benchmark_to_strategy(dates[:8], bret)
    assert len(aligned) == 8
    assert aligned.iloc[0] == bret.iloc[0]
