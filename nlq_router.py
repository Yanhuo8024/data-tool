#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nlq_router.py - 自然语言查数路由器

接收 LLM 返回的 JSON 指令，通过字典映射方式安全调用本地函数。
白名单控制可调用函数，不使用 eval/exec，防止代码注入。

日志查询：
    SELECT * FROM access_log WHERE method = 'GET' AND path LIKE '/api/finance%' AND status >= 400 ORDER BY timestamp DESC LIMIT 50
    → SELECT {"table": "access_log", "columns": ["*"], "where": [{"field": "method", "op": "=", "value": "GET"}, {"field": "path", "op": "like", "value": "/api/finance%"}, {"field": "status", "op": ">=", "value": 400}], "order_by": [{"field": "timestamp", "direction": "desc"}], "limit": 50}

股票筛选：
    {"action": "screen_stocks", "filters": {"pe_min": 0, "pe_max": 30, "roe_min": 10, "mcap_min": 100}}

数据获取：
    {"action": "fetch_stock_data", "symbol": "000001", "period": "daily", "start": "2024-01-01", "end": "2024-12-31"}

金融建模：
    {"action": "run_backtest", "strategy": "ma", "file": "xxx.csv", "fast": 5, "slow": 20}

报告生成：
    {"action": "generate_report", "file": "xxx.csv", "template": "data_exploration"}

NLU 意图识别：
    {"action": "classify_intent", "query": "筛选市盈率小于30且ROE大于10%的股票"}

说明：
    1. action 必须是白名单内的函数名，否则直接返回 error
    2. 参数缺失或类型不匹配时，返回明确的错误信息
    3. 所有异常被捕获，不会抛给 Flask
"""

import traceback
import math
from pathlib import Path
from typing import Dict, Any, List, Optional
from functools import wraps

# ==================== 日志查询引擎 ====================

LOG_DIR = Path("output/logs")

def execute_log_query(table: str, columns: List[str] = None,
                      where: List[Dict] = None,
                      order_by: List[Dict] = None,
                      limit: int = 50) -> Dict[str, Any]:
    """
    执行日志文件查询。
    支持表：access_log, error_log, memory_log, system_log
    返回 {"ok": True, "data": [...], "total": n}
    """
    # 白名单 + 路径安全
    allowed_tables = {
        "access_log": LOG_DIR / "access.log",
        "error_log": LOG_DIR / "errors.log",
        "memory_log": LOG_DIR / "memory.log",
        "system_log": LOG_DIR / "system.log",
    }

    if table not in allowed_tables:
        return {"ok": False, "error": f"Unknown table: {table}. Available: {list(allowed_tables.keys())}"}

    log_file = allowed_tables[table]
    if not log_file.exists():
        return {"ok": True, "data": [], "total": 0, "note": "Log file not found"}

    try:
        # 读取日志
        with open(log_file, 'r', encoding='utf-8') as f:
            lines = [line.strip() for line in f.readlines() if line.strip()]

        # 反转（最新的在前面）
        lines.reverse()

        # 解析每一行为简单对象
        results = []
        for line in lines:
            parsed = _parse_log_line(line, table)
            results.append(parsed)

        # 应用 WHERE 过滤
        if where:
            for condition in where:
                field = condition.get("field", "")
                op = condition.get("op", "=")
                value = condition.get("value", "")
                results = [r for r in results if _apply_condition(r, field, op, value)]

        # 排序
        if order_by:
            for order in order_by:
                field = order.get("field", "timestamp")
                direction = order.get("direction", "desc")
                results.sort(key=lambda x: x.get(field, ""), reverse=(direction == "desc"))

        # 限制数量
        total = len(results)
        results = results[:limit]

        # 列过滤
        if columns and columns != ["*"]:
            results = _filter_columns(results, columns)

        return {"ok": True, "data": results, "total": total}

    except Exception as e:
        return {"ok": False, "error": str(e)}


def _parse_log_line(line: str, table: str) -> Dict:
    """解析日志行为结构化对象"""
    result = {"raw": line}

    import re

    # 提取时间戳
    ts_match = re.match(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]', line)
    if ts_match:
        result["timestamp"] = ts_match.group(1)

    # 根据表类型提取字段
    if table == "access_log":
        # 格式: [timestamp] /path METHOD status duration
        parts = line.split()
        if len(parts) >= 4:
            result["path"] = parts[1] if parts[1].startswith("/") else ""
            result["method"] = parts[2] if parts[1].startswith("/") else parts[1]
            result["status"] = int(parts[3]) if parts[3].isdigit() else 0
            # duration 可能带 ms
            dur = parts[4] if len(parts) > 4 else "0ms"
            result["duration"] = float(dur.replace("ms", ""))

    elif table == "error_log":
        # 格式: [timestamp] [source] TYPE: message
        src_match = re.search(r'\[(\w+)\] (\w+):', line)
        if src_match:
            result["source"] = src_match.group(1)
            result["type"] = src_match.group(2)
        # 提取消息部分
        msg_match = re.search(r'\] (\w+): (.+)', line)
        if msg_match:
            result["message"] = msg_match.group(2)

    elif table == "memory_log":
        # 格式: [timestamp] RSS=xxxMB VMS=xxxMB x.x%
        rss_match = re.search(r'RSS=([\d.]+)MB', line)
        vms_match = re.search(r'VMS=([\d.]+)MB', line)
        pct_match = re.search(r'([\d.]+)%', line)
        if rss_match:
            result["rss_mb"] = float(rss_match.group(1))
        if vms_match:
            result["vms_mb"] = float(vms_match.group(1))
        if pct_match:
            result["percent"] = float(pct_match.group(1))

    elif table == "system_log":
        # 格式: [timestamp] message
        msg_match = re.search(r'\] (.+)$', line)
        if msg_match:
            result["message"] = msg_match.group(1)

    return result


def _apply_condition(record: Dict, field: str, op: str, value: Any) -> bool:
    """应用 WHERE 条件"""
    actual = record.get(field)
    if actual is None:
        return False

    # 数值比较
    try:
        if isinstance(value, (int, float)):
            actual = float(actual)
    except (ValueError, TypeError):
        pass

    if op == "=": return str(actual) == str(value)
    if op == "!=": return str(actual) != str(value)
    if op == ">": return actual > value
    if op == ">=": return actual >= value
    if op == "<": return actual < value
    if op == "<=": return actual <= value
    if op == "like": return str(value).lower() in str(actual).lower()
    if op == "in": return str(actual) in [str(v) for v in value]

    return False


def _filter_columns(records: List[Dict], columns: List[str]) -> List[Dict]:
    """过滤指定列"""
    return [{k: v for k, v in r.items() if k in columns or k == "raw"} for r in records]


# ==================== 错误处理装饰器 ====================

def safe_call(fn):
    """装饰器：捕获所有异常，返回标准格式"""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            result = fn(*args, **kwargs)
            if isinstance(result, dict):
                if "ok" not in result:
                    result = {"ok": True, "data": result}
                return result
            return {"ok": True, "data": result}
        except TypeError as e:
            return {"ok": False, "error": "参数类型错误", "detail": str(e)}
        except ValueError as e:
            return {"ok": False, "error": "参数值不合法", "detail": str(e)}
        except KeyError as e:
            return {"ok": False, "error": f"缺少必要参数: {str(e)}"}
        except FileNotFoundError as e:
            return {"ok": False, "error": "数据文件不存在", "detail": str(e)}
        except Exception as e:
            return {"ok": False, "error": "执行异常", "detail": str(e), "trace": traceback.format_exc(limit=5)}
    return wrapper


# ==================== 内置处理器 ====================

@safe_call
def handle_screen_stocks(filters: Dict = None) -> Dict:
    """股票筛选"""
    from stock_screener import fetch_all_stocks, screen_stocks

    df = fetch_all_stocks()
    if df is None:
        return {"ok": False, "error": "无法获取股票数据，请稍后重试"}

    result = screen_stocks(df, filters or {})
    return {
        "ok": True,
        "data": result.head(50).to_dict(orient="records"),
        "total": len(result),
        "note": f"共筛选出 {len(result)} 只股票，显示前50条"
    }


@safe_call
def handle_fetch_stock_data(symbol: str, period: str = "daily",
                             start: str = "", end: str = "", adjust: str = "qfq") -> Dict:
    """获取股票K线数据"""
    from finance_data import get_kline

    if not symbol:
        return {"ok": False, "error": "股票代码不能为空（symbol 参数缺失）"}

    result = get_kline(symbol=symbol, period=period, start=start, end=end, adjust=adjust)
    if result is None:
        return {"ok": False, "error": f"获取 {symbol} 数据失败，请检查股票代码或网络"}

    return {
        "ok": True,
        "symbol": symbol,
        "period": period,
        "rows": len(result),
        "preview": result.head(20).to_dict(orient="records"),
        "columns": list(result.columns)
    }


@safe_call
def handle_run_backtest(strategy: str, file: str, fast: int = 5, slow: int = 20,
                         benchmark: str = "000300", brinson: bool = False) -> Dict:
    """运行回测（含可选Brinson归因）"""
    from finance_quant import backtest_ma_crossover, backtest_macd
    from benchmark import get_benchmark_returns
    import pandas as pd

    file_path = Path("output") / file
    if not file_path.exists():
        return {"ok": False, "error": f"数据文件 {file} 不存在"}

    df = pd.read_csv(file_path)
    if strategy == "macd":
        result = backtest_macd(df)
    else:
        result = backtest_ma_crossover(df, fast=fast, slow=slow)
    return result


@safe_call
def handle_generate_report(file: str, template: str = "data_exploration") -> Dict:
    """生成报告"""
    from report_generator import save_report

    file_path = Path("output") / file
    if not file_path.exists():
        return {"ok": False, "error": f"数据文件 {file} 不存在"}

    path, meta = save_report(file_path, template=template)
    if path:
        return {"ok": True, "file": Path(path).name, "template": template}
    return {"ok": False, "error": meta.get("error", "报告生成失败")}


@safe_call
def handle_classify_intent(query: str) -> Dict:
    """意图识别（规则版NLU）"""
    import re

    query = query.strip().lower()

    # 登录日志分析
    if any(kw in query for kw in ["登录", "access", "访问", "请求", "log"]):
        return {
            "ok": True,
            "action": "execute_log_query",
            "query": {
                "table": "access_log",
                "columns": ["timestamp", "path", "method", "status"],
                "order_by": [{"field": "timestamp", "direction": "desc"}],
                "limit": 50
            },
            "intent": "查询请求日志"
        }

    # 错误日志
    if any(kw in query for kw in ["错误", "error", "异常", "exception"]):
        return {
            "ok": True,
            "action": "execute_log_query",
            "query": {
                "table": "error_log",
                "columns": ["timestamp", "source", "type", "message"],
                "order_by": [{"field": "timestamp", "direction": "desc"}],
                "limit": 50
            },
            "intent": "查询错误日志"
        }

    # 内存监控
    if any(kw in query for kw in ["内存", "memory", "资源", "泄漏"]):
        return {
            "ok": True,
            "action": "execute_log_query",
            "query": {
                "table": "memory_log",
                "columns": ["timestamp", "rss_mb", "percent"],
                "order_by": [{"field": "timestamp", "direction": "desc"}],
                "limit": 50
            },
            "intent": "查询内存使用日志"
        }

    # 股票筛选
    if any(kw in query for kw in ["筛选", "过滤", "找", "推荐", "pe", "roe", "市值"]):
        filters = _extract_filters(query)
        return {
            "ok": True,
            "action": "screen_stocks",
            "filters": filters,
            "intent": "股票筛选"
        }

    # 数据获取
    if any(kw in query for kw in ["获取", "查询股票", "k线", "行情", "走势"]):
        symbol = _extract_symbol(query)
        return {
            "ok": True,
            "action": "fetch_stock_data",
            "symbol": symbol or "000001",
            "intent": "获取股票数据"
        }

    # 回测
    if any(kw in query for kw in ["回测", "策略", "ma", "macd"]):
        return {
            "ok": True,
            "action": "run_backtest",
            "strategy": "macd" if "macd" in query else "ma",
            "intent": "运行金融回测"
        }

    # 报告
    if any(kw in query for kw in ["报告", "report", "分析文档", "总结"]):
        return {
            "ok": True,
            "action": "generate_report",
            "template": "data_exploration",
            "intent": "生成分析报告"
        }

    return {
        "ok": False,
        "error": "无法识别查询意图",
        "detail": f"请尝试使用更具体的关键词，如'筛选'、'获取'、'回测'、'报告'、'日志'等",
        "query": query
    }


def _extract_filters(query: str) -> Dict:
    """从自然语言中提取筛选条件"""
    import re
    filters = {}

    # PE
    pe_match = re.search(r'(?:pe|市盈率)\s*(?:小于|低于|<|<=)\s*(\d+)', query)
    if pe_match:
        filters['pe_max'] = float(pe_match.group(1))
    pe_match2 = re.search(r'(?:pe|市盈率)\s*(?:大于|高于|>|>=)\s*(\d+)', query)
    if pe_match2:
        filters['pe_min'] = float(pe_match2.group(1))

    # ROE
    roe_match = re.search(r'(?:roe|净资产收益率)\s*(?:大于|高于|>|>=)\s*(\d+)', query)
    if roe_match:
        filters['roe_min'] = float(roe_match.group(1))

    # 市值（亿）
    mcap_match = re.search(r'(?:市值|盘子)\s*(?:大于|高于|>|>=)\s*(\d+)\s*(?:亿)?', query)
    if mcap_match:
        filters['mcap_min'] = float(mcap_match.group(1))

    # 价格
    price_match = re.search(r'(?:股价|价格|最新价)\s*(?:小于|低于|<|<=)\s*(\d+)', query)
    if price_match:
        filters['price_max'] = float(price_match.group(1))

    return filters


def _extract_symbol(query: str) -> Optional[str]:
    """从自然语言中提取股票代码"""
    import re
    # 6位数字
    match = re.search(r'\b(\d{6})\b', query)
    if match:
        return match.group(1)
    return None


@safe_call
def handle_log_query(table: str, columns: List[str] = None,
                     where: List[Dict] = None,
                     order_by: List[Dict] = None,
                     limit: int = 50) -> Dict:
    """日志查询"""
    return execute_log_query(table, columns, where, order_by, limit)


@safe_call
def handle_system_health() -> Dict:
    """系统健康检查"""
    from monitoring import get_system_health, task_tracker
    return {
        "ok": True,
        "health": get_system_health(),
        "tasks": task_tracker.get_tasks()
    }


# ==================== 路由器 ====================

NLQ_ROUTER: Dict[str, callable] = {
    "screen_stocks": handle_screen_stocks,
    "fetch_stock_data": handle_fetch_stock_data,
    "run_backtest": handle_run_backtest,
    "generate_report": handle_generate_report,
    "classify_intent": handle_classify_intent,
    "execute_log_query": handle_log_query,
    "system_health": handle_system_health,
}


def nlq_execute(instruction: Dict[str, Any]) -> Dict[str, Any]:
    """
    接收 LLM 返回的 JSON 指令，路由到对应函数执行。

    Args:
        instruction: {"action": "xxx", ...参数}

    Returns:
        {"ok": True/False, "data"/"error": ...}

    安全机制：
    1. action 必须在 NLQ_ROUTER 白名单中
    2. 所有异常被 @safe_call 捕获
    3. 不做任何 eval/exec
    """
    if not isinstance(instruction, dict):
        return {"ok": False, "error": "指令必须是 JSON 对象"}

    action = instruction.get("action")
    if not action:
        return {"ok": False, "error": "缺少 action 字段"}

    if action not in NLQ_ROUTER:
        return {
            "ok": False,
            "error": f"不支持的操作: {action}",
            "available_actions": list(NLQ_ROUTER.keys())
        }

    handler = NLQ_ROUTER[action]
    # 移除 action 本身，其余全部作为参数传入
    kwargs = {k: v for k, v in instruction.items() if k != "action"}
    return handler(**kwargs)


def nlq_query(query: str) -> Dict[str, Any]:
    """快捷入口：自然语言 query → 意图识别 → 执行"""
    intent_result = handle_classify_intent(query)
    if not intent_result["ok"]:
        return intent_result

    action = intent_result["action"]
    if action in NLQ_ROUTER:
        kwargs = {k: v for k, v in intent_result.items() if k not in ("ok", "action", "intent", "query")}
        result = NLQ_ROUTER[action](**kwargs)
        result["intent"] = intent_result.get("intent")
        return result

    return {"ok": False, "error": f"意图识别结果无法执行: {action}"}
