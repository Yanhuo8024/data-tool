#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""  table.tools    / JOIN /  /  /  """
import pandas as pd
import numpy as np
import json
import re
from io import BytesIO
import base64

# ========================================
# 1.  JOIN
# ========================================
def multi_table_join(file1, file2, left_keys, right_keys, join_type='inner'):
    """ join """
    df1 = _read_file(file1)
    df2 = _read_file(file2)
    
    if left_keys is None or right_keys is None:
        left_keys = _find_common_cols(df1, df2)
        right_keys = left_keys
    
    result = pd.merge(df1, df2, left_on=left_keys, right_on=right_keys, how=join_type)
    
    return {
        'rows': len(result),
        'cols': len(result.columns),
        'columns': list(result.columns),
        'preview': _df_to_preview(result),
        'stats': {
            'df1_rows': len(df1), 'df2_rows': len(df2),
            'matched': len(result),
            'unmatched_df1': len(df1) - len(result[result.index.isin(df1.index)]),
        }
    }

# ========================================
# 2.  ()
# ========================================

FINANCE_FORMULAS = {
    'pct_change': {'label': '(pct_change)', 'expr': '{col}.pct_change()*100', 'params': ['col']},
    'log_return': {'label': '(log_return)', 'expr': 'np.log({col}/{col}.shift(1))*100', 'params': ['col']},
    'amplitude': {'label': '(amplitude)', 'expr': '({high}-{low})/{close}*100', 'params': ['high','low','close']},
    'ma': {'label': 'MA', 'expr': '{col}.rolling({window}).mean()', 'params': ['col','window']},
    'ema': {'label': 'EMA', 'expr': '{col}.ewm(span={window}).mean()', 'params': ['col','window']},
    'volatility': {'label': '', 'expr': '{col}.rolling({window}).std()', 'params': ['col','window']},
    'cumsum': {'label': '', 'expr': '{col}.cumsum()', 'params': ['col']},
    'diff': {'label': '', 'expr': '{col}.diff({n})', 'params': ['col','n']},
    'rank': {'label': '', 'expr': '{col}.rank(pct=True)*100', 'params': ['col']},
}

def apply_formula(df, formula_expr, new_col_name):
    """   """
    df = df.copy()
    local_vars = {'pd': pd, 'np': np, 'df': df}
    for col in df.columns:
        local_vars[col] = df[col].values
    
    try:
        expr = formula_expr
        for col in df.select_dtypes(include=np.number).columns:
            expr = expr.replace(f'{{{col}}}', f"df['{col}']")
        result = eval(expr, {"__builtins__": {}}, {**local_vars, 'pd': pd, 'np': np, 'df': df})
        df[new_col_name] = result
        return df
    except Exception as e:
        raise ValueError(f" {new_col_name}: {str(e)}")

# ========================================
# 3.  Pivot
# ========================================
def create_pivot(df, index_cols, columns_col=None, values_col=None, agg_func='mean', normalize=False):
    """  """
    if values_col is None:
        numeric_cols = df.select_dtypes(include=np.number).columns.tolist()
        values_col = numeric_cols[0] if numeric_cols else None
    
    if values_col is None:
        raise ValueError("  ")
    
    if columns_col and columns_col.strip():
        result = pd.pivot_table(df, index=index_cols, columns=columns_col, 
                               values=values_col, aggfunc=agg_func, fill_value=0)
    else:
        group = df.groupby(index_cols)[values_col].agg(agg_func)
        result = group.reset_index()
    
    if normalize and isinstance(result, pd.DataFrame):
        if columns_col:
            result = result.div(result.sum(axis=1), axis=0) * 100
    
    return {
        'rows': len(result), 'cols': len(result.columns),
        'preview': _df_to_preview(result.head(20)),
        'columns': [str(c) for c in result.columns],
    }

def create_crosstab(df, row_col, col_col, values_col=None, agg_func='count', normalize=False):
    """  """
    if values_col:
        result = pd.crosstab(df[row_col], df[col_col], values=df[values_col], 
                            aggfunc=agg_func, normalize=normalize)
    else:
        result = pd.crosstab(df[row_col], df[col_col], normalize=normalize)
    
    return {
        'rows': len(result), 'cols': len(result.columns),
        'preview': _df_to_preview(result.head(20)),
        'columns': [str(c) for c in result.columns],
    }

# ========================================
# 4.  
# ========================================
def random_sample(df, n=None, frac=None, random_state=42):
    """  /  """
    if n:
        result = df.sample(n=min(n, len(df)), random_state=random_state)
    elif frac:
        result = df.sample(frac=min(frac, 1.0), random_state=random_state)
    else:
        result = df.sample(n=min(100, len(df)), random_state=random_state)
    return {'sampled_rows': len(result), 'total_rows': len(df), 'preview': _df_to_preview(result)}

def stratified_sample(df, stratify_col, n=None, frac=None, random_state=42):
    """  """
    result = df.groupby(stratify_col, group_keys=False).apply(
        lambda g: g.sample(n=min(n or len(g), len(g)), 
                          frac=min(frac or 1.0, 1.0), 
                          random_state=random_state)
    ).reset_index(drop=True)
    return {'sampled_rows': len(result), 'total_rows': len(df), 'preview': _df_to_preview(result)}

def time_segment_sample(df, n_segments=10, sample_per_segment=5):
    """  """
    segment_size = len(df) // n_segments
    indices = []
    for i in range(n_segments):
        start = i * segment_size
        end = start + segment_size if i < n_segments - 1 else len(df)
        seg_indices = np.random.choice(range(start, end), 
                                       size=min(sample_per_segment, end-start), 
                                       replace=False)
        indices.extend(seg_indices)
    result = df.iloc[sorted(indices)]
    return {'sampled_rows': len(result), 'total_rows': len(df), 'preview': _df_to_preview(result)}

# ========================================
#  
# ========================================
def _read_file(filepath):
    if filepath.endswith('.csv'):
        return pd.read_csv(filepath)
    elif filepath.endswith(('.xlsx', '.xls')):
        return pd.read_excel(filepath)
    raise ValueError(f" {filepath}")

def _find_common_cols(df1, df2):
    return list(set(df1.columns) & set(df2.columns))

def _df_to_preview(df, n=10):
    return df.head(n).fillna('').astype(str).to_dict(orient='records')

def get_file_columns(filepath):
    """  """
    df = _read_file(filepath)
    return {
        'columns': list(df.columns),
        'numeric': df.select_dtypes(include=np.number).columns.tolist(),
        'categorical': df.select_dtypes(include=['object','category']).columns.tolist(),
        'rows': len(df), 'cols': len(df.columns),
    }
