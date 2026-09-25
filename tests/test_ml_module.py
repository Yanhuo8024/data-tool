# -*- coding: utf-8 -*-
"""ml_module.py 单测：回归/分类/聚类训练器核心流程"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml_module import MLTrainer, get_ml_model_list


@pytest.fixture(scope="module")
def df():
    np.random.seed(42)
    n = 300
    x1 = np.random.randn(n)
    x2 = np.random.randn(n) * 0.5
    cat = np.random.choice(['A', 'B', 'C'], n)
    y_reg = 2.0 * x1 - 1.5 * x2 + np.random.randn(n) * 0.3
    y_cls = (y_reg > 0).astype(int)
    return pd.DataFrame({'x1': x1, 'x2': x2, 'cat': cat, 'y_reg': y_reg, 'y_cls': y_cls})


def test_model_catalog():
    cat = get_ml_model_list()
    assert 'regression' in cat and 'classification' in cat
    assert len(cat['regression']) >= 3


def test_train_regression_linear(df):
    trainer = MLTrainer(df)
    res = trainer.train_regression(['x1', 'x2', 'cat'], 'y_reg', model_name='linear_regression')
    assert res['model_type'] == 'regression'
    m = res['metrics']
    assert 'test_r2' in m and 'test_rmse' in m
    assert m['test_r2'] > 0.7  # 线性关系应拟合良好
    # 特征重要性（线性模型用 |coef|）
    assert 'feature_importance' in m


def test_train_regression_forest(df):
    trainer = MLTrainer(df)
    res = trainer.train_regression(['x1', 'x2'], 'y_reg', model_name='random_forest')
    assert res['metrics']['test_r2'] > 0.5


def test_train_classification(df):
    trainer = MLTrainer(df)
    res = trainer.train_classification(['x1', 'x2', 'cat'], 'y_cls', model_name='logistic_regression')
    assert res['model_type'] == 'classification'
    m = res['metrics']
    assert 'test_accuracy' in m
    assert m['test_accuracy'] > 0.7


def test_predict_consistency(df):
    trainer = MLTrainer(df)
    trainer.train_regression(['x1', 'x2'], 'y_reg')
    X_new = df[['x1', 'x2']].head(10)
    preds = trainer.predict(X_new)
    assert len(preds) == 10
    assert np.isfinite(preds).all()


def test_too_few_samples_raises():
    small = pd.DataFrame({'a': [1.0, 2.0], 'b': [1.0, 0.0]})
    trainer = MLTrainer(small)
    with pytest.raises(ValueError):
        trainer.train_regression(['a'], 'b')
