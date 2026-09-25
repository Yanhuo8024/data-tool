# -*- coding: utf-8 -*-
"""duckdb_engine.py 单测：视图注册 / 只读查询 / 描述统计 / 大文件加载 / 无 duckdb 降级"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import duckdb_engine as de
from duckdb_engine import HAS_DUCKDB, _view_name, register_file, query_file, \
    quick_describe, large_load_dataframe

needs_duckdb = pytest.mark.skipif(not HAS_DUCKDB, reason='duckdb 未安装')


@pytest.fixture(scope="module")
def csv_path(tmp_path_factory):
    """生成 10 万行测试 CSV（含数值/分类/空值）"""
    np.random.seed(42)
    n = 100_000
    df = pd.DataFrame({
        'grp': np.random.choice(['A', 'B', 'C'], n),
        'x': np.random.randn(n),
        'y': np.random.randn(n) * 2,
        'z': np.random.choice([1.0, np.nan], n, p=[0.9, 0.1]),
    })
    p = tmp_path_factory.mktemp('duckdb') / 'big_test.csv'
    df.to_csv(p, index=False)
    return str(p)


def test_view_name_rules():
    assert _view_name('C:/data/finance_ml_final.csv') == 'v_finance_ml_final'
    assert _view_name('data/2024_q1.csv') == 'v_t_2024_q1'          # 数字开头加前缀
    assert _view_name('a/b/报告-数据.csv') == 'v______'              # 非 ASCII 字符→下划线
    assert _view_name('x/y/z.parquet') == 'v_z'


@needs_duckdb
def test_register_and_query(csv_path):
    view = register_file(csv_path)
    assert view == 'v_big_test'
    df = query_file(csv_path, f'SELECT count(*) AS n, count(distinct grp) AS g FROM "{view}"')
    assert df is not None
    assert df['n'].iloc[0] == 100_000
    assert df['g'].iloc[0] == 3


@needs_duckdb
def test_query_group_by(csv_path):
    view = register_file(csv_path)
    df = query_file(csv_path, f'SELECT grp, avg(x) AS mx FROM "{view}" GROUP BY grp ORDER BY grp')
    assert df is not None
    assert list(df['grp']) == ['A', 'B', 'C']
    assert df['mx'].notna().all()


@needs_duckdb
def test_query_limit(csv_path):
    df = query_file(csv_path, 'SELECT * FROM v_big_test', limit=50)
    assert len(df) == 50


@needs_duckdb
def test_quick_describe(csv_path):
    res = quick_describe(csv_path)
    assert res is not None
    assert res['rows'] == 100_000
    assert 'x' in res['numeric'] and 'y' in res['numeric']
    # 数值统计合理性：x ~ N(0,1)
    assert abs(res['stats']['x']['mean']) < 0.05
    assert abs(res['stats']['x']['std'] - 1) < 0.1
    # 缺失值列 z 仍统计非空计数
    assert res['stats']['z']['count'] < 100_000


@needs_duckdb
def test_large_load_dataframe(csv_path):
    df = large_load_dataframe(csv_path, nrows=10_000)
    assert len(df) == 10_000
    assert set(['grp', 'x', 'y', 'z']).issubset(df.columns)
    df_all = large_load_dataframe(csv_path)
    assert len(df_all) == 100_000


def test_missing_file_returns_none(csv_path, monkeypatch):
    if not HAS_DUCKDB:
        pytest.skip('duckdb 未安装')
    de._registered.clear()
    assert register_file('C:/nonexistent/no.csv') is None
    assert query_file('C:/nonexistent/no.csv', 'SELECT 1') is None
    assert large_load_dataframe('C:/nonexistent/no.csv') is None
    # 回归：存在文件时重复注册幂等（视图名稳定）
    view1 = register_file(csv_path)
    view2 = register_file(csv_path)
    assert view1 == view2


def test_fallback_without_duckdb(csv_path, monkeypatch):
    """无 duckdb 时全部接口返回 None（调用方走 pandas 兜底）"""
    monkeypatch.setattr(de, 'HAS_DUCKDB', False)
    assert register_file(csv_path) is None
    assert query_file(csv_path, 'SELECT 1') is None
    assert quick_describe(csv_path) is None
    assert large_load_dataframe(csv_path) is None
