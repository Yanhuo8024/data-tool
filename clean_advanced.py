"""
数据清洗增强模块 — 文本清洗 / 编码 / 日期 / 批量 / 日志 / 缺失热力图
"""

import base64, io, json, os, re
from datetime import datetime
from collections import OrderedDict

import pandas as pd
import numpy as np

try:
    import jieba
    HAS_JIEBA = True
except ImportError:
    HAS_JIEBA = False

try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import seaborn as sns
    HAS_MPL = True
except ImportError:
    HAS_MPL = False


# ==================== 操作日志管理 ====================

class CleaningLogger:
    """清洗操作日志，支持撤销"""

    def __init__(self, filepath):
        self.filepath = filepath
        self.df = pd.read_csv(filepath)
        self.history = []  # [(desc, df_snapshot)]
        self._save_snapshot("初始状态")

    def _save_snapshot(self, desc):
        self.history.append((desc, self.df.copy()))

    def get_df(self):
        return self.df

    def commit(self, desc, new_df):
        self._save_snapshot(desc)
        self.df = new_df

    def undo(self):
        if len(self.history) <= 1:
            return False, "无法撤销，已到初始状态"
        self.history.pop()  # 移除当前
        prev_desc, prev_df = self.history[-1]
        self.df = prev_df
        return True, prev_desc

    def get_log(self):
        return [(i, desc, len(df)) for i, (desc, df) in enumerate(self.history)]

    def save(self, output_path):
        self.df.to_csv(output_path, index=False, encoding='utf-8-sig')


_loggers = {}  # 按文件路径缓存


def get_logger(filepath):
    key = str(filepath)
    if key not in _loggers:
        _loggers[key] = CleaningLogger(filepath)
    return _loggers[key]


def reset_logger(filepath):
    key = str(filepath)
    if key in _loggers:
        del _loggers[key]
    return get_logger(filepath)


def img_to_b64(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=100, bbox_inches='tight',
                facecolor='#0f111c', edgecolor='none')
    buf.seek(0)
    return base64.b64encode(buf.read()).decode()


# ==================== 文本清洗 ====================

def clean_text_column(df, column, to_lower=True, to_upper=False,
                       remove_special=True, trim_spaces=True, tokenize=False):
    """
    金融文本清洗：去空格/大小写/特殊符号/分词
    """
    if column not in df.columns:
        raise ValueError(f"列 '{column}' 不存在")

    s = df[column].astype(str).copy()

    if trim_spaces:
        s = s.str.strip().str.replace(r'\s+', ' ', regex=True)
    if to_lower:
        s = s.str.lower()
    if to_upper:
        s = s.str.upper()
    if remove_special:
        # 保留字母/数字/中文/空格/基本标点
        # pandas 3.x Arrow regex: no backslash-u escapes; use literal CJK range
        s = s.str.replace(r'[^\w一-鿿\s.,;:!?()（）%￥$]', '', regex=True)

    df = df.copy()
    df[column] = s
    result = {"ok": True, "column": column, "rows_processed": len(df)}

    if tokenize and HAS_JIEBA:
        tokens = df[column].apply(lambda x: ' '.join(jieba.cut(str(x))))
        df[column + '_tokens'] = tokens
        result["token_column"] = column + '_tokens'

    return df, result


# ==================== 分类变量处理 ====================

def encode_categorical(df, column, method='onehot', drop_first=False):
    """
    分类变量编码：独热编码 / 标签编码
    """
    if column not in df.columns:
        raise ValueError(f"列 '{column}' 不存在")

    df = df.copy()

    if method == 'onehot':
        dummies = pd.get_dummies(df[column], prefix=column, drop_first=drop_first)
        df = pd.concat([df.drop(columns=[column]), dummies], axis=1)
        return df, {
            "ok": True, "method": "onehot", "column": column,
            "new_columns": dummies.columns.tolist(),
            "n_categories": len(dummies.columns),
        }

    elif method == 'label':
        from sklearn.preprocessing import LabelEncoder
        le = LabelEncoder()
        df[column + '_encoded'] = le.fit_transform(df[column].astype(str))
        mapping = {str(k): int(v) for k, v in zip(le.classes_, le.transform(le.classes_))}
        return df, {
            "ok": True, "method": "label", "column": column,
            "new_column": column + '_encoded',
            "mapping": mapping,
            "n_categories": len(le.classes_),
        }

    raise ValueError(f"不支持的编码方法: {method}")


# ==================== 日期处理 ====================

def standardize_dates(df, column, target_format='%Y-%m-%d',
                       remove_invalid=True, errors='coerce'):
    """
    日期统一格式化，清除非法日期
    """
    if column not in df.columns:
        raise ValueError(f"列 '{column}' 不存在")

    df = df.copy()
    original_count = len(df)

    # 尝试多种常见格式
    parsed = pd.to_datetime(df[column], errors='coerce')

    invalid_mask = parsed.isna()
    n_invalid = invalid_mask.sum()

    if remove_invalid:
        df = df[~invalid_mask].copy()
        df[column] = parsed[~invalid_mask].dt.strftime(target_format)
    else:
        df[column] = parsed.dt.strftime(target_format)
        df.loc[n_invalid, column] = 'Invalid'

    return df, {
        "ok": True,
        "column": column,
        "rows_before": original_count,
        "rows_after": len(df),
        "invalid_removed": n_invalid,
        "target_format": target_format,
    }


# ==================== 批量操作 ====================

def batch_replace(df, column, old_value, new_value, regex=False):
    """批量替换指定值"""
    df = df.copy()
    if column not in df.columns:
        raise ValueError(f"列 '{column}' 不存在")

    before = df[column].astype(str)
    if regex:
        df[column] = df[column].astype(str).str.replace(old_value, new_value, regex=True)
    else:
        to_replace = {old_value: new_value}
        if old_value.replace('.', '').isdigit():
            to_replace = {float(old_value): float(new_value)} if '.' in new_value else {float(old_value): new_value}
        df[column] = df[column].replace(to_replace)

    changed = (before != df[column].astype(str)).sum()
    return df, {
        "ok": True, "column": column,
        "changed_count": int(changed),
        "old_value": old_value, "new_value": new_value,
    }


def condition_filter(df, column, operator, value):
    """条件过滤删除行"""
    df = df.copy()
    before = len(df)

    if column not in df.columns:
        raise ValueError(f"列 '{column}' 不存在")

    if operator == 'eq':
        mask = df[column].astype(str) == str(value)
    elif operator == 'neq':
        mask = df[column].astype(str) != str(value)
    elif operator == 'gt':
        mask = pd.to_numeric(df[column], errors='coerce') > float(value)
    elif operator == 'lt':
        mask = pd.to_numeric(df[column], errors='coerce') < float(value)
    elif operator == 'gte':
        mask = pd.to_numeric(df[column], errors='coerce') >= float(value)
    elif operator == 'lte':
        mask = pd.to_numeric(df[column], errors='coerce') <= float(value)
    elif operator == 'contains':
        mask = df[column].astype(str).str.contains(str(value), na=False)
    elif operator == 'null':
        mask = df[column].isna()
    elif operator == 'notnull':
        mask = df[column].notna()
    else:
        raise ValueError(f"不支持的操作符: {operator}")

    if operator in ('null', 'notnull'):
        df = df[mask]
    elif operator in ('eq', 'neq', 'contains'):
        df = df[mask]  # keep matching
    else:
        # 数值比较默认保留满足条件的行
        df = df[mask]

    return df, {
        "ok": True, "column": column,
        "rows_before": before, "rows_after": len(df),
        "rows_removed": before - len(df),
        "operator": operator, "value": str(value),
    }


# ==================== 缺失值热力图 ====================

def missing_heatmap(filepath, figsize=(12, 6)):
    """缺失值可视化热力图"""
    if not HAS_MPL:
        return {"error": "matplotlib 未安装"}, None

    df = pd.read_csv(filepath)

    # 每列缺失统计
    missing_pct = (df.isna().sum() / len(df) * 100).sort_values(ascending=False)
    missing_pct = missing_pct[missing_pct > 0]

    if missing_pct.empty:
        return {"missing_cols": [], "total_missing": 0, "n_rows": len(df),
                "n_cols": len(df.columns), "message": "数据无缺失值"}, None

    # 缺失模式矩阵采样
    sample_size = min(500, len(df))
    df_sample = df.sample(sample_size, random_state=42) if len(df) > sample_size else df
    missing_cols = missing_pct.index.tolist()

    fig, axes = plt.subplots(1, 2, figsize=figsize, gridspec_kw={'width_ratios': [1, 3]})
    fig.patch.set_facecolor('#0f111c')

    # 左：缺失百分比柱状图
    ax1 = axes[0]
    ax1.set_facecolor('#151725')
    colors_bar = ['#ef5350' if v > 30 else '#ff9800' if v > 10 else '#ffd54f' for v in missing_pct.values]
    ax1.barh(range(len(missing_pct)), missing_pct.values, color=colors_bar, alpha=0.8)
    ax1.set_yticks(range(len(missing_pct)))
    ax1.set_yticklabels([c[:15] for c in missing_pct.index], fontsize=8)
    ax1.set_xlabel('缺失 %', color='#a0a0b0', fontsize=9)
    ax1.set_title('缺失率', color='#e0e0e0', fontsize=11)
    ax1.tick_params(colors='#a0a0b0')
    ax1.invert_yaxis()

    # 右：缺失值矩阵热力图
    ax2 = axes[1]
    ax2.set_facecolor('#151725')
    miss_matrix = df_sample[missing_cols].isna()
    ax2.imshow(miss_matrix.T, aspect='auto', cmap='Reds',
               interpolation='nearest', vmin=0, vmax=1)
    ax2.set_xlabel(f'行 (采样 {sample_size})', color='#a0a0b0', fontsize=9)
    ax2.set_yticks(range(len(missing_cols)))
    ax2.set_yticklabels([c[:15] for c in missing_cols], fontsize=8)
    ax2.tick_params(colors='#a0a0b0')
    ax2.set_title('缺失模式矩阵 (红色=缺失)', color='#e0e0e0', fontsize=11)

    plt.tight_layout()
    chart = img_to_b64(fig)
    plt.close()

    return {
        "missing_cols": [{"col": c, "pct": round(float(v), 2), "count": int(df[c].isna().sum())}
                         for c, v in missing_pct.items()],
        "total_missing": int(df.isna().sum().sum()),
        "n_rows": len(df),
        "n_cols": len(df.columns),
    }, chart
