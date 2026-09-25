#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
feature_engineering.py - 时序特征 & 交叉特征自动生成
"""

import pandas as pd
import numpy as np
from typing import List, Optional, Dict


def add_lag_features(df, columns, lags=None, drop_na=False):
    if lags is None: lags = [1, 2, 3]
    result = df.copy()
    for col in columns:
        if col not in result.columns: continue
        for lag in lags:
            result[f'{col}_lag{lag}'] = result[col].shift(lag)
    if drop_na: result = result.dropna().reset_index(drop=True)
    return result


def add_rolling_features(df, columns, windows=None, stats=None, drop_na=False):
    if windows is None: windows = [5, 10, 20]
    if stats is None: stats = ['mean', 'std', 'min', 'max']
    result = df.copy()
    for col in columns:
        if col not in result.columns: continue
        for window in windows:
            for stat in stats:
                result[f'{col}_roll_{stat}_{window}'] = result[col].rolling(window=window, min_periods=1).agg(stat)
    if drop_na: result = result.dropna().reset_index(drop=True)
    return result


def add_growth_features(df, columns, periods=None, drop_na=False):
    if periods is None: periods = [1, 4, 12]
    result = df.copy()
    for col in columns:
        if col not in result.columns: continue
        for period in periods:
            result[f'{col}_growth_m{period}'] = result[col].pct_change(period)
    if drop_na: result = result.dropna().reset_index(drop=True)
    return result


def add_diff_features(df, columns, periods=None, drop_na=False):
    if periods is None: periods = [1, 2]
    result = df.copy()
    for col in columns:
        if col not in result.columns: continue
        for period in periods:
            result[f'{col}_diff{period}'] = result[col].diff(period)
    if drop_na: result = result.dropna().reset_index(drop=True)
    return result


def add_date_features(df, date_col='date', features=None):
    if features is None: features = ['year', 'month', 'day', 'weekday', 'quarter']
    result = df.copy()
    if date_col not in result.columns: return result
    dt = pd.to_datetime(result[date_col])
    feat_map = {
        'year': dt.dt.year, 'month': dt.dt.month, 'day': dt.dt.day,
        'weekday': dt.dt.weekday, 'quarter': dt.dt.quarter
    }
    for feat in features:
        if feat in feat_map:
            result[f'{date_col}_{feat}'] = feat_map[feat]
    return result


def add_cross_cat_features(df, cat_cols, sep='_', max_features=50):
    result = df.copy()
    count = 0
    for i in range(len(cat_cols)):
        for j in range(i + 1, len(cat_cols)):
            if count >= max_features: break
            col1, col2 = cat_cols[i], cat_cols[j]
            if col1 in result.columns and col2 in result.columns:
                result[f'{col1}{sep}{col2}'] = result[col1].astype(str) + sep + result[col2].astype(str)
                count += 1
    return result


def add_cross_num_features(df, num_cols, ops=None, max_features=100):
    if ops is None: ops = ['add', 'sub', 'mul', 'div']
    result = df.copy()
    count = 0
    for i in range(len(num_cols)):
        for j in range(i + 1, len(num_cols)):
            if count >= max_features: break
            col1, col2 = num_cols[i], num_cols[j]
            if col1 not in result.columns or col2 not in result.columns: continue
            for op in ops:
                name = f'{col1}_{op}_{col2}'
                if op == 'add': result[name] = result[col1] + result[col2]
                elif op == 'sub': result[name] = result[col1] - result[col2]
                elif op == 'mul': result[name] = result[col1] * result[col2]
                elif op == 'div': result[name] = result[col1] / result[col2].replace(0, np.nan)
                count += 1
    return result


def add_cross_stat_features(df, num_cols, stats=None):
    if stats is None: stats = ['mean', 'std', 'max', 'min', 'range']
    result = df.copy()
    valid_cols = [c for c in num_cols if c in result.columns]
    if not valid_cols: return result
    data = result[valid_cols]
    for stat in stats:
        if stat == 'mean': result['cross_mean'] = data.mean(axis=1)
        elif stat == 'std': result['cross_std'] = data.std(axis=1)
        elif stat == 'max': result['cross_max'] = data.max(axis=1)
        elif stat == 'min': result['cross_min'] = data.min(axis=1)
        elif stat == 'range': result['cross_range'] = data.max(axis=1) - data.min(axis=1)
    return result


def auto_feature_pipeline(df, num_cols=None, cat_cols=None, date_col=None,
                          ts_config=None, cross_config=None, drop_na=False):
    result = df.copy()
    if num_cols is None: num_cols = result.select_dtypes(include=[np.number]).columns.tolist()
    if cat_cols is None: cat_cols = result.select_dtypes(include=['object', 'category']).columns.tolist()
    if ts_config is None: ts_config = {'lag': {'lags': [1, 2, 3]}, 'rolling': {'windows': [5, 10], 'stats': ['mean', 'std']}}
    if cross_config is None: cross_config = {'cat_cross': True, 'num_cross': True, 'num_ops': ['add', 'sub', 'mul', 'div']}
    if date_col and date_col in result.columns: result = add_date_features(result, date_col)
    if ts_config.get('lag'): result = add_lag_features(result, num_cols, **ts_config['lag'])
    if ts_config.get('rolling'): result = add_rolling_features(result, num_cols, **ts_config['rolling'])
    if ts_config.get('growth'): result = add_growth_features(result, num_cols, **ts_config['growth'])
    if ts_config.get('diff'): result = add_diff_features(result, num_cols, **ts_config['diff'])
    if cross_config.get('cat_cross') and len(cat_cols) >= 2: result = add_cross_cat_features(result, cat_cols)
    if cross_config.get('num_cross') and len(num_cols) >= 2:
        ops = cross_config.get('num_ops', ['add', 'sub', 'mul', 'div'])
        result = add_cross_num_features(result, num_cols, ops=ops)
    if cross_config.get('stat_cross') and len(num_cols) >= 2: result = add_cross_stat_features(result, num_cols)
    if drop_na: result = result.dropna().reset_index(drop=True)
    return result
