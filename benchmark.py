#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
benchmark.py - 基准数据获取 + 指标计算
"""
import pandas as pd, numpy as np
from typing import Optional, Dict

INDICES = {
    "000300": "沪深300",
    "000905": "中证500",
    "000016": "上证50",
    "399006": "创业板指",
    "000688": "科创50",
}


def get_index_kline(symbol="000300", period="daily", start="", end=""):
    """获取指数K线"""
    try:
        import akshare as ak
        df = ak.index_zh_a_hist(symbol=symbol, period=period,
                                start_date=start.replace("-", ""),
                                end_date=end.replace("-", ""))
        if df is None or df.empty:
            return None
        cm = {"日期": "date", "开盘": "open", "收盘": "close", "最高": "high",
              "最低": "low", "成交量": "volume", "成交额": "amount",
              "涨跌幅": "pct_change", "涨跌额": "change", "换手率": "turnover"}
        df = df.rename(columns={k: v for k, v in cm.items() if k in df.columns})
        df["symbol"] = symbol
        return df
    except Exception as e:
        print(f"[Benchmark] Error: {e}")
        return None


def get_benchmark_cumulative(symbol="000300", start="", end=""):
    """获取指数累计净值"""
    df = get_index_kline(symbol, "daily", start, end)
    if df is None: return None
    return (1 + df["close"].pct_change().fillna(0)).cumprod()


def compute_benchmark_metrics(returns: pd.Series, trading_days=252) -> Dict:
    """计算基准指标: 年化收益/波动率/最大回撤/夏普"""
    if returns.empty: return {}
    cum = (1 + returns.fillna(0)).cumprod()
    n = len(returns)
    ann_ret = (cum.iloc[-1]) ** (trading_days / n) - 1 if n > 0 else 0
    vol = returns.std() * np.sqrt(trading_days)
    dd = ((cum - cum.cummax()) / cum.cummax()).min()
    rf = 0.02 / trading_days
    sharpe = (returns - rf).mean() / returns.std() * np.sqrt(trading_days) if returns.std() > 0 else 0
    return {"annual_return_pct": round(ann_ret * 100, 2), "volatility_pct": round(vol * 100, 2),
            "max_drawdown_pct": round(dd * 100, 2), "sharpe_ratio": round(sharpe, 2)}


def get_benchmark_returns(symbol="000300", start="", end=""):
    """获取基准日收益率"""
    df = get_index_kline(symbol, "daily", start, end)
    if df is None: return None
    return df.set_index("date")["close"].pct_change()


def align_benchmark_to_strategy(strategy_dates, benchmark_returns):
    """将基准对齐到策略交易日"""
    if not isinstance(strategy_dates, pd.DatetimeIndex):
        strategy_dates = pd.to_datetime(strategy_dates)
    benchmark_returns.index = pd.to_datetime(benchmark_returns.index)
    return benchmark_returns.reindex(strategy_dates, method="ffill").fillna(0)
