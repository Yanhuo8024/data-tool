#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""  timeseries.tools    /  / Prophet /  """
import pandas as pd
import numpy as np
from io import BytesIO
import base64
from pathlib import Path
import os

try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'Arial Unicode MS', 'DejaVu Sans']
    plt.rcParams['axes.unicode_minus'] = False
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False

# ========================================
# 1. 交易日历（动态生成）
# ========================================
# 规则：周一~周五为基础交易日，再扣除法定节假日。
# 节假日按年维护（春节等浮动假日逐年更新）；未收录年份退化为"工作日=交易日"近似。
_CN_HOLIDAYS = {
    # 2025 年法定休市日（含春节调休后的实际闭市日）
    2025: set([
        '2025-01-01',
        '2025-01-28', '2025-01-29', '2025-01-30', '2025-01-31',
        '2025-02-01', '2025-02-02', '2025-02-03', '2025-02-04',
        '2025-04-04', '2025-04-05', '2025-04-06',
        '2025-05-01', '2025-05-02', '2025-05-03', '2025-05-04', '2025-05-05',
        '2025-05-31', '2025-06-01', '2025-06-02',
        '2025-10-01', '2025-10-02', '2025-10-03', '2025-10-04',
        '2025-10-05', '2025-10-06', '2025-10-07', '2025-10-08',
    ]),
    # 2026 年法定休市日（按国务院办公厅公布的休假安排）
    2026: set([
        '2026-01-01', '2026-01-02', '2026-01-03',
        '2026-02-15', '2026-02-16', '2026-02-17', '2026-02-18',
        '2026-02-19', '2026-02-20', '2026-02-21',
        '2026-04-04', '2026-04-05', '2026-04-06',
        '2026-05-01', '2026-05-02', '2026-05-03',
        '2026-06-19', '2026-06-20', '2026-06-21',
        '2026-09-25', '2026-09-26', '2026-09-27',
        '2026-10-01', '2026-10-02', '2026-10-03', '2026-10-04',
        '2026-10-05', '2026-10-06', '2026-10-07',
    ]),
}


def get_trading_days(year=None):
    """返回指定年份的交易日集合（工作日 − 法定节假日）。
    未收录节假日表的年份退化为全年工作日近似。"""
    if year is None:
        year = pd.Timestamp.today().year
    holidays = _CN_HOLIDAYS.get(year, set())
    days = pd.date_range(f'{year}-01-01', f'{year}-12-31', freq='D')
    return {d.strftime('%Y-%m-%d') for d in days
            if d.weekday() < 5 and d.strftime('%Y-%m-%d') not in holidays}


# 兼容旧引用：预计算 2025/2026 两年交易日
TRADING_DAYS_2025 = get_trading_days(2025) | get_trading_days(2026)

def extract_date_features(df, date_col):
    """  """
    df = df.copy()
    df[date_col] = pd.to_datetime(df[date_col], errors='coerce')
    df['dt_year'] = df[date_col].dt.year
    df['dt_quarter'] = df[date_col].dt.quarter
    df['dt_month'] = df[date_col].dt.month
    df['dt_week'] = df[date_col].dt.isocalendar().week.astype(int)
    df['dt_day'] = df[date_col].dt.day
    df['dt_weekday'] = df[date_col].dt.weekday
    df['dt_weekday_name'] = df[date_col].dt.day_name()
    df['dt_is_weekend'] = df[date_col].dt.weekday >= 5
    df['dt_date_str'] = df[date_col].dt.strftime('%Y-%m-%d')
    # 动态交易日判定：按数据所在年份分别匹配
    trading = set()
    for y in df[date_col].dt.year.dropna().unique():
        trading |= get_trading_days(int(y))
    df['dt_is_trading_day'] = df['dt_date_str'].isin(trading)
    df['dt_year_month'] = df[date_col].dt.strftime('%Y-%m')
    
    return {
        'new_columns': ['dt_year','dt_quarter','dt_month','dt_week','dt_day',
                       'dt_weekday','dt_weekday_name','dt_is_weekend',
                       'dt_is_trading_day','dt_year_month'],
        'preview': df.head(10).fillna('').astype(str).to_dict(orient='records'),
        'total_rows': len(df),
    }

# ========================================
# 2.  
# ========================================
def resample_timeseries(df, date_col, value_col, freq='D', agg_func='mean'):
    """  """
    df = df.copy()
    df[date_col] = pd.to_datetime(df[date_col])
    df = df.set_index(date_col)
    
    agg_map = {'mean': 'mean', 'sum': 'sum', 'max': 'max', 'min': 'min',
               'first': 'first', 'last': 'last', 'std': 'std', 'count': 'count'}
    
    if agg_func not in agg_map:
        agg_func = 'mean'
    
    result = df[value_col].resample(freq).agg(agg_map.get(agg_func, 'mean'))
    result = result.reset_index()
    result.columns = [date_col, f'{value_col}_{agg_func}']
    
    return {
        'rows': len(result),
        'preview': result.head(20).fillna('').astype(str).to_dict(orient='records'),
        'freq': freq, 'agg_func': agg_func,
    }

# ========================================
# 3. Prophet 
# ========================================
def prophet_forecast(df, date_col, value_col, periods=30):
    """Prophet 时序预测（含趋势/季节分解与预测区间）

    依赖：prophet + 已编译的 Stan 后端（cmdstan）。
    cmdstan 缺失时给出可操作提示并建议改用 ARIMA，而不是抛出难懂的 Stan 解析错误。
    """
    try:
        from prophet import Prophet
    except ImportError:
        return {'ok': False,
                'error': 'prophet 未安装（请执行 pip install prophet）',
                'hint': '可改用 ARIMA（/api/ts/arima）完成同类时序预测'}

    # Stan 后端可用性预检：cmdstan 未安装/未编译时，拟合会报晦涩的 "parsing Stan csv" 错误
    try:
        import cmdstanpy
        cmdstanpy.cmdstan_path()
    except Exception:
        return {'ok': False,
                'error': 'Prophet 的 Stan 计算后端（cmdstan）未安装或不可用，无法拟合模型',
                'hint': '请在可联网环境执行 `python -m cmdstanpy.install_cmdstan` 完成安装；'
                        '或直接改用 ARIMA（/api/ts/arima）进行同类时序预测'}

    df = df.copy()
    df[date_col] = pd.to_datetime(df[date_col])

    prophet_df = pd.DataFrame({'ds': df[date_col], 'y': df[value_col]})
    prophet_df = prophet_df.dropna()

    if len(prophet_df) < 10:
        return {'ok': False, 'error': f'数据量不足（{len(prophet_df)} 行），Prophet 至少需要 10 个观测值'}

    m = Prophet(yearly_seasonality=True, weekly_seasonality=True, daily_seasonality=False)
    try:
        m.fit(prophet_df)
    except Exception as e:
        _msg = str(e)
        if 'Stan' in _msg or 'cmdstan' in _msg.lower() or 'csv' in _msg.lower():
            return {'ok': False,
                    'error': 'Prophet 拟合失败：Stan 后端不可用或数据格式不适配',
                    'hint': '确认 cmdstan 已安装（python -m cmdstanpy.install_cmdstan）；'
                            '或改用 ARIMA（/api/ts/arima）'}
        return {'ok': False, 'error': f'Prophet 拟合失败：{_msg[:200]}'}
    
    future = m.make_future_dataframe(periods=periods)
    forecast = m.predict(future)
    
    #  RMSE/R²
    from sklearn.metrics import mean_squared_error, r2_score
    y_true = prophet_df['y'].values
    y_pred = forecast.iloc[:len(prophet_df)]['yhat'].values
    rmse = np.sqrt(mean_squared_error(y_true, y_pred))
    r2 = r2_score(y_true, y_pred)
    
    #  
    fig = m.plot(forecast, figsize=(12, 6))
    title = 'Prophet '
    plt.title(title, fontsize=14, fontweight='bold')
    
    buf = BytesIO()
    plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
    buf.seek(0)
    chart_b64 = base64.b64encode(buf.read()).decode('utf-8')
    plt.close()
    
    #  
    components_fig = m.plot_components(forecast, figsize=(10, 8))
    buf2 = BytesIO()
    plt.savefig(buf2, format='png', dpi=100, bbox_inches='tight')
    buf2.seek(0)
    components_b64 = base64.b64encode(buf2.read()).decode('utf-8')
    plt.close()
    
    predictions = forecast[['ds', 'yhat', 'yhat_lower', 'yhat_upper']].tail(periods)
    
    return {
        'ok': True,
        'rmse': float(rmse), 'r2': float(r2),
        'predictions': predictions.fillna('').astype(str).to_dict(orient='records'),
        'forecast_chart': chart_b64,
        'components_chart': components_b64,
    }

# ========================================
# 4.  / EWMA / 
# ========================================
def rolling_window_calc(df, value_col, window=20, calc_types=None):
    """  """
    if calc_types is None:
        calc_types = ['rolling_mean', 'rolling_std']
    
    df = df.copy()
    results = {}
    
    for ct in calc_types:
        col_name = f'{value_col}_{ct}_{window}'
        if ct == 'rolling_mean':
            df[col_name] = df[value_col].rolling(window=window, min_periods=1).mean()
        elif ct == 'rolling_std':
            df[col_name] = df[value_col].rolling(window=window, min_periods=1).std()
        elif ct == 'rolling_max':
            df[col_name] = df[value_col].rolling(window=window, min_periods=1).max()
        elif ct == 'rolling_min':
            df[col_name] = df[value_col].rolling(window=window, min_periods=1).min()
        elif ct == 'rolling_sum':
            df[col_name] = df[value_col].rolling(window=window, min_periods=1).sum()
        elif ct == 'ewma':
            df[col_name] = df[value_col].ewm(span=window, adjust=False).mean()
        elif ct == 'rolling_volatility':
            df[col_name] = df[value_col].rolling(window=window, min_periods=1).std() * np.sqrt(252)
        elif ct == 'rolling_sharpe':
            ret = df[value_col].pct_change()
            df[col_name] = (ret.rolling(window).mean() / ret.rolling(window).std()) * np.sqrt(252)
        elif ct == 'rolling_correlation':
            pass
        results[col_name] = df[col_name].dropna().tolist()
    
    #  
    fig, ax = plt.subplots(figsize=(12, 6))
    ax.plot(range(len(df)), df[value_col], label=value_col, color='black', alpha=0.5, linewidth=1)
    colors = ['#FF6B6B','#8b5cf6','#06b6d4','#22c55e','#f59e0b','#ef4444','#ec4899']
    for i, (cn, vals) in enumerate(results.items()):
        ax.plot(range(len(df)), df[cn] if cn in df.columns else vals, 
               label=cn, color=colors[i % len(colors)], linewidth=1.5)
    ax.legend(fontsize=9)
    ax.set_title(f'{value_col}  (window={window})', fontsize=14, fontweight='bold')
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    
    buf = BytesIO()
    plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
    buf.seek(0)
    chart_b64 = base64.b64encode(buf.read()).decode('utf-8')
    plt.close()
    
    return {
        'new_columns': list(results.keys()),
        'chart': chart_b64,
        'preview': df.head(20).fillna('').astype(str).to_dict(orient='records'),
        'window': window,
    }

#  
def get_numeric_columns(filepath):
    df = pd.read_csv(filepath)
    return {
        'columns': list(df.columns),
        'numeric': df.select_dtypes(include=np.number).columns.tolist(),
        'date_cols': [c for c in df.columns if 'date' in c.lower() or 'time' in c.lower() or '日期' in c or '时间' in c],
    }

# ========================================
# 5. 时序分解（趋势、季节、残差）
# ========================================
def decompose_timeseries(df, date_col, value_col, period=7, model='additive'):
    """STL / 经典分解"""
    df = df.copy()
    df[date_col] = pd.to_datetime(df[date_col], errors='coerce')
    df = df.dropna(subset=[date_col, value_col])
    df = df.sort_values(date_col)
    
    # 处理重复日期：按日期聚合（取均值）
    if df[date_col].duplicated().any():
        df = df.groupby(date_col, as_index=False)[value_col].mean()
    
    if len(df) < period * 2:
        return {'error': f'数据量 ({len(df)}) 不足，至少需要 {period*2} 行（2个完整周期）'}
    
    series = df.set_index(date_col)[value_col].asfreq('D')
    series = series.interpolate(method='linear')
    series = series.dropna()
    
    if len(series) < period * 2:
        return {'error': f'处理后数据量 ({len(series)}) 不足'}
    
    from statsmodels.tsa.seasonal import seasonal_decompose
    result = seasonal_decompose(series, model=model, period=period)
    
    n_show = min(500, len(series))
    show_dates = series.index[-n_show:]
    trend = result.trend.iloc[-n_show:]
    seasonal = result.seasonal.iloc[-n_show:]
    resid = result.resid.iloc[-n_show:]
    orig = series.iloc[-n_show:]
    
    fig, axes = plt.subplots(4, 1, figsize=(14, 12), sharex=True)
    axes[0].plot(show_dates, orig.values, color='#FF6B6B', linewidth=1)
    axes[0].set_title('原始数据', fontsize=12, fontweight='bold')
    axes[0].grid(True, alpha=0.3)
    
    axes[1].plot(show_dates, trend.values, color='#22c55e', linewidth=1.5)
    axes[1].set_title('趋势分量', fontsize=12, fontweight='bold')
    axes[1].grid(True, alpha=0.3)
    
    axes[2].plot(show_dates, seasonal.values, color='#f59e0b', linewidth=1.5)
    axes[2].set_title(f'季节分量 (period={period})', fontsize=12, fontweight='bold')
    axes[2].grid(True, alpha=0.3)
    
    axes[3].bar(show_dates, resid.values, color='#06b6d4', alpha=0.7, width=1)
    axes[3].set_title('残差分量', fontsize=12, fontweight='bold')
    axes[3].grid(True, alpha=0.3)
    axes[3].axhline(y=0, color='black', linestyle='--', linewidth=0.5, alpha=0.5)
    
    fig.autofmt_xdate()
    plt.tight_layout()
    
    buf = BytesIO()
    plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
    buf.seek(0)
    chart_b64 = base64.b64encode(buf.read()).decode('utf-8')
    plt.close()
    
    return {
        'chart': chart_b64,
        'period': period,
        'model': model,
        'total_rows': len(series),
        'trend_strength': round(float(1 - resid.var() / (trend + resid).var()) if (trend + resid).var() > 0 else 0, 4),
        'seasonal_strength': round(float(1 - resid.var() / (seasonal + resid).var()) if (seasonal + resid).var() > 0 else 0, 4),
    }

# ========================================
# 6. ADF 平稳性检验 + 差分工具
# ========================================
def adf_test_and_diff(df, value_col, diff_order=0, auto_diff=False):
    """ADF检验 + 差分"""
    df = df.copy()
    series = pd.to_numeric(df[value_col], errors='coerce').dropna()
    
    if len(series) < 10:
        return {'error': '数据量不足'}
    
    from statsmodels.tsa.stattools import adfuller
    
    result = adfuller(series, autolag='AIC')
    adf_stat = round(float(result[0]), 4)
    adf_pval = round(float(result[1]), 6)
    adf_crit = {k: round(float(v), 4) for k, v in result[4].items()}
    is_stationary = adf_pval < 0.05
    
    diff_series = None
    diff_chart = None
    actual_diff_order = diff_order
    
    if auto_diff or (not is_stationary and diff_order > 0):
        if auto_diff:
            d = 0
            s = series.copy()
            while d < 5:
                r = adfuller(s, autolag='AIC')
                if r[1] < 0.05:
                    break
                s = s.diff().dropna()
                d += 1
            actual_diff_order = d
        else:
            actual_diff_order = diff_order
        
        if actual_diff_order > 0:
            diff_series = series.diff(actual_diff_order).dropna()
            
            fig, axes = plt.subplots(2, 1, figsize=(14, 8))
            n1 = min(200, len(series))
            axes[0].plot(range(n1), series.iloc[-n1:].values, color='#FF6B6B', linewidth=1)
            axes[0].set_title('原始序列', fontsize=12, fontweight='bold')
            axes[0].grid(True, alpha=0.3)
            
            n2 = min(200, len(diff_series))
            axes[1].plot(range(n2), diff_series.iloc[-n2:].values, color='#22c55e', linewidth=1)
            axes[1].set_title(f'{actual_diff_order}阶差分后序列', fontsize=12, fontweight='bold')
            axes[1].grid(True, alpha=0.3)
            axes[1].axhline(y=0, color='black', linestyle='--', linewidth=0.5, alpha=0.5)
            
            diff_adf = adfuller(diff_series, autolag='AIC')
            diff_is_stationary = diff_adf[1] < 0.05
            
            plt.tight_layout()
            buf = BytesIO()
            plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
            buf.seek(0)
            diff_chart = base64.b64encode(buf.read()).decode('utf-8')
            plt.close()
            
            return {
                'adf_stat': adf_stat, 'adf_pval': adf_pval, 'adf_crit': adf_crit,
                'is_stationary': is_stationary,
                'diff_order': actual_diff_order,
                'diff_is_stationary': diff_is_stationary,
                'diff_adf_stat': round(float(diff_adf[0]), 4),
                'diff_adf_pval': round(float(diff_adf[1]), 6),
                'diff_chart': diff_chart,
                'sample_size': len(series),
            }
    
    return {
        'adf_stat': adf_stat, 'adf_pval': adf_pval, 'adf_crit': adf_crit,
        'is_stationary': is_stationary,
        'diff_order': actual_diff_order,
        'sample_size': len(series),
    }

# ========================================
# 7. ARIMA / SARIMA 时序模型
# ========================================
def arima_sarima_model(df, date_col, value_col, model_type='auto', order=(1,0,1),
                       seasonal_order=(1,0,1,7), forecast_periods=30):
    """ARIMA/SARIMA 建模与预测"""
    df = df.copy()
    if date_col:
        df[date_col] = pd.to_datetime(df[date_col], errors='coerce')
        df = df.sort_values(date_col)
    
    series = pd.to_numeric(df[value_col], errors='coerce').dropna()
    
    if len(series) < 20:
        return {'error': '数据量不足（至少需要20个观测值）'}
    
    from statsmodels.tsa.arima.model import ARIMA
    from statsmodels.tsa.statespace.sarimax import SARIMAX
    from statsmodels.tsa.stattools import adfuller
    
    train = series.iloc[:-forecast_periods] if len(series) > forecast_periods * 2 else series
    test = series.iloc[-forecast_periods:] if len(series) > forecast_periods * 2 else None
    
    if model_type == 'auto':
        r = adfuller(series, autolag='AIC')
        d = 1 if r[1] > 0.05 else 0
        order = (2, d, 1)
    
    try:
        if model_type in ('arima', 'auto'):
            model = ARIMA(train, order=order)
            fitted = model.fit()
        else:
            model = SARIMAX(train, order=order, seasonal_order=seasonal_order,
                           enforce_stationarity=False, enforce_invertibility=False)
            fitted = model.fit(disp=False)
    except Exception as e:
        return {'error': f'模型拟合失败: {str(e)}'}
    
    forecast = fitted.forecast(steps=forecast_periods)
    forecast_ci = fitted.get_forecast(steps=forecast_periods).conf_int() if hasattr(fitted, 'get_forecast') else None
    
    n_hist = min(200, len(train))
    fc_index = range(n_hist, n_hist + forecast_periods)
    
    fig, ax = plt.subplots(figsize=(14, 6))
    ax.plot(range(n_hist), train.iloc[-n_hist:].values, color='#FF6B6B', linewidth=1.5, label='训练数据')
    ax.plot(fc_index, forecast.values, color='#ef4444', linewidth=2, label='预测值')
    if forecast_ci is not None:
        ax.fill_between(fc_index, forecast_ci.iloc[:,0], forecast_ci.iloc[:,1],
                        color='#ef4444', alpha=0.15, label='95%置信区间')
    if test is not None and len(test) > 0:
        ax.plot(range(n_hist, n_hist + len(test)), test.values, color='#22c55e', 
                linewidth=1.5, linestyle='--', label='实际值')
        from sklearn.metrics import mean_squared_error, mean_absolute_error
        rmse = np.sqrt(mean_squared_error(test.values[:len(forecast)], forecast.values[:len(test)]))
        mae = mean_absolute_error(test.values[:len(forecast)], forecast.values[:len(test)])
    
    ax.legend(fontsize=10)
    model_name = 'ARIMA' if model_type in ('arima','auto') else 'SARIMA'
    ax.set_title(f'{model_name}({order[0]},{order[1]},{order[2]}) 预测', fontsize=14, fontweight='bold')
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    
    buf = BytesIO()
    plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
    buf.seek(0)
    chart_b64 = base64.b64encode(buf.read()).decode('utf-8')
    plt.close()
    
    fc_list = [round(float(x), 2) for x in forecast.values]
    
    result = {
        'chart': chart_b64,
        'model_name': model_name,
        'order': order,
        'seasonal_order': seasonal_order,
        'forecast': fc_list,
        'forecast_periods': forecast_periods,
        'aic': round(float(fitted.aic), 2),
        'bic': round(float(fitted.bic), 2),
    }
    
    if test is not None and len(test) > 0:
        result['rmse'] = round(float(rmse), 4)
        result['mae'] = round(float(mae), 4)
    
    return result
