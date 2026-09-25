#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
finance_data.py - 金融数据获取（简化版）
统一接口获取A股/指数/ETF K线数据，支持复权
"""
import pandas as pd
import numpy as np
from typing import Optional

def get_kline(symbol, period="daily", start="", end="", adjust="qfq"):
    """获取A股K线数据（含复权）"""
    try:
        import akshare as ak
        df = ak.stock_zh_a_hist(symbol=symbol, period=period,
                                start_date=start.replace("-", ""),
                                end_date=end.replace("-", ""),
                                adjust=adjust)
        if df is None or df.empty: return None
        cm = {"日期": "date", "股票代码": "symbol", "开盘": "open",
              "收盘": "close", "最高": "high", "最低": "low",
              "成交量": "volume", "成交额": "amount", "振幅": "amplitude",
              "涨跌幅": "pct_change", "涨跌额": "change", "换手率": "turnover"}
        return df.rename(columns={k: v for k, v in cm.items() if k in df.columns})
    except Exception as e:
        print(f"[FinanceData] Error: {e}")
        return None


def get_index_kline(symbol="000300", period="daily", start="", end=""):
    """获取指数K线"""
    try:
        import akshare as ak
        df = ak.index_zh_a_hist(symbol=symbol, period=period,
                                start_date=start.replace("-", ""),
                                end_date=end.replace("-", ""))
        if df is None or df.empty: return None
        cm = {"日期": "date", "开盘": "open", "收盘": "close", "最高": "high",
              "最低": "low", "成交量": "volume", "成交额": "amount",
              "涨跌幅": "pct_change", "涨跌额": "change", "换手率": "turnover"}
        return df.rename(columns={k: v for k, v in cm.items() if k in df.columns})
    except Exception as e:
        print(f"[FinanceData] Error: {e}")
        return None


def get_stock_list():
    """获取A股列表"""
    try:
        import akshare as ak
        df = ak.stock_zh_a_spot_em()
        cm = {"代码": "code", "名称": "name", "最新价": "price", "涨跌幅": "pct_chg",
              "成交量": "volume", "成交额": "amount", "市盈率-动态": "pe", "市净率": "pb",
              "总市值": "market_cap", "流通市值": "float_cap"}
        return df.rename(columns={k: v for k, v in cm.items() if k in df.columns})
    except Exception as e:
        print(f"[FinanceData] Error: {e}")
        return None


def normalize_hk_code(code):
    """港股代码统一为 5 位数字：HK0700 / hk0700 / HK 9988 / 700 → 00700 / 09988 / 00700
    返回 None 表示非有效港股代码"""
    s = str(code or "").strip().upper().replace(" ", "")
    if s.startswith("HK"):
        s = s[2:]
    if s.isdigit() and 1 <= len(s) <= 5:
        return s.zfill(5)
    return None
