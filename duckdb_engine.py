#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
duckdb_engine.py — DuckDB 大数据量分析引擎
针对 GB 级金融数据（十年 Tick / 海量面板）：
- 注册 CSV/Parquet 为视图（惰性，不复制数据）
- 只读 SQL 查询 / 聚合描述统计（DuckDB 列式引擎，远快于纯 pandas）
- 与 pandas 无缝互转；无 duckdb 时自动降级（调用方走 pandas 兜底）
"""
import os
from pathlib import Path

HAS_DUCKDB = False
try:
    import duckdb
    HAS_DUCKDB = True
except ImportError:
    duckdb = None

_conn = None
_registered = {}  # path -> view name
_MAX_ROWS = 500


def get_duck():
    """惰性获取内存连接"""
    global _conn
    if _conn is None:
        _conn = duckdb.connect()  # 内存数据库，无需部署
    return _conn


def _norm_path(path) -> str:
    return str(path).replace('\\', '/')


def _view_name(path: str) -> str:
    """稳定视图名：文件主名（去扩展名与特殊字符），用户 SQL 可直接引用"""
    import re
    stem = Path(_norm_path(path)).stem
    stem = re.sub(r'[^A-Za-z0-9_]', '_', stem)
    if not stem or stem[0].isdigit():
        stem = 't_' + stem
    return f'v_{stem}'


def register_file(path) -> str:
    """注册 CSV/Parquet 为 DuckDB 视图，返回视图名；失败返回 None"""
    if not HAS_DUCKDB:
        return None
    path = _norm_path(path)
    if path in _registered:
        return _registered[path]
    if not os.path.exists(path):
        return None
    view = _view_name(path)
    con = get_duck()
    try:
        con.execute(f'DROP VIEW IF EXISTS "{view}"')
        if path.lower().endswith(('.csv', '.tsv', '.txt')):
            sql = (f'CREATE OR REPLACE VIEW "{view}" AS '
                   f"SELECT * FROM read_csv_auto('{path}')")
        elif path.lower().endswith('.parquet'):
            sql = f'CREATE OR REPLACE VIEW "{view}" AS SELECT * FROM read_parquet(\'{path}\')'
        else:
            return None
        con.execute(sql)
        _registered[path] = view
        return view
    except Exception:
        try:
            con.execute(f'DROP VIEW IF EXISTS "{view}"')
        except Exception:
            pass
        return None


def query_file(path, sql: str, limit: int = _MAX_ROWS):
    """对文件执行只读 SQL（语句已由外层 sql_guard 校验），返回 DataFrame"""
    if not HAS_DUCKDB:
        return None
    view = register_file(path)
    if not view:
        return None
    con = get_duck()
    try:
        df = con.execute(sql).df()
        if limit and len(df) > limit:
            df = df.head(limit)
        return df
    except Exception:
        return None


def quick_describe(path, numeric_cols=None):
    """DuckDB 聚合描述统计（count/avg/stddev/min/max/p50），GB 级秒级"""
    if not HAS_DUCKDB:
        return None
    view = register_file(path)
    if not view:
        return None
    con = get_duck()
    try:
        total = int(con.execute(f'SELECT count(*) FROM "{view}"').fetchone()[0])
        sample = con.execute(f'SELECT * FROM "{view}" LIMIT 2000').df()
        cols = list(sample.columns)
        num = []
        for c in cols:
            if numeric_cols and c not in numeric_cols:
                continue
            if sample[c].dtype.kind in 'fiu':
                num.append(c)
        stats = {}
        for c in num:
            try:
                row = con.execute(
                    f'SELECT count("{c}"), avg("{c}"), stddev("{c}"), '
                    f'min("{c}"), max("{c}"), approx_quantile("{c}", 0.5) '
                    f'FROM "{view}"').fetchone()
                stats[c] = {
                    'count': int(row[0] or 0),
                    'mean': round(float(row[1]), 4) if row[1] is not None else None,
                    'std': round(float(row[2]), 4) if row[2] is not None else None,
                    'min': round(float(row[3]), 4) if row[3] is not None else None,
                    'max': round(float(row[4]), 4) if row[4] is not None else None,
                    'p50': round(float(row[5]), 4) if row[5] is not None else None,
                }
            except Exception:
                continue
        return {'columns': cols, 'numeric': num, 'rows': total, 'stats': stats}
    except Exception:
        return None


def large_load_dataframe(path, nrows=None):
    """大文件加载：duckdb 读取（列式，内存友好），返回 pandas DataFrame"""
    if not HAS_DUCKDB:
        return None
    path = _norm_path(path)
    if not os.path.exists(path):
        return None
    con = get_duck()
    try:
        limit_sql = f' LIMIT {int(nrows)}' if nrows else ''
        if path.lower().endswith(('.csv', '.tsv', '.txt')):
            df = con.execute(f"SELECT * FROM read_csv_auto('{path}'){limit_sql}").df()
        elif path.lower().endswith('.parquet'):
            df = con.execute(f"SELECT * FROM read_parquet('{path}'){limit_sql}").df()
        else:
            return None
        return df
    except Exception:
        return None
