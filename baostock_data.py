#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
baostock_data.py — baostock 金融数据源封装

baostock 特点：免费、无限频、无需 token，适合 A 股/指数历史数据与财务数据。

支持接口：
- kline          : 历史 K 线（日/周/月/5/15/30/60 分钟，前复权/后复权/不复权）
- stock_basic    : 证券基本信息（代码/名称/上市日期/退市日期）
- index_stocks   : 成分股列表（沪深300 / 上证50 / 中证500）
- profit         : 盈利能力（ROE/净利率/毛利率/净利润/每股收益）
- balance        : 偿债能力（资产负债率/流动比率/速动比率）
- cashflow       : 现金流量（每股现金流/经营现金流增速）
- growth         : 成长能力（营收/净利/资产/权益同比增速）
- dividend       : 分红送配
- industry       : 行业分类
- trade_dates    : 交易日历
- st            : ST 状态
"""
from datetime import datetime
import pandas as pd

import baostock as bs


def _query_all(rs):
    rows = []
    while rs.next():
        rows.append(rs.get_row_data())
    return pd.DataFrame(rows, columns=rs.fields)


def _login():
    lg = bs.login()
    if lg.error_code != '0':
        raise RuntimeError(f"baostock 登录失败: {lg.error_msg}")
    return lg


def _normalize_code(symbol: str) -> str:
    """把用户输入的代码转为 baostock 格式：600519 -> sh.600519, 000001.SZ -> sz.000001"""
    if not symbol:
        return ''
    s = symbol.strip().upper().replace(' ', '')
    if '.' in s:  # 已经是 sh.600519 / sz.000001 / 000001.SZ / 600519.SH
        if s.startswith('SH.') or s.startswith('SZ.'):
            return s.lower()
        code, market = s.split('.')
        prefix = 'sh' if market == 'SH' else 'sz'
        return f"{prefix}.{code}"
    # 纯数字
    if s.startswith(('6', '9')):  # 沪市：6 开头 A 股、9 开头 B 股
        return f"sh.{s}"
    if s.startswith(('0', '1', '2', '3')):  # 深市
        return f"sz.{s}"
    if s.startswith(('5',)):  # 沪市基金/ETF
        return f"sh.{s}"
    return f"sz.{s}"


def get_kline(symbol: str, period: str = 'daily', start_date: str = '', end_date: str = '',
              adjust: str = 'qfq', **kwargs):
    """历史 K 线

    Args:
        symbol: 股票/指数代码（600519 / sh.600519 / 000001.SZ）
        period: daily / weekly / monthly / 5 / 15 / 30 / 60（分钟）
        adjust: qfq(前复权) / hfq(后复权) / ''(不复权)
    """
    code = _normalize_code(symbol)
    if not code:
        raise ValueError("请提供股票代码")
    _login()
    try:
        freq = 'd' if period in ('daily', 'd') else 'w' if period == 'weekly' else 'm' if period == 'monthly' else str(period)
        # baostock adjustflag: 1=后复权 2=前复权 3=不复权
        adj = '1' if adjust == 'hfq' else '2' if adjust == 'qfq' else '3'
        fields = "date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,pctChg,isST"
        rs = bs.query_history_k_data_plus(code, fields,
                                          start_date=start_date.replace('/', '-'),
                                          end_date=end_date.replace('/', '-'),
                                          frequency=freq, adjustflag=adj)
        if rs is None or rs.error_code != '0':
            raise RuntimeError(f"查询失败: {rs.error_msg if rs else '无返回'}")
        df = _query_all(rs)
        return df
    finally:
        bs.logout()


def get_stock_basic(**kwargs):
    """证券基本信息（type=1 股票 2 指数 3 其他；status=1 上市 0 退市）"""
    _login()
    try:
        rs = bs.query_stock_basic()
        if rs.error_code != '0':
            raise RuntimeError(f"查询失败: {rs.error_msg}")
        return _query_all(rs)
    finally:
        bs.logout()


def get_index_stocks(index: str = '300', **kwargs):
    """成分股列表：300=沪深300 50=上证50 500=中证500"""
    _login()
    try:
        if index == '300':
            rs = bs.query_hs300_stocks()
        elif index == '50':
            rs = bs.query_sz50_stocks()
        elif index == '500':
            rs = bs.query_zz500_stocks()
        else:
            raise ValueError("index 仅支持 300/50/500")
        if rs.error_code != '0':
            raise RuntimeError(f"查询失败: {rs.error_msg}")
        return _query_all(rs)
    finally:
        bs.logout()


def get_financial(report_type: str = 'profit', symbol: str = '', year: int = 0,
                  quarter: int = 4, **kwargs):
    """财务报表数据

    report_type:
        profit   盈利能力: roeAvg/npMargin/gpMargin/netProfit/epsTTM/MBRevenue
        balance  偿债能力: currentRatio/quickRatio/cashRatio/liabilityToAsset/assetToEquity
        cashflow 现金流量: CAToAsset/NCAToAsset/operateCashFlowPerShare
        growth   成长能力: YOYEquity/YOYAsset/YOYNI/YOYEPSBasic/YOYPNI
    """
    code = _normalize_code(symbol)
    if not code:
        raise ValueError("请提供股票代码")
    if not year:
        year = datetime.now().year
    _login()
    try:
        if report_type == 'profit':
            rs = bs.query_profit_data(code=code, year=year, quarter=quarter)
        elif report_type == 'balance':
            rs = bs.query_balance_data(code=code, year=year, quarter=quarter)
        elif report_type == 'cashflow':
            rs = bs.query_cash_flow_data(code=code, year=year, quarter=quarter)
        elif report_type == 'growth':
            rs = bs.query_growth_data(code=code, year=year, quarter=quarter)
        else:
            raise ValueError("report_type 仅支持 profit/balance/cashflow/growth")
        if rs.error_code != '0':
            raise RuntimeError(f"查询失败: {rs.error_msg}")
        return _query_all(rs)
    finally:
        bs.logout()


def get_dividend(symbol: str, year: str = '', **kwargs):
    """分红送配"""
    code = _normalize_code(symbol)
    if not code:
        raise ValueError("请提供股票代码")
    _login()
    try:
        rs = bs.query_dividend_data(code=code, year=year, yearType='report')
        if rs.error_code != '0':
            raise RuntimeError(f"查询失败: {rs.error_msg}")
        return _query_all(rs)
    finally:
        bs.logout()


def get_industry(symbol: str = '', **kwargs):
    """行业分类（不传 symbol 返回全部）"""
    _login()
    try:
        rs = bs.query_stock_industry()
        if rs.error_code != '0':
            raise RuntimeError(f"查询失败: {rs.error_msg}")
        df = _query_all(rs)
        if symbol:
            code = _normalize_code(symbol)
            return df[df['code'] == code]
        return df
    finally:
        bs.logout()


def get_trade_dates(start_date: str = '', end_date: str = '', **kwargs):
    """交易日历"""
    _login()
    try:
        rs = bs.query_trade_dates(start_date=start_date.replace('/', '-'),
                                  end_date=end_date.replace('/', '-'))
        if rs.error_code != '0':
            raise RuntimeError(f"查询失败: {rs.error_msg}")
        return _query_all(rs)
    finally:
        bs.logout()


def get_st_status(symbol: str = '', **kwargs):
    """ST 状态"""
    _login()
    try:
        rs = bs.query_st_stock()
        if rs.error_code != '0':
            raise RuntimeError(f"查询失败: {rs.error_msg}")
        df = _query_all(rs)
        if symbol:
            code = _normalize_code(symbol)
            return df[df['code'] == code]
        return df
    finally:
        bs.logout()


# 接口注册表（供路由分发）
BAOSTOCK_FUNCS = {
    'kline': {'func': get_kline, 'desc': '历史K线', 'params': ['symbol', 'period', 'start_date', 'end_date', 'adjust']},
    'stock_basic': {'func': get_stock_basic, 'desc': '证券基本信息', 'params': []},
    'index_stocks': {'func': get_index_stocks, 'desc': '成分股(300/50/500)', 'params': ['index']},
    'profit': {'func': get_financial, 'desc': '盈利能力(ROE/净利率)', 'params': ['symbol', 'year', 'quarter']},
    'balance': {'func': get_financial, 'desc': '偿债能力', 'params': ['symbol', 'year', 'quarter']},
    'cashflow': {'func': get_financial, 'desc': '现金流量', 'params': ['symbol', 'year', 'quarter']},
    'growth': {'func': get_financial, 'desc': '成长能力', 'params': ['symbol', 'year', 'quarter']},
    'dividend': {'func': get_dividend, 'desc': '分红送配', 'params': ['symbol', 'year']},
    'industry': {'func': get_industry, 'desc': '行业分类', 'params': ['symbol']},
    'trade_dates': {'func': get_trade_dates, 'desc': '交易日历', 'params': ['start_date', 'end_date']},
    'st': {'func': get_st_status, 'desc': 'ST状态', 'params': ['symbol']},
}
