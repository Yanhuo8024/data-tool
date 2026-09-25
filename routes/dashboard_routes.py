#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
data.tool — 仪表盘数据 API
"""

from core_config import exc_response
import time
from datetime import datetime, timedelta
from functools import wraps
from flask import Blueprint, jsonify
from core_config import logger, handle_errors
from auth import optional_auth

dashboard_bp = Blueprint('dashboard', __name__)

_cache = {}
_cache_time = {}
CACHE_TTL = {'index': 60, 'fund_flow': 120, 'exchange': 300, 'movers': 120, 'sentiment': 600, 'tasks': 60}

def _is_error_result(result):
    """判断路由返回值是否为错误响应。

    注意 Flask 的 `return jsonify(...), 500` 里，Response 对象自身的 status_code 仍是 200，
    真正的状态码在元组第二个元素上，所以必须先看元组、再看 Response。
    """
    status = None
    target = result
    if isinstance(result, tuple):
        target = result[0] if result else None
        if len(result) >= 2 and isinstance(result[1], int):
            status = result[1]
    if status is None:
        status = getattr(target, 'status_code', None)
    try:
        return int(status) >= 400
    except (TypeError, ValueError):
        return False


def cached(key, ttl):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            now = time.time()
            if key in _cache and (now - _cache_time.get(key, 0)) < ttl:
                return _cache[key]
            result = f(*args, **kwargs)
            # 只缓存成功响应。此前会把 (Response, 500) 这类错误也缓存整整一个 TTL，
            # 后果：① 前端在 TTL 内持续看到旧错误；② 缓存里的 Response 对象被
            # /api/stream/finance 取走去 json.dumps，抛 TypeError 把 SSE 连接打断。
            if not _is_error_result(result):
                _cache[key] = result
                _cache_time[key] = now
            return result
        return wrapper
    return decorator

INDEX_MAP = [
    ('sh000001', 'sh.000001', '上证指数'),
    ('sz399001', 'sz.399001', '深证成指'),
    ('sz399006', 'sz.399006', '创业板指'),
]

def _fetch_tencent_indices():
    """腾讯实时行情接口（qt.gtimg.cn），返回今天的最新价/涨跌额/涨跌幅"""
    import requests
    codes = ','.join(k for k, _, _ in INDEX_MAP)
    r = requests.get(f'https://qt.gtimg.cn/q={codes}', timeout=8,
                     headers={'User-Agent': 'Mozilla/5.0'})
    r.encoding = 'gbk'
    result = {}
    for line in r.text.strip().split(';'):
        line = line.strip()
        if '="' not in line:
            continue
        var_part, data_part = line.split('=', 1)
        key = var_part.replace('v_', '').strip()
        fields = data_part.strip('"').split('~')
        if len(fields) < 33:
            continue
        name = fields[1]
        try:
            close = float(fields[3])
            prev = float(fields[4])
            ts = fields[30]
        except (ValueError, IndexError):
            continue
        if prev <= 0:
            continue
        change = close - prev
        change_pct = change / prev * 100
        result[key] = {
            "name": name, "close": round(close, 2),
            "change": round(change, 2), "change_pct": round(change_pct, 2),
            "date": f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}" if len(ts) >= 8 else datetime.now().strftime('%Y-%m-%d'),
            "source": "realtime",
        }
    return result


def _fetch_sina_indices():
    """新浪实时行情接口（hq.sinajs.cn）"""
    import requests
    codes = ','.join('s_' + k for k, _, _ in INDEX_MAP)
    r = requests.get(f'https://hq.sinajs.cn/list={codes}', timeout=8,
                     headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://finance.sina.com.cn'})
    r.encoding = 'gbk'
    result = {}
    today = datetime.now().strftime('%Y-%m-%d')
    for line in r.text.strip().split('\n'):
        if '="' not in line:
            continue
        var_part, data_part = line.split('=', 1)
        key = var_part.replace('var hq_str_s_', '').strip()
        fields = data_part.strip('";').split(',')
        if len(fields) < 4:
            continue
        try:
            close = float(fields[1])
            change = float(fields[2])
            change_pct = float(fields[3])
        except (ValueError, IndexError):
            continue
        name = fields[0]
        result[key] = {
            "name": name, "close": round(close, 2),
            "change": round(change, 2), "change_pct": round(change_pct, 2),
            "date": today, "source": "realtime",
        }
    return result


def _fetch_akshare_spot():
    """akshare 东方财富实时行情（盘中可用）"""
    import akshare as ak
    df = ak.stock_zh_index_spot_em()
    if df is None or df.empty:
        return {}
    result = {}
    for _, name in [(k, n) for k, _, n in INDEX_MAP]:
        row = df[df['名称'] == name]
        if row.empty:
            continue
        r = row.iloc[0]
        code = str(r['代码'])
        close = float(r['最新价'])
        prev = float(r['昨收'])
        if prev <= 0:
            continue
        change = close - prev
        change_pct = change / prev * 100
        key = code[:2].lower() + code[2:]
        result[key] = {
            "name": name, "close": round(close, 2),
            "change": round(change, 2), "change_pct": round(change_pct, 2),
            "date": datetime.now().strftime('%Y-%m-%d'), "source": "realtime",
        }
    return result


def _fetch_baostock_daily():
    """baostock 日线（收盘后更新，免费无限频）"""
    import baostock as bs
    import pandas as pd
    lg = bs.login()
    if lg.error_code != '0':
        return {}
    result = {}
    today = datetime.now().strftime('%Y-%m-%d')
    start = (datetime.now() - timedelta(days=10)).strftime('%Y-%m-%d')
    try:
        for key, bscode, name in INDEX_MAP:
            try:
                rs = bs.query_history_k_data_plus(bscode, 'date,close', start_date=start, end_date=today, frequency='d', adjustflag='3')
                if rs.error_code != '0':
                    continue
                data_list = []
                while rs.next():
                    data_list.append(rs.get_row_data())
                if not data_list:
                    continue
                df = pd.DataFrame(data_list, columns=rs.fields)
                df['close'] = pd.to_numeric(df['close'])
                latest = df.iloc[-1]
                prev = df.iloc[-2] if len(df) > 1 else latest
                close = float(latest['close'])
                prev_close = float(prev['close'])
                change = close - prev_close
                change_pct = (change / prev_close * 100) if prev_close else 0
                result[key] = {
                    "name": name, "close": round(close, 2),
                    "change": round(change, 2), "change_pct": round(change_pct, 2),
                    "date": str(latest['date']),
                }
            except Exception as e:
                logger.warning(f"baostock {bscode} error: {e}")
    finally:
        bs.logout()
    return result


def _fetch_akshare_daily():
    """akshare 日线兜底"""
    import akshare as ak
    result = {}
    for key, _, name in INDEX_MAP:
        try:
            df = ak.stock_zh_index_daily(symbol=key)
            if df is not None and not df.empty:
                latest = df.iloc[-1]
                prev = df.iloc[-2] if len(df) > 1 else latest
                close = float(latest['close'])
                prev_close = float(prev['close'])
                change = close - prev_close
                change_pct = (change / prev_close * 100) if prev_close else 0
                result[key] = {
                    "name": name, "close": round(close, 2),
                    "change": round(change, 2), "change_pct": round(change_pct, 2),
                    "date": str(latest['date']), "source": "prev_close",
                }
        except Exception as e:
            logger.warning(f"akshare daily {key} error: {e}")
    return result


# 1. 大盘指数（实时优先，多源降级）
@dashboard_bp.route('/api/dashboard/index-quote', methods=['GET'])
@handle_errors
@optional_auth
@cached('index', 300)
def index_quote():
    try:
        # 尝试1：腾讯实时（qt.gtimg.cn，盘中/盘后实时）
        try:
            result = _fetch_tencent_indices()
            if len(result) >= 3:
                return jsonify(result)
            logger.warning(f"tencent partial: {list(result.keys())}")
        except Exception as e:
            logger.warning(f"tencent error: {e}")

        # 尝试2：新浪实时（hq.sinajs.cn）
        try:
            result = _fetch_sina_indices()
            if len(result) >= 3:
                return jsonify(result)
            logger.warning(f"sina partial: {list(result.keys())}")
        except Exception as e:
            logger.warning(f"sina error: {e}")

        # 尝试3：akshare 东方财富实时
        try:
            result = _fetch_akshare_spot()
            if len(result) >= 3:
                return jsonify(result)
        except Exception as e:
            logger.warning(f"akshare spot error: {e}")

        # 尝试4：baostock 日线（收盘后更新）
        try:
            result = _fetch_baostock_daily()
            if len(result) >= 3:
                return jsonify(result)
        except Exception as e:
            logger.warning(f"baostock error: {e}")

        # 兜底：akshare 日线
        result = _fetch_akshare_daily()
        return jsonify(result)
    except Exception as e:
        logger.error(f"index_quote fatal: {e}")
        return exc_response(e)

# 2. 大盘资金流向（沪深港通近15日净流入折线图）
@dashboard_bp.route('/api/dashboard/fund-flow', methods=['GET'])
@handle_errors
@optional_auth
@cached('fund_flow_daily', 120)
def fund_flow():
    """沪深港通（北向+南向）近15个交易日净流入折线图"""
    import requests
    url = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
    params = {
        'sortColumns': 'TRADE_DATE', 'sortTypes': '-1',
        'pageSize': '100', 'pageNumber': '1',
        'reportName': 'RPT_MUTUAL_DEAL_HISTORY',
        'columns': 'ALL', 'source': 'WEB', 'client': 'WEB',
    }
    headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://data.eastmoney.com/'}
    r = requests.get(url, params=params, headers=headers, timeout=10)
    d = r.json()
    if not d.get('success') or not d.get('result'):
        return jsonify({"error": "数据获取失败", "daily": []}), 500

    data = d['result'].get('data', [])
    # MUTUAL_TYPE: 001=沪股通(北向) 002=港股通沪(南向) 003=深股通(北向) 004=港股通深(南向)
    # NET_DEAL_AMT 单位: 百万元 → 亿
    by_date = {}
    for item in data:
        mt = item.get('MUTUAL_TYPE')
        if mt not in ('001', '002', '003', '004'):
            continue
        date_str = (item.get('TRADE_DATE') or '')[:10]
        amt = item.get('NET_DEAL_AMT')
        if amt is None:
            continue
        try:
            amt = float(amt) / 100.0  # 百万 → 亿
        except (TypeError, ValueError):
            continue
        rec = by_date.setdefault(date_str, {'date': date_str, 'north': None, 'south': None})
        if mt in ('001', '003'):
            rec['north'] = round((rec['north'] or 0) + amt, 2)
        elif mt in ('002', '004'):
            rec['south'] = round((rec['south'] or 0) + amt, 2)

    daily = sorted(by_date.values(), key=lambda x: x['date'])[-15:]
    return jsonify({'daily': daily})
    d = r.json()
    if d.get('rc') != 0 or not d.get('data'):
        return jsonify({"error": "数据获取失败"}), 500

    klines = d['data'].get('klines', [])
    records = []
    for line in klines:
        parts = line.split(',')
        if len(parts) < 12:
            continue
        records.append({
            'date': parts[0],
            'main_net': round(float(parts[1]) / 1e8, 2),       # 主力净流入(亿)
            'small_net': round(float(parts[2]) / 1e8, 2),       # 小单净流入(亿)
            'mid_net': round(float(parts[3]) / 1e8, 2),         # 中单净流入(亿)
            'big_net': round(float(parts[4]) / 1e8, 2),         # 大单净流入(亿)
            'super_net': round(float(parts[5]) / 1e8, 2),       # 超大单净流入(亿)
            'close': float(parts[11]),
            'pct_chg': float(parts[12]),
        })
    return jsonify({'daily': records})

# 3. 汇率
@dashboard_bp.route('/api/dashboard/exchange-rate', methods=['GET'])
@handle_errors
@optional_auth
@cached('exchange', CACHE_TTL['exchange'])
def exchange_rate():
    try:
        import akshare as ak
        df = ak.currency_boc_safe()
        if df is None or df.empty:
            return jsonify({"error": "暂无数据"}), 404
        recent = df.tail(30)
        latest = df.iloc[-1]
        data = {
            "dates": recent['日期'].tolist(),
            "usd": [float(x)/100 for x in recent['美元'].tolist()],
            "eur": [float(x)/100 for x in recent['欧元'].tolist()],
            "latest": {"date": str(latest['日期']), "usd": float(latest['美元'])/100, "eur": float(latest['欧元'])/100},
        }
        if len(df) >= 2:
            prev = df.iloc[-2]
            data["change"] = {"usd": round(float(latest['美元']) - float(prev['美元']), 2), "eur": round(float(latest['欧元']) - float(prev['欧元']), 2)}
        return jsonify(data)
    except Exception as e:
        return exc_response(e)

# 4. 大盘异动
@dashboard_bp.route('/api/dashboard/market-movers', methods=['GET'])
@handle_errors
@optional_auth
@cached('movers', CACHE_TTL['movers'])
def market_movers():
    today = datetime.now().strftime('%Y%m%d')
    zt_list, lhb_list = [], []
    try:
        import akshare as ak
        df_zt = ak.stock_zt_pool_em(date=today)
        if df_zt is not None and not df_zt.empty:
            for _, row in df_zt.head(15).iterrows():
                zt_list.append({"code": str(row['代码']), "name": str(row['名称']), "change": float(row['涨跌幅']), "price": float(row['最新价'])})
    except Exception as e:
        logger.warning(f"zt error: {e}")
    try:
        import akshare as ak
        start = (datetime.now() - timedelta(days=7)).strftime('%Y%m%d')
        df_lhb = ak.stock_lhb_detail_em(start_date=start, end_date=today)
        if df_lhb is not None and not df_lhb.empty:
            for _, row in df_lhb.head(15).iterrows():
                lhb_list.append({"code": str(row['代码']), "name": str(row['名称']), "date": str(row['上榜日']), "change": float(row['涨跌幅']) if '涨跌幅' in row else 0})
    except Exception as e:
        logger.warning(f"lhb error: {e}")
    return jsonify({"zt_pool": zt_list, "lhb": lhb_list})

# 5. 市场情绪
@dashboard_bp.route('/api/dashboard/sentiment', methods=['GET'])
@handle_errors
@optional_auth
@cached('sentiment', CACHE_TTL['sentiment'])
def sentiment():
    import math
    def clean(v):
        """将 NaN/Infinity 转换为 None，保证 JSON 合法"""
        try:
            f = float(v)
            if math.isnan(f) or math.isinf(f):
                return None
            return f
        except (TypeError, ValueError):
            return v
    try:
        import akshare as ak
        result = {}
        try:
            df = ak.bond_zh_us_rate(start_date=(datetime.now() - timedelta(days=30)).strftime('%Y%m%d'))
            if df is not None and not df.empty:
                latest = df.iloc[-1]
                result["bond"] = {"china_10y": clean(latest['中国国债收益率10年']), "us_10y": clean(latest['美国国债收益率10年'])}
        except Exception:
            pass  # 外部数据源不可用时跳过该项，不阻塞整体响应
        try:
            df = ak.macro_china_pmi()
            if df is not None and not df.empty:
                latest = df.iloc[-1]
                result["pmi"] = {"manufacturing": clean(latest['制造业-指数']), "non_manufacturing": clean(latest['非制造业-指数'])}
        except Exception:
            pass
        try:
            df = ak.macro_china_cpi()
            if df is not None and not df.empty:
                latest = df.iloc[-1]
                result["cpi"] = {"yoy": clean(latest['全国-同比增长'])}
        except Exception:
            pass
        return jsonify(result)
    except Exception as e:
        return exc_response(e)

# 6. 任务
@dashboard_bp.route('/api/dashboard/tasks', methods=['GET'])
@handle_errors
@cached('tasks', CACHE_TTL['tasks'])
def tasks():
    return jsonify({"tasks": [
        {"id": 1, "name": "A股行情数据更新", "type": "数据采集", "owner": "系统", "progress": 100, "status": "ok", "time": "2m 15s"},
        {"id": 2, "name": "因子模型训练 v4", "type": "机器学习", "owner": "研究组 A", "progress": 100, "status": "ok", "time": "4m 12s"},
        {"id": 3, "name": "财报抓取 Q2", "type": "金融数据", "owner": "爬虫组", "progress": 64, "status": "run", "time": "1m 48s"},
        {"id": 4, "name": "归因分析", "type": "统计分析", "owner": "量化组", "progress": 38, "status": "run", "time": "32s"},
        {"id": 5, "name": "周报生成", "type": "报告", "owner": "AI 助手", "progress": 100, "status": "err", "time": "9s"},
        {"id": 6, "name": "条件筛选 #8821", "type": "筛选器", "owner": "研究组 B", "progress": 100, "status": "warn", "time": "12s"},
    ]})
