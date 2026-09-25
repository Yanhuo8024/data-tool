#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
示例插件：散点图 + 回归线（chart 类型）
接口：PLUGIN_META + render(df, params) -> dict
"""
import base64
import io

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import pandas as pd

PLUGIN_META = {
    "name": "散点图+回归线",
    "type": "chart",
    "description": "绘制两列的散点图并叠加线性拟合线",
    "version": "0.1",
}


def render(df: pd.DataFrame, params: dict) -> dict:
    x = params.get("x") or (df.select_dtypes('number').columns.tolist() or [None])[0]
    y = params.get("y")
    if y is None:
        nums = [c for c in df.select_dtypes('number').columns if c != x]
        y = nums[0] if nums else None
    if x is None or y is None or x not in df.columns or y not in df.columns:
        raise ValueError("需要指定两个数值列（x/y）")
    sub = df[[x, y]].dropna()
    fig, ax = plt.subplots(figsize=(7, 4.5))
    ax.scatter(sub[x], sub[y], alpha=0.55, s=32, color='#FF6B6B', edgecolors='white')
    if len(sub) > 2:
        import numpy as np
        k, b = np.polyfit(sub[x], sub[y], 1)
        xs = np.linspace(sub[x].min(), sub[x].max(), 100)
        ax.plot(xs, k * xs + b, color='#5E6AD2', linewidth=2, label=f'y={k:.3f}x{b:+.3f}')
        ax.legend()
    ax.set_xlabel(x)
    ax.set_ylabel(y)
    ax.set_title(f'{x} vs {y}（插件渲染）')
    ax.grid(alpha=0.3, linestyle='--')
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=100, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    buf.seek(0)
    return {
        "chart": "data:image/png;base64," + base64.b64encode(buf.read()).decode(),
        "n": int(len(sub)),
        "note": f"样本量 {len(sub)}",
    }
