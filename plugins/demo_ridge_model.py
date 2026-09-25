#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
示例插件：Ridge 回归（model 类型）
接口：PLUGIN_META + run(df, params) -> dict（结果 JSON 安全）
"""
import json

import numpy as np
import pandas as pd

PLUGIN_META = {
    "name": "Ridge 回归",
    "type": "model",
    "description": "岭回归建模：特征列 → 目标列，返回系数与 R²",
    "version": "0.1",
}


def _json_safe(obj):
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


def run(df: pd.DataFrame, params: dict) -> dict:
    target = params.get("target")
    features = params.get("features") or []
    alpha = float(params.get("alpha", 1.0))
    if not target or target not in df.columns:
        raise ValueError("请指定目标列 target")
    if not features:
        features = [c for c in df.select_dtypes('number').columns if c != target][:5]
    missing = [c for c in features if c not in df.columns]
    if missing:
        raise ValueError(f"特征列不存在: {missing}")
    if not features:
        raise ValueError("无可用数值特征列")

    from sklearn.linear_model import Ridge
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import r2_score, mean_squared_error

    sub = df[features + [target]].dropna()
    if len(sub) < 10:
        raise ValueError(f"有效样本不足（{len(sub)}<10）")
    X = sub[features].astype(float)
    y = sub[target].astype(float)
    Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=0.25, random_state=42)
    model = Ridge(alpha=alpha)
    model.fit(Xtr, ytr)
    yp = model.predict(Xte)
    return _json_safe({
        "r2": float(r2_score(yte, yp)),
        "rmse": float(np.sqrt(mean_squared_error(yte, yp))),
        "coefficients": dict(zip(features, model.coef_.tolist())),
        "intercept": float(model.intercept_),
        "n_train": int(len(Xtr)),
        "n_test": int(len(Xte)),
        "features": features,
    })
