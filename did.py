#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
did.py — DID（双重差分）计量经济学分析模块

功能：
1. 数据清洗与描述统计
2. DID基准回归（双向固定效应）
3. 稳健性检验（平行趋势、安慰剂、替换变量、子样本）
4. 异质性分析（分区域、老龄化、城乡、收入）
5. 机制分析（中介效应、交互项）
6. 结果输出（stargazer格式表格、可视化）
"""

import os
import warnings
import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Tuple, Any

warnings.filterwarnings("ignore")

# 尝试导入 linearmodels
try:
    from linearmodels.panel import PanelOLS, PooledOLS, RandomEffects, FirstDifferenceOLS
    from linearmodels.iv import IV2SLS
    HAS_LINEARMODELS = True
except ImportError:
    HAS_LINEARMODELS = False


def _drop_collinear(exog: pd.DataFrame) -> pd.DataFrame:
    """
    剔除导致设计矩阵秩亏的列（常数/全零列、与其他列完全共线的列），
    保留第一列（const）。PanelOLS 双向固定效应在共线时拒绝估计
    （ValueError: exog does not have full column rank），这里在建模前做秩维护。
    """
    keep: List[str] = []
    for c in exog.columns:
        col = exog[c]
        # 常数/全零列：去均值后为零向量，必导致秩亏
        if col.nunique() <= 1:
            continue
        if not keep:
            keep.append(c)
            continue
        # 与已保留列完全共线（|corr|≈1）
        corr = exog[[c] + keep].corr().iloc[0, 1:]
        if (corr.abs() >= 0.9999).any():
            continue
        keep.append(c)
    return exog[keep]

# 尝试导入 statsmodels
try:
    import statsmodels.api as sm
    from statsmodels.regression.linear_model import OLS
    from statsmodels.tools import add_constant
    HAS_STATSMODELS = True
except ImportError:
    HAS_STATSMODELS = False


# ============================================================
# 1. 数据清洗
# ============================================================

def clean_data(df: pd.DataFrame, 
               consume_cols: List[str] = None,
               treat_col: str = 'treat',
               time_col: 'str' = 'year') -> Dict[str, Any]:
    """
    数据清洗：检查缺失值、异常值，处理极端值
    
    Parameters
    ----------
    df : DataFrame
    consume_cols : 消费占比列名列表（如 ['surv', 'dev', 'upgr']）
    treat_col : 处理组虚拟变量列名
    time_col : 时间列名
    
    Returns
    -------
    dict : {cleaned_df, report}
    """
    report = []
    df_clean = df.copy()
    
    # 1. 检查缺失值
    missing = df_clean.isnull().sum()
    missing_pct = (missing / len(df_clean) * 100).round(2)
    missing_report = pd.DataFrame({'缺失数': missing, '缺失率%': missing_pct})
    missing_report = missing_report[missing_report['缺失数'] > 0]
    if len(missing_report) > 0:
        report.append(f"⚠️ 发现缺失值:\n{missing_report.to_string()}")
    else:
        report.append("✅ 无缺失值")
    
    # 2. 检查消费占比之和是否≈100
    if consume_cols and all(c in df_clean.columns for c in consume_cols):
        df_clean['consume_sum'] = df_clean[consume_cols].sum(axis=1)
        invalid_sum = df_clean[(df_clean['consume_sum'] < 95) | (df_clean['consume_sum'] > 105)]
        if len(invalid_sum) > 0:
            report.append(f"⚠️ {len(invalid_sum)} 行消费占比之和不在95-105%范围内")
            # 归一化处理
            for col in consume_cols:
                df_clean[col] = df_clean[col] / df_clean['consume_sum'] * 100
            report.append("✅ 已归一化消费占比")
        else:
            report.append("✅ 消费占比之和均在合理范围")
        df_clean.drop(columns=['consume_sum'], inplace=True)
    
    # 3. 检查异常值（IQR方法）
    num_cols = df_clean.select_dtypes(include=[np.number]).columns
    outlier_count = 0
    for col in num_cols:
        q1 = df_clean[col].quantile(0.25)
        q3 = df_clean[col].quantile(0.75)
        iqr = q3 - q1
        lower = q1 - 3 * iqr
        upper = q3 + 3 * iqr
        outliers = df_clean[(df_clean[col] < lower) | (df_clean[col] > upper)]
        if len(outliers) > 0:
            outlier_count += len(outliers)
            # Winsorize 1% 和 99% 分位数
            p1 = df_clean[col].quantile(0.01)
            p99 = df_clean[col].quantile(0.99)
            df_clean[col] = df_clean[col].clip(p1, p99)
    
    if outlier_count > 0:
        report.append(f"⚠️ 处理了 {outlier_count} 个极端值（Winsorize 1%/99%）")
    else:
        report.append("✅ 无极端值")
    
    # 4. 检查处理组变量
    if treat_col in df_clean.columns:
        treat_vals = df_clean[treat_col].unique()
        report.append(f"✅ 处理组变量取值: {sorted(treat_vals)}")
    
    return {
        'cleaned_df': df_clean,
        'report': '\n'.join(report),
        'n_rows': len(df_clean),
        'n_cols': len(df_clean.columns)
    }


# ============================================================
# 2. 描述性统计
# ============================================================

def descriptive_stats(df: pd.DataFrame, 
                      group_col: str = None,
                      vars_list: List[str] = None) -> pd.DataFrame:
    """
    描述性统计表（论文Table 1格式）
    
    Parameters
    ----------
    df : DataFrame
    group_col : 分组列名（如treat）
    vars_list : 需要统计的变量列表
    
    Returns
    -------
    DataFrame : 描述统计表
    """
    if vars_list is None:
        vars_list = df.select_dtypes(include=[np.number]).columns.tolist()
    
    if group_col and group_col in df.columns:
        # 分组统计
        groups = df[group_col].unique()
        stats_list = []
        for g in sorted(groups):
            sub = df[df[group_col] == g]
            desc = sub[vars_list].describe().T
            desc['group'] = f'Group={g}'
            stats_list.append(desc)
        
        result = pd.concat(stats_list)
        result = result[['group', 'count', 'mean', 'std', 'min', 'max']]
    else:
        result = df[vars_list].describe().T
        result = result[['count', 'mean', 'std', 'min', 'max']]
    
    return result.round(4)


# ============================================================
# 3. 相关性矩阵
# ============================================================

def correlation_matrix(df: pd.DataFrame, 
                       vars_list: List[str] = None,
                       method: str = 'pearson') -> pd.DataFrame:
    """
    相关性矩阵
    
    Parameters
    ----------
    df : DataFrame
    vars_list : 变量列表
    method : pearson/spearman/kendall
    
    Returns
    -------
    DataFrame : 相关系数矩阵
    """
    if vars_list is None:
        vars_list = df.select_dtypes(include=[np.number]).columns.tolist()
    
    corr = df[vars_list].corr(method=method)
    return corr.round(4)


# ============================================================
# 4. DID 基准回归
# ============================================================

def did_regression(df: pd.DataFrame,
                   dep_var: str,
                   treat_col: str = 'treat',
                   time_col: str = 'year',
                   unit_col: str = 'province',
                   control_vars: List[str] = None,
                   cluster: bool = True,
                   post_col: str = None,
                   policy_year=None) -> Dict[str, Any]:
    """
    DID基准回归（双向固定效应）

    post 期判定优先级：显式 post_col > 数据自带 post 列 > policy_year 参数 >
    时间列中位数自动二分。核心估计量为 treat×post 交互项系数
    （双向固定效应下 treat/post 主效应分别被个体/时间效应吸收，属预期行为）。
    """
    if not HAS_LINEARMODELS:
        return {'error': 'linearmodels 未安装，请运行: pip install linearmodels'}

    # 准备数据（缺列时返回可读错误，而非裸 KeyError → 500）
    all_vars = [dep_var, treat_col, time_col, unit_col] + (control_vars or [])
    if post_col:
        all_vars.append(post_col)
    elif 'post' in df.columns and 'post' not in all_vars:
        all_vars.append('post')  # 数据自带 post 列：保留进子集，供 post 期判定使用
    missing = [c for c in dict.fromkeys(all_vars) if c not in df.columns]
    if missing:
        return {'error': f'数据缺少必需列: {", ".join(missing)}'
                         f'（当前数据共 {len(df.columns)} 列，请检查列名或换用面板数据）'}
    df_reg = df[all_vars].dropna().copy()
    
    # 确保时间列可用于面板索引：优先数值化，失败则解析为日期。
    # 注意：判据用「非数值且非日期」而非 dtype == object ——
    # pandas 新版会把字符串列推断为 str dtype（不再是 object），旧判据会漏掉。
    def _time_numeric(s: pd.Series) -> pd.Series:
        """时间列转数值（用于 post 期判定与大小比较）；日期型转纳秒时间戳"""
        if pd.api.types.is_datetime64_any_dtype(s):
            return s.astype('int64')
        return pd.to_numeric(s, errors='coerce')

    if not pd.api.types.is_numeric_dtype(df_reg[time_col]) and \
       not pd.api.types.is_datetime64_any_dtype(df_reg[time_col]):
        _num = pd.to_numeric(df_reg[time_col], errors='coerce')
        if _num.notna().any():
            df_reg[time_col] = _num
        else:
            _dt = pd.to_datetime(df_reg[time_col], errors='coerce')
            if _dt.notna().any():
                df_reg[time_col] = _dt
            else:
                return {'error': f'时间列「{time_col}」无法解析为数值或日期，'
                                 f'请检查格式（如 2020 或 2020-01-01）'}

    # 处理重复值：按个体-时间聚合（均值）
    if df_reg.duplicated(subset=[unit_col, time_col]).any():
        df_reg = df_reg.groupby([unit_col, time_col], as_index=False).mean()

    # ---- 构建 DID 变量（必须在 set_index 之前，时间列还需作为普通列使用）----
    # 1) post 期判定：显式列 > 数据自带 post > policy_year > 时间中位数
    if post_col:
        df_reg['_post'] = pd.to_numeric(df_reg[post_col], errors='coerce').fillna(0).astype(int)
    elif 'post' in df_reg.columns:
        df_reg['_post'] = pd.to_numeric(df_reg['post'], errors='coerce').fillna(0).astype(int)
    elif policy_year is not None:
        df_reg['_post'] = (_time_numeric(df_reg[time_col])
                           >= float(policy_year)).astype(int)
    else:
        cutoff = _time_numeric(df_reg[time_col]).median()
        df_reg['_post'] = (_time_numeric(df_reg[time_col]) >= cutoff).astype(int)

    # 2) DID 交互项 = treat × post（双向固定效应下的核心估计量）。
    #    只放 treat 主效应会被个体固定效应完全吸收（秩亏），原实现因此对任何
    #    标准面板数据都报 "All columns in exog have been fully absorbed"。
    df_reg['treat_x_post'] = (pd.to_numeric(df_reg[treat_col], errors='coerce')
                              * df_reg['_post']).astype(float)

    # 设置面板索引
    df_reg = df_reg.set_index([unit_col, time_col])

    # 构建公式
    exog_vars = ['treat_x_post']
    if control_vars:
        valid_ctrls = [c for c in control_vars if c in df_reg.columns and c != time_col and c != unit_col]
        exog_vars.extend(valid_ctrls)

    # 去重
    exog_vars = list(dict.fromkeys(exog_vars))
    
    exog = sm.add_constant(df_reg[exog_vars])
    endog = df_reg[dep_var]
    
    # 剔除常数/全零/完全共线列，避免 PanelOLS 秩亏报错
    exog = _drop_collinear(exog)
    
    # 双向固定效应（drop_absorbed: 被效应完全吸收的变量自动剔除，避免秩亏报错）
    model = PanelOLS(endog, exog, entity_effects=True, time_effects=True, drop_absorbed=True)
    
    try:
        if cluster:
            result = model.fit(cov_type='clustered', cluster_entity=True)
        else:
            result = model.fit()
    except Exception as e:
        # 如果聚类失败，尝试非聚类
        try:
            result = model.fit()
        except Exception as e2:
            return {'error': f'回归失败: {str(e2)}'}
    
    return {
        'model': result,
        'summary': str(result.summary),
        'params': result.params.to_dict(),
        'se': result.std_errors.to_dict(),
        'tvalues': result.tstats.to_dict(),
        'pvalues': result.pvalues.to_dict(),
        'r2': result.rsquared,
        'n_obs': result.nobs,
        'dep_var': dep_var,
        'treat_col': treat_col,
        'control_vars': control_vars or []
    }


# ============================================================
# 5. 逐步加入控制变量
# ============================================================

def stepwise_regression(df: pd.DataFrame,
                        dep_var: str,
                        treat_col: str = 'treat',
                        time_col: str = 'year',
                        unit_col: str = 'province',
                        control_groups: Dict[str, List[str]] = None) -> List[Dict]:
    """
    逐步加入控制变量
    
    Parameters
    ----------
    control_groups : 分组控制变量，如 {'人口结构': ['oldpop', 'urban'], '社会经济': ['lninc', 'gdp']}
    
    Returns
    -------
    list : 各模型结果列表
    """
    if control_groups is None:
        control_groups = {}
    
    results = []
    
    # 模型1：仅政策变量
    res1 = did_regression(df, dep_var, treat_col, time_col, unit_col, control_vars=None)
    res1['model_name'] = '模型1: 仅政策变量'
    results.append(res1)
    
    # 逐步加入控制变量
    current_controls = []
    for group_name, vars_list in control_groups.items():
        # 只添加存在的列
        valid_vars = [v for v in vars_list if v in df.columns]
        current_controls.extend(valid_vars)
        
        res = did_regression(df, dep_var, treat_col, time_col, unit_col, control_vars=current_controls)
        res['model_name'] = f'模型{len(results)+1}: +{group_name}'
        results.append(res)
    
    return results


# ============================================================
# 6. 标准化系数回归
# ============================================================

def standardized_regression(df: pd.DataFrame,
                            dep_var: str,
                            treat_col: str = 'treat',
                            time_col: str = 'year',
                            unit_col: str = 'province',
                            control_vars: List[str] = None) -> Dict[str, Any]:
    """
    标准化系数回归（beta系数）
    """
    if not HAS_LINEARMODELS:
        return {'error': 'linearmodels 未安装'}
    
    # 标准化连续变量
    df_std = df.copy()
    num_cols = [dep_var, treat_col] + (control_vars or [])
    num_cols = [c for c in num_cols if c in df_std.columns]
    
    for col in num_cols:
        if df_std[col].std() > 0:
            df_std[col] = (df_std[col] - df_std[col].mean()) / df_std[col].std()
    
    return did_regression(df_std, dep_var, treat_col, time_col, unit_col, control_vars)


# ============================================================
# 7. 平行趋势检验（Event Study）
# ============================================================

def parallel_trends(df: pd.DataFrame,
                    dep_var: str,
                    treat_col: str = 'treat',
                    time_col: str = 'year',
                    unit_col: str = 'province',
                    control_vars: List[str] = None,
                    pre_periods: int = 5,
                    post_periods: int = 5) -> Dict[str, Any]:
    """
    平行趋势检验（Event Study）
    
    Parameters
    ----------
    pre_periods : 政策前时期数
    post_periods : 政策后时期数
    
    Returns
    -------
    dict : {coef_df, fig_data, passed}
    """
    if not HAS_STATSMODELS:
        return {'error': 'statsmodels 未安装'}
    
    df_es = df.copy()

    # 时间列稳健化：字符串日期 → 时间戳，并映射为连续期数（0,1,2,...），
    # 避免「字符串日期无法做减法」以及不同时间尺度（年 vs 纳秒）混用
    _times = df_es[time_col]
    _tnum = pd.to_numeric(_times, errors='coerce')
    if not _tnum.notna().any():
        _tnum = pd.to_datetime(_times, errors='coerce').astype('int64')
    df_es['_t_num'] = _tnum
    _uniq = sorted(df_es['_t_num'].dropna().unique())
    if not _uniq:
        return {'error': f'时间列「{time_col}」无法解析为数值或日期，请检查格式'}
    _map = {v: i for i, v in enumerate(_uniq)}
    df_es['_t_period'] = df_es['_t_num'].map(_map)

    # 找到政策实施期（treat 首次为 1 的期数）
    _treated = df_es[df_es[treat_col] == 1]
    if _treated.empty:
        _vals = list(pd.unique(df_es[treat_col]))[:5]
        return {'error': f'无法确定政策时点：列「{treat_col}」中不存在取值为 1 的处理组记录。'
                         f'请确认处理组标记为 0/1（当前取值示例：{_vals}）'}
    policy_period = _treated['_t_period'].min()
    policy_year = policy_period  # 兼容原名，语义为政策所在期

    # 创建相对时间变量（相对政策期的期数偏移）
    df_es['relative_time'] = df_es['_t_period'] - policy_period
    
    # 创建事件研究虚拟变量
    coef_data = []
    
    for t in range(-pre_periods, post_periods + 1):
        if t == -1:  # 基准期
            continue
        col_name = f'event_{t}' if t >= 0 else f'event_m{abs(t)}'
        df_es[col_name] = ((df_es['relative_time'] == t) & (df_es[treat_col] == 1)).astype(int)
    
    # 构建回归
    event_cols = [c for c in df_es.columns if c.startswith('event_')]
    
    if not event_cols:
        return {'error': '无法创建事件研究变量'}
    
    # 使用面板固定效应
    # 时间维必须是数值或日期型：字符串列（pandas 新版为 str dtype）需先数值/时间戳化，
    # 否则 linearmodels 抛 "index on the time dimension must be numeric or date-like"
    df_es[time_col] = df_es['_t_num']
    df_reg = df_es.set_index([unit_col, time_col])
    exog_vars = event_cols
    if control_vars:
        valid_ctrls = [c for c in control_vars if c in df_reg.columns]
        exog_vars.extend(valid_ctrls)
    
    exog = sm.add_constant(df_reg[exog_vars])
    endog = df_reg[dep_var]
    
    # 剔除常数/全零/完全共线列（如无处理样本的事件期虚拟变量），避免秩亏报错
    exog = _drop_collinear(exog)
    
    model = PanelOLS(endog, exog, entity_effects=True, time_effects=True, drop_absorbed=True)
    result = model.fit(cov_type='clustered', cluster_entity=True)
    
    # 提取系数和置信区间
    coef_df = pd.DataFrame({
        'time': list(range(-pre_periods, post_periods + 1)),
        'coef': np.nan,
        'se': np.nan,
        't_stat': np.nan,
        'p_value': np.nan,
        'ci_lower': np.nan,
        'ci_upper': np.nan
    })
    
    for i, t in enumerate(range(-pre_periods, post_periods + 1)):
        if t == -1:
            coef_df.loc[i, 'coef'] = 0  # 基准期
            coef_df.loc[i, 'ci_lower'] = 0
            coef_df.loc[i, 'ci_upper'] = 0
            continue
        
        col_name = f'event_{t}' if t >= 0 else f'event_m{abs(t)}'
        if col_name in result.params.index:
            coef_df.loc[i, 'coef'] = result.params[col_name]
            coef_df.loc[i, 'se'] = result.std_errors[col_name]
            coef_df.loc[i, 't_stat'] = result.tstats[col_name]
            coef_df.loc[i, 'p_value'] = result.pvalues[col_name]
            ci = result.conf_int().loc[col_name]
            coef_df.loc[i, 'ci_lower'] = ci.iloc[0]
            coef_df.loc[i, 'ci_upper'] = ci.iloc[1]
    
    # 判断平行趋势：政策前系数不显著（bool() 转 Python bool，避免 numpy.bool_ 无法 JSON 序列化）
    pre_coefs = coef_df[coef_df['time'] < 0]
    passed = bool((pre_coefs['p_value'] > 0.1).all()) if len(pre_coefs) > 0 else False

    payload = {
        'coef_df': coef_df,
        'policy_year': policy_year,
        'passed': passed,
        'model': result,
        'summary': str(result.summary)
    }

    # 数据期数不足时事件虚拟变量会被整体吸收 → 系数全为 NaN。
    # 给出可操作提示，而不是让用户看到一张空图。
    if coef_df['coef'].isna().all():
        _n_periods = int(df_es['_t_period'].nunique())
        payload['warning'] = (
            f'未估计出有效事件期系数：数据共 {_n_periods} 个时间期，'
            f'而检验按 pre_periods={pre_periods}、post_periods={post_periods} 构造事件窗口。'
            f'请减小 pre_periods / post_periods（例如各设为 2），或使用时间跨度更长的面板数据。'
        )
    return payload


# ============================================================
# 8. 安慰剂检验
# ============================================================

def _eff_coef(result: Dict[str, Any], treat_col: str) -> float:
    """从 DID 回归结果中提取处理效应系数。

    did_regression 的交互项键名固定为 `treat_x_post`（`treat:post`），
    历史实现曾用 `treat_col` 作为键，这里兼容两者以免取到 0。"""
    params = result.get('params') or {}
    if 'treat_x_post' in params:
        return float(params.get('treat_x_post', 0.0))
    return float(params.get(treat_col, 0.0))


def placebo_test(df: pd.DataFrame,
                 dep_var: str,
                 treat_col: str = 'treat',
                 time_col: str = 'year',
                 unit_col: str = 'province',
                 control_vars: List[str] = None,
                 n_placebo: int = 500,
                 seed: int = 42) -> Dict[str, Any]:
    """
    安慰剂检验
    
    Parameters
    ----------
    n_placebo : 随机次数
    seed : 随机种子
    
    Returns
    -------
    dict : {coef_list, p_value, passed, fig_data}
    """
    np.random.seed(seed)
    
    # 真实DID估计
    true_result = did_regression(df, dep_var, treat_col, time_col, unit_col, control_vars)
    true_coef = _eff_coef(true_result, treat_col)
    
    # 随机分配处理组
    placebo_coefs = []
    n_treat = df[treat_col].sum()
    
    for _ in range(n_placebo):
        df_placebo = df.copy()
        # 随机选择处理组
        treat_idx = np.random.choice(df_placebo.index, size=int(n_treat), replace=False)
        df_placebo[treat_col] = 0
        df_placebo.loc[treat_idx, treat_col] = 1
        
        try:
            res = did_regression(df_placebo, dep_var, treat_col, time_col, unit_col, control_vars)
            placebo_coefs.append(_eff_coef(res, treat_col))
        except Exception:
            # 单次安慰剂回归失败时跳过该轮（常见于该轮样本无法收敛）
            continue
    
    # 计算p值：真实系数大于安慰剂系数的比例
    placebo_coefs = np.array(placebo_coefs)
    if true_coef > 0:
        p_value = (placebo_coefs >= true_coef).mean()
    else:
        p_value = (placebo_coefs <= true_coef).mean()
    
    passed = bool(p_value < 0.05)
    
    return {
        'true_coef': true_coef,
        'placebo_coefs': placebo_coefs.tolist(),
        'p_value': p_value,
        'passed': passed,
        'n_placebo': n_placebo,
        'n_success': len(placebo_coefs)
    }


# ============================================================
# 9. 子样本回归
# ============================================================

def subsample_regression(df: pd.DataFrame,
                         dep_var: str,
                         treat_col: str = 'treat',
                         time_col: str = 'year',
                         unit_col: str = 'province',
                         control_vars: List[str] = None,
                         exclude_units: List[str] = None,
                         include_units: List[str] = None) -> Dict[str, Any]:
    """
    子样本回归
    
    Parameters
    ----------
    exclude_units : 排除的个体列表
    include_units : 仅包含的个体列表
    
    Returns
    -------
    dict : 回归结果
    """
    if exclude_units:
        df_sub = df[~df[unit_col].isin(exclude_units)]
    elif include_units:
        df_sub = df[df[unit_col].isin(include_units)]
    else:
        df_sub = df.copy()
    
    result = did_regression(df_sub, dep_var, treat_col, time_col, unit_col, control_vars)
    result['n_obs'] = len(df_sub)
    result['n_units'] = df_sub[unit_col].nunique()
    
    return result


# ============================================================
# 10. 异质性分析
# ======================================================================

def heterogeneity_analysis(df: pd.DataFrame,
                           dep_var: str,
                           treat_col: str = 'treat',
                           time_col: str = 'year',
                           unit_col: str = 'province',
                           control_vars: List[str] = None,
                           hetero_var: str = None,
                           split_method: str = 'median') -> Dict[str, Any]:
    """
    异质性分析
    
    Parameters
    ----------
    hetero_var : 异质性变量
    split_method : median/mean/tertile
    
    Returns
    -------
    dict : {high_group, low_group, diff_test}
    """
    if hetero_var not in df.columns:
        return {'error': f'变量 {hetero_var} 不存在'}
    
    # 计算分组阈值
    if split_method == 'median':
        threshold = df[hetero_var].median()
    elif split_method == 'mean':
        threshold = df[hetero_var].mean()
    elif split_method == 'tertile':
        threshold = df[hetero_var].quantile(0.5)
    else:
        threshold = df[hetero_var].median()
    
    # 分组
    df_high = df[df[hetero_var] >= threshold]
    df_low = df[df[hetero_var] < threshold]
    
    # 分别回归
    res_high = did_regression(df_high, dep_var, treat_col, time_col, unit_col, control_vars)
    res_low = did_regression(df_low, dep_var, treat_col, time_col, unit_col, control_vars)
    
    return {
        'hetero_var': hetero_var,
        'threshold': threshold,
        'high_group': res_high,
        'low_group': res_low,
        'n_high': len(df_high),
        'n_low': len(df_low)
    }


# ============================================================
# 11. 机制分析（中介效应）
# ============================================================

def mediation_analysis(df: pd.DataFrame,
                       dep_var: str,
                       mediator: str,
                       treat_col: str = 'treat',
                       time_col: str = 'year',
                       unit_col: str = 'province',
                       control_vars: List[str] = None) -> Dict[str, Any]:
    """
    中介效应分析（三步法）
    
    Parameters
    ----------
    mediator : 中介变量
    
    Returns
    -------
    dict : {step1, step2, step3, indirect_effect, direct_effect, mediation_ratio}
    """
    if not HAS_LINEARMODELS:
        return {'error': 'linearmodels 未安装'}
    
    # Step 1: Y = c*Treat + controls
    step1 = did_regression(df, dep_var, treat_col, time_col, unit_col, control_vars)
    
    # Step 2: M = a*Treat + controls
    step2 = did_regression(df, mediator, treat_col, time_col, unit_col, control_vars)
    
    # Step 3: Y = c'*Treat + b*M + controls
    ctrl_with_mediator = (control_vars or []) + [mediator]
    step3 = did_regression(df, dep_var, treat_col, time_col, unit_col, ctrl_with_mediator)
    
    # 计算间接效应和直接效应
    a = step2['params'].get(treat_col, 0)  # Treat -> M
    b = step3['params'].get(mediator, 0)   # M -> Y
    c = step1['params'].get(treat_col, 0)  # Treat -> Y (总效应)
    c_prime = step3['params'].get(treat_col, 0)  # Treat -> Y (直接效应)
    
    indirect_effect = a * b
    direct_effect = c_prime
    mediation_ratio = indirect_effect / c if c != 0 else 0
    
    return {
        'step1': step1,
        'step2': step2,
        'step3': step3,
        'a_coefficient': a,
        'b_coefficient': b,
        'total_effect': c,
        'direct_effect': direct_effect,
        'indirect_effect': indirect_effect,
        'mediation_ratio': mediation_ratio,
        'mediator': mediator
    }


# ============================================================
# 12. 交互项分析
# ============================================================

def interaction_analysis(df: pd.DataFrame,
                         dep_var: str,
                         treat_col: str = 'treat',
                         time_col: str = 'year',
                         unit_col: str = 'province',
                         control_vars: List[str] = None,
                         interact_var: str = None) -> Dict[str, Any]:
    """
    交互项分析（调节效应）
    
    Parameters
    ----------
    interact_var : 调节变量
    
    Returns
    -------
    dict : 回归结果
    """
    if interact_var not in df.columns:
        return {'error': f'变量 {interact_var} 不存在'}
    
    # 创建交互项
    df_inter = df.copy()
    interact_col = f'{treat_col}_x_{interact_var}'
    df_inter[interact_col] = df_inter[treat_col] * df_inter[interact_var]
    
    # 回归
    ctrl_with_inter = (control_vars or []) + [interact_col]
    result = did_regression(df_inter, dep_var, treat_col, time_col, unit_col, ctrl_with_inter)
    result['interact_col'] = interact_col
    result['interact_var'] = interact_var
    
    return result


# ============================================================
# 13. 结果输出（stargazer格式）
# ============================================================

def format_regression_table(results: List[Dict],
                            title: str = "回归结果",
                            dep_var_label: str = "被解释变量") -> str:
    """
    格式化回归结果表格（类似stargazer）
    
    Parameters
    ----------
    results : 回归结果列表
    title : 表格标题
    
    Returns
    -------
    str : HTML表格
    """
    if not results:
        return ""
    
    # 收集所有变量
    all_vars = set()
    for res in results:
        if 'params' in res:
            all_vars.update(res['params'].keys())
    
    all_vars = sorted(all_vars)
    
    # 构建HTML表格
    html = f'<div style="padding:16px;"><h4>{title}</h4>'
    html += '<table style="width:100%;border-collapse:collapse;font-size:12px;">'
    
    # 表头
    html += '<tr style="border-bottom:2px solid #333;">'
    html += '<th style="padding:8px;text-align:left;">变量</th>'
    for i, res in enumerate(results):
        model_name = res.get('model_name', f'Model {i+1}')
        html += f'<th style="padding:8px;text-align:center;">{model_name}</th>'
    html += '</tr>'
    
    # 变量行
    for var in all_vars:
        html += f'<tr style="border-bottom:1px solid #eee;">'
        html += f'<td style="padding:6px 8px;font-weight:600;">{var}</td>'
        
        for res in results:
            if 'params' not in res:
                html += '<td style="padding:6px 8px;text-align:center;">-</td>'
                continue
            
            coef = res['params'].get(var, np.nan)
            se = res['se'].get(var, np.nan)
            pval = res['pvalues'].get(var, np.nan)
            
            if np.isnan(coef):
                html += '<td style="padding:6px 8px;text-align:center;">-</td>'
            else:
                # 显著性标记
                sig = ''
                if not np.isnan(pval):
                    if pval < 0.01:
                        sig = '***'
                    elif pval < 0.05:
                        sig = '**'
                    elif pval < 0.1:
                        sig = '*'
                
                html += f'<td style="padding:6px 8px;text-align:center;">{coef:.4f}{sig}<br/><span style="color:#666;font-size:10px;">({se:.4f})</span></td>'
        
        html += '</tr>'
    
    # 观测数和R²
    html += '<tr style="border-top:2px solid #333;">'
    html += '<td style="padding:6px 8px;font-weight:600;">观测数</td>'
    for res in results:
        n = res.get('n_obs', '-')
        html += f'<td style="padding:6px 8px;text-align:center;">{n}</td>'
    html += '</tr>'
    
    html += '<tr>'
    html += '<td style="padding:6px 8px;font-weight:600;">R²</td>'
    for res in results:
        r2 = res.get('r2', '-')
        if isinstance(r2, (int, float)):
            html += f'<td style="padding:6px 8px;text-align:center;">{r2:.4f}</td>'
        else:
            html += f'<td style="padding:6px 8px;text-align:center;">{r2}</td>'
    html += '</tr>'
    
    html += '</table>'
    html += '<p style="font-size:10px;color:#666;margin-top:8px;">注：括号内为标准误，*** p<0.01, ** p<0.05, * p<0.1</p>'
    html += '</div>'
    
    return html


# ============================================================
# 14. 时间趋势图数据
# ============================================================

def time_trend_data(df: pd.DataFrame,
                    y_var: str,
                    time_col: str = 'year',
                    group_col: str = 'treat',
                    agg_func: str = 'mean') -> pd.DataFrame:
    """
    时间趋势图数据
    
    Parameters
    ----------
    y_var : Y轴变量
    time_col : 时间列
    group_col : 分组列
    agg_func : 聚合函数
    
    Returns
    -------
    DataFrame : 趋势数据
    """
    trend = df.groupby([time_col, group_col])[y_var].agg(agg_func).reset_index()
    trend = trend.pivot(index=time_col, columns=group_col, values=y_var)
    trend.columns = [f'Group={c}' for c in trend.columns]
    trend = trend.reset_index()
    return trend


# ============================================================
# 15. 综合DID分析（一键运行）
# ============================================================

def run_full_did_analysis(df: pd.DataFrame,
                          dep_var: str,
                          treat_col: str = 'treat',
                          time_col: str = 'year',
                          unit_col: str = 'province',
                          control_vars: List[str] = None,
                          consume_cols: List[str] = None) -> Dict[str, Any]:
    """
    运行完整的DID分析流程
    
    Returns
    -------
    dict : 所有分析结果
    """
    results = {}
    
    # 1. 数据清洗
    print("Step 1: 数据清洗...")
    clean_result = clean_data(df, consume_cols, treat_col, time_col)
    results['cleaning'] = clean_result
    
    df_clean = clean_result['cleaned_df']
    
    # 2. 描述性统计
    print("Step 2: 描述性统计...")
    num_vars = df_clean.select_dtypes(include=[np.number]).columns.tolist()
    results['descriptive'] = descriptive_stats(df_clean, treat_col, num_vars)
    
    # 3. 相关性矩阵
    print("Step 3: 相关性矩阵...")
    results['correlation'] = correlation_matrix(df_clean, num_vars)
    
    # 4. 基准回归
    print("Step 4: 基准回归...")
    results['baseline'] = did_regression(df_clean, dep_var, treat_col, time_col, unit_col, control_vars)
    
    # 5. 平行趋势检验
    print("Step 5: 平行趋势检验...")
    results['parallel_trends'] = parallel_trends(df_clean, dep_var, treat_col, time_col, unit_col, control_vars)
    
    # 6. 安慰剂检验
    print("Step 6: 安慰剂检验...")
    results['placebo'] = placebo_test(df_clean, dep_var, treat_col, time_col, unit_col, control_vars, n_placebo=200)
    
    return results


# ============================================================
# 辅助函数
# ============================================================

# 报告 HTML 构建统一复用 report_helpers（增强超集实现）
# 保持 _build_* 函数名以兼容 did_routes 的现有引用
from report_helpers import (
    build_autonomous_report_html as _build_autonomous_report_html,
    build_analysis_html as _build_analysis_html,
    build_correlation_html as _build_correlation_html,
    build_group_html as _build_group_html,
)


if __name__ == '__main__':
    # 测试
    print("DID模块加载成功")
    print(f"linearmodels: {'可用' if HAS_LINEARMODELS else '不可用'}")
    print(f"statsmodels: {'可用' if HAS_STATSMODELS else '不可用'}")
