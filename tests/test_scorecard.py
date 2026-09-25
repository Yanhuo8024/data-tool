# -*- coding: utf-8 -*-
"""scorecard.py 单测"""
import os
import sys
import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scorecard import (bin_series, woe_assign, build_scorecard,
                       apply_scorecard, psi_score, save_scorecard,
                       load_scorecard, list_scorecards)


@pytest.fixture(scope="module")
def df():
    np.random.seed(42)
    n = 1500
    d = pd.DataFrame({
        'age': np.random.randint(20, 65, n).astype(float),
        'income': np.random.lognormal(10, 0.6, n),
        'debt_ratio': np.clip(np.random.normal(0.4, 0.2, n), 0, 1.5),
        'noise': np.random.rand(n),
    })
    p = 1 / (1 + np.exp(-(2.0 - 0.06 * d['age']
                           - 0.6 * np.log(d['income'] / 20000)
                           + 1.2 * d['debt_ratio'])))
    d['default'] = (np.random.rand(n) < p).astype(int)
    return d


def test_bin_series_quantile(df):
    res = bin_series(df['age'], df['default'], method='quantile', max_bins=5)
    assert res['iv'] > 0
    assert 2 <= len(res['table']) <= 6
    # assign 可复现
    woe = woe_assign(df['age'], res)
    assert woe.notna().all()
    assert abs(woe.mean()) < 50  # woe 值合理


def test_bin_series_monotonic(df):
    res = bin_series(df['debt_ratio'], df['default'], method='chimerge',
                     max_bins=5, monotonic=True)
    w = [t['woe'] for t in res['table'] if t['bin'] != '缺失']
    diffs = [w[i + 1] - w[i] for i in range(len(w) - 1)]
    assert all(d >= 0 for d in diffs) or all(d <= 0 for d in diffs)


def test_build_scorecard_quality(df):
    res = build_scorecard(df, 'default', ['age', 'income', 'debt_ratio', 'noise'],
                          min_iv=0.02)
    assert res['ok']
    assert 'noise' not in res['kept_features']  # 纯噪声应被 IV 筛掉
    assert res['eval']['test_auc'] > 0.6
    assert res['eval']['ks'] > 0.2
    assert res['eval']['avg_score_good'] > res['eval']['avg_score_bad']


def test_apply_scorecard_consistency(df):
    res = build_scorecard(df, 'default', ['age', 'income', 'debt_ratio'], seed=1)
    out = apply_scorecard(df, res)
    assert 'score' in out.columns and 'risk_level' in out.columns
    # 训练集打分应可复现：同一数据两次打分一致
    out2 = apply_scorecard(df, res)
    assert (out['score'] == out2['score']).all()


def test_psi_stable_vs_drift(df):
    res = build_scorecard(df, 'default', ['age', 'income', 'debt_ratio'], seed=2)
    s1 = apply_scorecard(df, res)['score']
    df2 = df.sample(500, random_state=1)
    s2 = apply_scorecard(df2, res)['score']
    assert psi_score(s1, s2) < 0.1  # 同分布稳定
    df3 = df.copy()
    df3['debt_ratio'] = df3['debt_ratio'] + 0.4
    s3 = apply_scorecard(df3, res)['score']
    assert psi_score(s1, s3) > 0.1  # 偏移检出（>0.1 至少轻度偏移）


def test_save_load_roundtrip(df, tmp_path, monkeypatch):
    import scorecard as sc
    monkeypatch.setattr(sc, 'MODEL_DIR', str(tmp_path))
    res = build_scorecard(df, 'default', ['age', 'income', 'debt_ratio'], seed=3)
    fname = save_scorecard(res, '测试模型')
    assert os.path.exists(os.path.join(str(tmp_path), fname))
    loaded = load_scorecard(fname)
    assert loaded['model']['A'] == res['model']['A']
    assert len(list_scorecards()) == 1
