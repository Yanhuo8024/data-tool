# -*- coding: utf-8 -*-
"""finance_quant.py 单测：CAPM 与均线回测（合成数据，无需网络）"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import finance_quant as fq


def _make_price_series(n=300, seed=7, drift=0.001):
    np.random.seed(seed)
    rets = np.random.randn(n) * 0.02 + drift
    price = 100 * np.cumprod(1 + rets)
    return price


@pytest.fixture(scope="module")
def kline_csv(tmp_path_factory):
    """合成日线数据：date / close / market"""
    n = 300
    dates = pd.date_range('2024-01-01', periods=n, freq='B')
    close = _make_price_series(n, seed=1)
    market = _make_price_series(n, seed=2)
    df = pd.DataFrame({'date': dates.strftime('%Y-%m-%d'), 'close': close, 'market': market})
    p = tmp_path_factory.mktemp('fin') / 'kline.csv'
    df.to_csv(p, index=False)
    return str(p)


def test_capm_analysis_basic(kline_csv):
    res = fq.capm_analysis(kline_csv, close_col='close', market_col='market', rf_rate=0.02)
    assert isinstance(res['beta'], float)
    assert isinstance(res['alpha_daily'], float)
    assert 'sharpe_ratio' in res and 'expected_return_pct' in res
    assert res['data_points'] > 0


def test_capm_no_market_col_fallback(kline_csv):
    """无市场列时退化为简化假设，beta 应为 0"""
    res = fq.capm_analysis(kline_csv, close_col='close', rf_rate=0.02)
    assert res['beta'] == 0.0


def test_capm_missing_close_raises(tmp_path):
    p = tmp_path / 'bad.csv'
    pd.DataFrame({'x': [1, 2, 3]}).to_csv(p, index=False)
    with pytest.raises(ValueError):
        fq.capm_analysis(str(p))


def test_backtest_ma_crossover(kline_csv):
    """传 benchmark='' 跳过基准网络拉取"""
    res = fq.backtest_ma_crossover(kline_csv, close_col='close', fast=5, slow=20, benchmark='')
    assert 'total_return_pct' in res and 'max_drawdown_pct' in res
    assert res['trade_count'] > 0  # 应有交易信号


def test_backtest_macd(kline_csv):
    res = fq.backtest_macd(kline_csv, close_col='close', benchmark='')
    assert 'total_return_pct' in res and 'win_rate_pct' in res
    assert 'strategy' in res


def test_find_col():
    df = pd.DataFrame({'close': [1], '日期': [2], 'date': [3]})
    assert fq._find_col(df, 'close', '收盘') == 'close'
    assert fq._find_col(df, '日期') == '日期'
    assert fq._find_col(df, 'nonexistent') is None
