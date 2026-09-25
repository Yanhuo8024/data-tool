# -*- coding: utf-8 -*-
"""did.py 单测：数据清洗 / DID 回归 / 安慰剂检验（合成面板数据）"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import did
from did import clean_data, did_regression, placebo_test, format_regression_table

# linearmodels 缺失时跳过回归相关测试
try:
    import linearmodels  # noqa: F401
    _NO_LM = False
except ImportError:
    _NO_LM = True

needs_lm = pytest.mark.skipif(_NO_LM, reason='linearmodels 未安装')


@pytest.fixture(scope="module")
def panel_df():
    """合成面板数据：真实处理效应 = +2.0"""
    np.random.seed(42)
    n_units, n_years = 40, 8
    rows = []
    for u in range(n_units):
        unit_fe = np.random.randn() * 2
        treated = u < 20  # 前20个个体为处理组
        for t in range(n_years):
            year = 2015 + t
            treat = 1 if (treated and year >= 2019) else 0  # 2019 起处理
            noise = np.random.randn()
            y = 10 + unit_fe + 0.5 * (year - 2015) + 2.0 * treat + noise
            x = np.random.randn() + 0.3 * treat
            rows.append({'province': f'P{u:02d}', 'year': year, 'treat': treat,
                         'y': y, 'x1': x})
    return pd.DataFrame(rows)


def test_clean_data_no_missing(panel_df):
    res = clean_data(panel_df.copy(), consume_cols=['x1'], treat_col='treat', time_col='year')
    assert 'cleaned_df' in res and 'report' in res
    assert len(res['cleaned_df']) == len(panel_df)


def test_clean_data_normalizes_consume_sum():
    df = pd.DataFrame({
        'a': [60.0, 30.0], 'b': [40.0, 30.0], 'treat': [1, 0], 'year': [2020, 2021]
    })
    res = clean_data(df.copy(), consume_cols=['a', 'b'], treat_col='treat', time_col='year')
    clean = res['cleaned_df']
    assert abs(clean['a'].iloc[0] + clean['b'].iloc[0] - 100) < 1e-6


@needs_lm
def test_did_regression_detects_effect(panel_df):
    res = did_regression(panel_df, 'y', 'treat', 'year', 'province', control_vars=['x1'])
    assert 'error' not in res
    # 真实效应为 2.0，应检出显著正系数（交互项键名为 treat_x_post）
    assert res['params']['treat_x_post'] > 1.0
    assert res['pvalues']['treat_x_post'] < 0.01
    # PanelOLS 的 rsquared 为组内 R²（不含个体/时间固定效应解释力），阈值放宽
    assert res['r2'] > 0.1
    assert res['n_obs'] > 0


@needs_lm
def test_did_regression_no_cluster(panel_df):
    res = did_regression(panel_df, 'y', 'treat', 'year', 'province', cluster=False)
    assert 'error' not in res


@needs_lm
def test_placebo_test_runs(panel_df):
    """小样本安慰剂检验：真实效应应显著大于安慰剂分布"""
    res = placebo_test(panel_df, 'y', 'treat', 'year', 'province',
                       control_vars=['x1'], n_placebo=30, seed=1)
    assert 'p_value' in res and 'passed' in res
    assert 0 <= res['p_value'] <= 1
    assert res['true_coef'] > 1.0
    assert res['n_success'] > 0
    # 固定随机种子下结果可复现
    res2 = placebo_test(panel_df, 'y', 'treat', 'year', 'province',
                        control_vars=['x1'], n_placebo=30, seed=1)
    assert res['p_value'] == res2['p_value']


def test_format_regression_table():
    # 模拟 did_regression 的返回结构
    rows = [{
        'model_name': 'DID(1)',
        'params': {'treat': 2.0, 'const': 10.0},
        'se': {'treat': 0.4, 'const': 0.5},
        'pvalues': {'treat': 0.001, 'const': 0.0},
        'n_obs': 320,
        'r2': 0.2,
    }]
    html = format_regression_table(rows, title='DID回归结果')
    assert '<table' in html and 'treat' in html and 'DID(1)' in html
    assert '320' in html  # 观测数
    # 空列表返回空字符串
    assert format_regression_table([]) == ''
