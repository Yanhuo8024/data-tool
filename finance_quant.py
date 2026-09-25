"""
金融建模增强模块 — CAPM / 回测 / 策略评估 / 模型管理
"""

import json, os, pickle
from datetime import datetime

import pandas as pd
import numpy as np

try:
    from benchmark import (
        get_benchmark_cumulative,
        get_benchmark_returns,
        align_benchmark_to_strategy,
        compute_benchmark_metrics,
        INDICES,
    )
    HAS_BENCHMARK = True
except ImportError:
    HAS_BENCHMARK = False

try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    HAS_MPL = True
except ImportError:
    HAS_MPL = False

try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    HAS_PLOTLY = True
except ImportError:
    HAS_PLOTLY = False

MODEL_DIR = None
COMMON_LAYOUT = dict(paper_bgcolor='#0f111c', plot_bgcolor='#151725',
                     font=dict(color='#e0e0e0'), showlegend=True,
                     margin=dict(l=50, r=20, t=40, b=40))




def set_model_dir(d):
    global MODEL_DIR
    MODEL_DIR = d
    os.makedirs(d, exist_ok=True)


def img_to_b64(fig):
    """matplotlib figure -> base64 PNG"""
    import base64, io
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=100, bbox_inches='tight',
                facecolor='#0f111c', edgecolor='none')
    buf.seek(0)
    return base64.b64encode(buf.read()).decode()


def fig_to_html(fig):
    """Plotly figure -> HTML div"""
    return fig.to_html(
        include_plotlyjs='cdn',
        full_html=False,
        config={
            'scrollZoom': True,
            'displayModeBar': True,
            'displaylogo': False,
            'responsive': True,
        }
    )


def _load_data(filepath):
    df = pd.read_csv(filepath)
    # 自动日期解析
    for c in df.columns:
        cl = c.lower()
        if 'date' in cl or '日期' in cl or 'time' in cl:
            df[cl] = pd.to_datetime(df[cl], errors='coerce')
    return df


def _find_col(df, *keywords):
    """根据关键词查找列名"""
    for c in df.columns:
        cl = c.lower()
        for kw in keywords:
            if kw in cl:
                return c
    return None


# ==================== CAPM ====================

def capm_analysis(filepath, close_col=None, market_col=None, rf_rate=0.02):
    """
    CAPM 分析：计算 Beta、Alpha、夏普比率、预期收益率
    """
    df = _load_data(filepath)
    
    cc = close_col or _find_col(df, 'close', '收盘')
    if not cc:
        raise ValueError('未找到收盘价列')
    
    # 计算日收益率
    df['return'] = df[cc].pct_change().dropna()
    
    # 如有市场列用市场，否则用收盘价自身的均值归零
    if market_col and market_col in df.columns:
        df['market_return'] = df[market_col].pct_change()
    else:
        # 假设市场收益率为0（简化）
        df['market_return'] = df['return'] * 0
    
    df = df.dropna()
    
    returns = df['return'].values
    mkt = df['market_return'].values
    
    # 线性回归求 Beta
    if len(returns) > 1 and np.std(mkt) > 1e-10:
        cov = np.cov(returns, mkt)[0, 1]
        var = np.var(mkt)
        beta = cov / var
        alpha = np.mean(returns) - beta * np.mean(mkt)
    else:
        beta = 0
        alpha = np.mean(returns)
    
    # 年化基准（美股252，A股约244）
    trading_days = 252
    daily_rf = rf_rate / trading_days
    
    mean_return = np.mean(returns)
    std_return = np.std(returns)
    annual_return = mean_return * trading_days
    annual_vol = std_return * np.sqrt(trading_days)
    sharpe = (annual_return - rf_rate) / annual_vol if annual_vol > 0 else 0
    
    # 预期收益 (CAPM)
    expected_return = rf_rate + beta * (annual_return - rf_rate)
    
    # 最大回撤
    cum = pd.Series((1 + returns).cumprod())
    peak = cum.cummax()
    drawdown = (cum - peak) / peak
    max_dd = drawdown.min()
    
    # 胜率
    win_rate = (returns > 0).sum() / len(returns)
    
    # 生成图表
    if HAS_MPL:
        fig, axes = plt.subplots(2, 2, figsize=(10, 7))
        fig.patch.set_facecolor('#0f111c')
        for ax in axes.flat:
            ax.set_facecolor('#151725')
            ax.tick_params(colors='#a0a0b0')
            ax.spines.values().__iter__().__next__().set_color('#333')
        
        # 收益分布
        axes[0, 0].hist(returns * 100, bins=30, color='#4caf50', alpha=0.7, edgecolor='none')
        axes[0, 0].axvline(x=0, color='#ef5350', linestyle='--', linewidth=1)
        axes[0, 0].set_title('日收益率分布', color='#e0e0e0', fontsize=11)
        axes[0, 0].set_xlabel('收益率 (%)', color='#a0a0b0')
        
        # 累计收益
        axes[0, 1].plot(cum.index, cum.values, color='#4caf50', linewidth=1.5)
        axes[0, 1].fill_between(range(len(cum)), cum.values, 1, alpha=0.2, color='#4caf50')
        axes[0, 1].set_title('累计净值曲线', color='#e0e0e0', fontsize=11)
        
        # 回撤
        axes[1, 0].fill_between(range(len(drawdown)), drawdown.values * 100, 0,
                                color='#ef5350', alpha=0.5)
        axes[1, 0].set_title('回撤曲线 (%)', color='#e0e0e0', fontsize=11)
        
        # SML
        dp = min(len(returns), 100)
        axes[1, 1].scatter(returns[:dp] * 100, mkt[:dp] * 100, alpha=0.4,
                           c='#7c4dff', s=20)
        xr = np.linspace(min(returns)*100, max(returns)*100, 50)
        axes[1, 1].plot(xr, [0]*50, color='#666', linewidth=0.5)
        axes[1, 1].set_title(f'收益 vs 市场 (β={beta:.3f})', color='#e0e0e0', fontsize=11)
        
        plt.tight_layout()
        chart = img_to_b64(fig)
        plt.close()
    else:
        chart = None
    
    return {
        'beta': round(beta, 4),
        'alpha_daily': round(alpha, 6),
        'annual_return_pct': round(annual_return * 100, 2),
        'annual_volatility_pct': round(annual_vol * 100, 2),
        'sharpe_ratio': round(sharpe, 3),
        'max_drawdown_pct': round(max_dd * 100, 2),
        'expected_return_pct': round(expected_return * 100, 2),
        'win_rate_pct': round(win_rate * 100, 2),
        'data_points': len(returns),
        'chart': chart,
    }


# ==================== 回测框架辅助函数 ====================

def _calc_drawdown(cum_returns):
    peak = cum_returns.cummax()
    dd = (cum_returns - peak) / peak
    return dd.min()

def _prepare_project():
    """Prepares plotting environment."""
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        return True
    except ImportError:
        return False

def _fetch_index_benchmark(df, benchmark):
    """Fetch benchmark index data and returns."""
    if not HAS_BENCHMARK or not benchmark:
        return None, None
    date_col = _find_col(df, 'date', '日期')
    if date_col:
        start = str(df[date_col].iloc[0])[:10].replace('-', '')
        end = str(df[date_col].iloc[-1])[:10].replace('-', '')
        idx_ret = get_benchmark_returns(benchmark, start, end)
        if idx_ret is not None and len(idx_ret) > 0:
            idx_cum = (1 + idx_ret.fillna(0)).cumprod()
            return idx_cum, compute_benchmark_metrics(idx_ret)
    return None, None

def _build_index_result(result, index_metrics, benchmark):
    """Adds benchmark metrics to result dict."""
    if index_metrics:
        result.update({
            'index_annual_return_pct': index_metrics.get('annual_return_pct'),
            'index_volatility_pct': index_metrics.get('volatility_pct'),
            'index_max_drawdown_pct': index_metrics.get('max_drawdown_pct'),
            'index_sharpe_ratio': index_metrics.get('sharpe_ratio'),
            'index_name': INDICES.get(benchmark, benchmark),
        })

def _add_benchmark_trace(fig, index_cum, benchmark, row, col):
    """Adds benchmark line to plotly figure."""
    if index_cum is not None and len(index_cum) > 0:
        name = INDICES.get(benchmark, benchmark)
        fig.add_trace(go.Scatter(
            y=index_cum.values, name=name,
            line=dict(color='#ff9800', width=1.5, dash='dot')), row=row, col=col)

def _make_backtest_chart(fig, strat_cum, bench_cum, index_cum, benchmark, **kwargs):
    """Creates standard backtest plotly chart with optional benchmark."""
    fig.add_trace(go.Scatter(y=strat_cum.values, name='Strategy',
                              line=dict(color='#4caf50', width=2)), row=1, col=1)
    fig.add_trace(go.Scatter(y=bench_cum.values, name='Buy & Hold',
                              line=dict(color='#666', width=1, dash='dash')), row=1, col=1)
    
    if index_cum is not None and len(index_cum) > 0:
        name = INDICES.get(benchmark, benchmark)
        fig.add_trace(go.Scatter(
            y=index_cum.values, name=name,
            line=dict(color='#ff9800', width=1.5, dash='dot')), row=1, col=1)
    
    dd = (strat_cum - strat_cum.cummax()) / strat_cum.cummax() * 100
    fig.add_trace(go.Scatter(y=dd.values, name='Drawdown %',
                              fill='tozeroy', line=dict(color='#ef5350', width=1)),
                  row=2, col=1)
    
    fig.update_layout(
        paper_bgcolor='#0f111c', plot_bgcolor='#151725',
        font=dict(color='#e0e0e0'), showlegend=True,
        height=kwargs.get('height', 500),
        margin=dict(l=50, r=20, t=40, b=40),
        yaxis=dict(title='Equity'), yaxis2=dict(title='Drawdown (%)'),
    )
    return fig_to_html(fig)


def backtest_ma_crossover(filepath, close_col=None, fast=5, slow=20, benchmark="000300"):
    """均线金叉/死叉策略回测（含基准对比）"""
    df = _load_data(filepath)
    cc = close_col or _find_col(df, 'close', '收盘')
    if not cc:
        raise ValueError('未找到收盘价列')
    
    df['fast_ma'] = df[cc].rolling(fast).mean()
    df['slow_ma'] = df[cc].rolling(slow).mean()
    df = df.dropna()
    
    df['signal'] = np.where(df['fast_ma'] > df['slow_ma'], 1, -1)
    df['position'] = df['signal'].shift(1).fillna(0)
    df['daily_return'] = df[cc].pct_change()
    df['strategy_return'] = df['daily_return'] * df['position']
    
    trading_days = 252
    strat_cum = (1 + df['strategy_return'].fillna(0)).cumprod()
    bench_cum = (1 + df['daily_return'].fillna(0)).cumprod()
    
    index_cum, index_metrics = _fetch_index_benchmark(df, benchmark)
    
    strat_ann = (strat_cum.iloc[-1]) ** (trading_days / len(df)) - 1
    bench_ann = (bench_cum.iloc[-1]) ** (trading_days / len(df)) - 1
    strat_vol = df['strategy_return'].std() * np.sqrt(trading_days)
    bench_vol = df['daily_return'].std() * np.sqrt(trading_days)
    max_dd = _calc_drawdown(strat_cum)
    
    wins = (df['strategy_return'] > 0).sum()
    total = max((df['strategy_return'] != 0).sum(), 1)
    win_rate = wins / total
    trade_signals = (df['signal'].diff() != 0).sum()
    
    chart_html = None
    if HAS_PLOTLY:
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                            vertical_spacing=0.08, row_heights=[0.6, 0.4])
        fig.add_trace(go.Scatter(y=strat_cum.values, name='策略净值',
                                  line=dict(color='#4caf50', width=2)), row=1, col=1)
        fig.add_trace(go.Scatter(y=bench_cum.values, name='基准 (持有)',
                                  line=dict(color='#666', width=1, dash='dash')), row=1, col=1)
        _add_benchmark_trace(fig, index_cum, benchmark, 1, 1)
        
        dd_series = (strat_cum - strat_cum.cummax()) / strat_cum.cummax() * 100
        fig.add_trace(go.Scatter(y=dd_series.values, name='回撤 %',
                                  fill='tozeroy', line=dict(color='#ef5350', width=1)), row=2, col=1)
        
        title = f'均线策略回测 (MA{fast}/{slow})'
        if index_metrics:
            title += f' vs {INDICES.get(benchmark, benchmark)}'
        fig.update_layout(title=title, height=500, **COMMON_LAYOUT)
        chart_html = fig_to_html(fig)
    
    result = {
        'strategy': f'MA{fast}/{slow} 金叉做多/死叉做空',
        'strategy_annual_return_pct': round(strat_ann * 100, 2),
        'benchmark_annual_return_pct': round(bench_ann * 100, 2),
        'strategy_volatility_pct': round(strat_vol * 100, 2),
        'benchmark_volatility_pct': round(bench_vol * 100, 2),
        'max_drawdown_pct': round(max_dd * 100, 2),
        'win_rate_pct': round(win_rate * 100, 2),
        'trade_count': int(trade_signals),
        'total_return_pct': round((strat_cum.iloc[-1] - 1) * 100, 2),
        'chart_html': chart_html,
        'benchmark_symbol': benchmark,
    }
    _build_index_result(result, index_metrics, benchmark)
    return result


def backtest_macd(filepath, close_col=None, fast=12, slow=26, signal=9, benchmark="000300"):
    """MACD 策略回测（含基准对比）"""
    df = _load_data(filepath)
    cc = close_col or _find_col(df, 'close', '收盘')
    if not cc:
        raise ValueError('未找到收盘价列')
    
    df['ema_fast'] = df[cc].ewm(span=fast).mean()
    df['ema_slow'] = df[cc].ewm(span=slow).mean()
    df['dif'] = df['ema_fast'] - df['ema_slow']
    df['dea'] = df['dif'].ewm(span=signal).mean()
    df['macd_bar'] = 2 * (df['dif'] - df['dea'])
    df = df.dropna()
    
    df['signal'] = np.where(df['dif'] > df['dea'], 1, 0)
    df['position'] = df['signal'].shift(1).fillna(0)
    df['daily_return'] = df[cc].pct_change()
    df['strategy_return'] = df['daily_return'] * df['position']
    
    trading_days = 252
    strat_cum = (1 + df['strategy_return'].fillna(0)).cumprod()
    bench_cum = (1 + df['daily_return'].fillna(0)).cumprod()
    
    index_cum, index_metrics = _fetch_index_benchmark(df, benchmark)
    
    strat_ann = (strat_cum.iloc[-1]) ** (trading_days / len(df)) - 1
    bench_ann = (bench_cum.iloc[-1]) ** (trading_days / len(df)) - 1
    max_dd = _calc_drawdown(strat_cum)
    wins = (df['strategy_return'] > 0).sum()
    total = max((df['strategy_return'] != 0).sum(), 1)
    win_rate = wins / total
    
    chart_html = None
    if HAS_PLOTLY:
        fig = make_subplots(rows=3, cols=1, shared_xaxes=True,
                            vertical_spacing=0.05, row_heights=[0.35, 0.35, 0.3])
        
        fig.add_trace(go.Scatter(y=df[cc].values, name='收盘价',
                                  line=dict(color='#2196f3', width=1)), row=1, col=1)
        fig.add_trace(go.Scatter(y=strat_cum.values, name='策略净值',
                                  line=dict(color='#4caf50', width=2)), row=1, col=1)
        _add_benchmark_trace(fig, index_cum, benchmark, 1, 1)
        
        colors_bar = ['#ef5350' if v >= 0 else '#26a69a' for v in df['macd_bar']]
        fig.add_trace(go.Bar(y=df['macd_bar'].values, name='MACD柱',
                              marker_color=colors_bar, opacity=0.7), row=2, col=1)
        fig.add_trace(go.Scatter(y=df['dif'].values, name='DIF',
                                  line=dict(color='#ffd54f', width=1)), row=2, col=1)
        fig.add_trace(go.Scatter(y=df['dea'].values, name='DEA',
                                  line=dict(color='#ff9800', width=1)), row=2, col=1)
        
        dd_series = (strat_cum - strat_cum.cummax()) / strat_cum.cummax() * 100
        fig.add_trace(go.Scatter(y=dd_series.values, name='回撤 %',
                                  fill='tozeroy', line=dict(color='#ef5350', width=1)), row=3, col=1)
        
        title = f'MACD 策略回测 ({fast}/{slow}/{signal})'
        if index_metrics:
            title += f' vs {INDICES.get(benchmark, benchmark)}'
        fig.update_layout(title=title, height=600, **COMMON_LAYOUT)
        chart_html = fig_to_html(fig)
    
    result = {
        'strategy': f'MACD({fast},{slow},{signal}) 金叉做多/死叉做空',
        'strategy_annual_return_pct': round(strat_ann * 100, 2),
        'benchmark_annual_return_pct': round(bench_ann * 100, 2),
        'max_drawdown_pct': round(max_dd * 100, 2),
        'win_rate_pct': round(win_rate * 100, 2),
        'trade_count': int((df['signal'].diff() != 0).sum()),
        'total_return_pct': round((strat_cum.iloc[-1] - 1) * 100, 2),
        'chart_html': chart_html,
        'benchmark_symbol': benchmark,
    }
    _build_index_result(result, index_metrics, benchmark)
    return result


# ==================== 预测评估 ====================

def evaluate_predictions(filepath, close_col=None, test_size=0.3, model_type='linear',
                         predict_days=30):
    """
    训练预测模型并输出评估指标对比
    """
    from sklearn.linear_model import LinearRegression, Ridge, Lasso
    from sklearn.ensemble import RandomForestRegressor, GradientBoostingRegressor
    from sklearn.neighbors import KNeighborsRegressor
    from sklearn.svm import SVR
    from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
    
    df = _load_data(filepath)
    cc = close_col or _find_col(df, 'close', '收盘')
    if not cc:
        raise ValueError('未找到收盘价列')
    
    df['return'] = df[cc].pct_change()
    df['target'] = df[cc].shift(-predict_days)
    df = df.dropna()
    
    # 构建特征
    features = []
    for lag in [1, 2, 3, 5, 10]:
        if len(df) > lag:
            df[f'close_lag_{lag}'] = df[cc].shift(lag)
            features.append(f'close_lag_{lag}')
            df[f'return_lag_{lag}'] = df['return'].shift(lag)
            features.append(f'return_lag_{lag}')
    
    df = df.dropna()
    split = int(len(df) * (1 - test_size))
    train, test = df.iloc[:split], df.iloc[split:]
    
    X_train, y_train = train[features], train['target']
    X_test, y_test = test[features], test['target']
    
    models = {
        'Linear': LinearRegression(),
        'Ridge (α=1)': Ridge(alpha=1),
        'Lasso (α=0.01)': Lasso(alpha=0.01, max_iter=2000),
        'RandomForest': RandomForestRegressor(n_estimators=100, max_depth=5, random_state=42),
        'GradientBoost': GradientBoostingRegressor(n_estimators=100, max_depth=3, random_state=42),
        'KNN (k=5)': KNeighborsRegressor(n_neighbors=5),
        'SVR': SVR(kernel='rbf'),
    }
    
    results = []
    best_model = None
    best_score = -np.inf
    
    for name, model in models.items():
        try:
            model.fit(X_train, y_train)
            y_pred = model.predict(X_test)
            
            mae = mean_absolute_error(y_test, y_pred)
            mse = mean_squared_error(y_test, y_pred)
            rmse = np.sqrt(mse)
            r2 = r2_score(y_test, y_pred)
            
            # 方向准确率
            actual_dir = np.sign(np.diff(y_test, prepend=y_test[0]))
            pred_dir = np.sign(np.diff(y_pred, prepend=y_pred[0]))
            dir_acc = (actual_dir == pred_dir).sum() / len(actual_dir)
            
            results.append({
                'model': name,
                'MAE': round(mae, 4),
                'MSE': round(mse, 4),
                'RMSE': round(rmse, 4),
                'R2': round(r2, 4),
                'Direction_Accuracy': round(dir_acc, 4),
                'predictions': y_pred.tolist()[:5],  # 前5条预测
            })
            
            if r2 > best_score:
                best_score = r2
                best_model = {'name': name, 'model': model, 'features': features}
        except Exception:
            pass
    
    # 可视化评估对比
    if HAS_MPL:
        fig, axes = plt.subplots(2, 2, figsize=(11, 8))
        fig.patch.set_facecolor('#0f111c')
        for ax in axes.flat:
            ax.set_facecolor('#151725')
            ax.tick_params(colors='#a0a0b0', labelsize=9)
        
        names = [r['model'] for r in results]
        colors_palette = ['#4caf50', '#2196f3', '#ff9800', '#e91e63',
                          '#9c27b0', '#00bcd4', '#ff5722']
        
        metrics = ['MAE', 'RMSE', 'R2', 'Direction_Accuracy']
        for i, metric in enumerate(metrics):
            ax = axes.flat[i]
            vals = [r[metric] for r in results]
            bars = ax.bar(range(len(vals)), vals,
                          color=[colors_palette[j % len(colors_palette)] for j in range(len(vals))],
                          alpha=0.7, edgecolor='none')
            for bar, val in zip(bars, vals):
                ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.02,
                        f'{val:.3f}', ha='center', va='bottom', fontsize=8, color='#e0e0e0')
            ax.set_title(metric, color='#e0e0e0', fontsize=11)
            ax.set_xticks(range(len(names)))
            ax.set_xticklabels(names, rotation=45, ha='right', fontsize=8)
        
        plt.tight_layout()
        chart = img_to_b64(fig)
        plt.close()
    else:
        chart = None
    
    # 保存最佳模型
    model_path = None
    best_name = None
    if best_model and MODEL_DIR:
        model_path = os.path.join(MODEL_DIR, f'model_{datetime.now().strftime("%Y%m%d_%H%M%S")}.pkl')
        with open(model_path, 'wb') as f:
            pickle.dump(best_model, f)
        best_name = best_model['name']
    
    return {
        'results': results,
        'best_model': best_name,
        'model_path': model_path,
        'chart': chart,
    }


# ==================== 模型管理 ====================

def list_saved_models():
    """列出已保存的模型"""
    if not MODEL_DIR or not os.path.exists(MODEL_DIR):
        return []
    models = []
    for f in os.listdir(MODEL_DIR):
        if f.endswith('.pkl') and f.startswith('model_'):
            p = os.path.join(MODEL_DIR, f)
            models.append({
                'name': f,
                'path': p,
                'size_kb': round(os.path.getsize(p) / 1024, 2),
                'created': datetime.fromtimestamp(os.path.getmtime(p)).strftime('%Y-%m-%d %H:%M'),
            })
    return sorted(models, key=lambda m: m['created'], reverse=True)


def load_model(model_name):
    """加载保存的模型（白名单反序列化 + 路径约束在 MODEL_DIR 内）"""
    if not MODEL_DIR:
        raise ValueError('MODEL_DIR 未设置')
    # 仅接受纯文件名，防 ../ 路径穿越
    safe_name = os.path.basename(str(model_name))
    if not safe_name or safe_name != str(model_name).strip() or '..' in str(model_name):
        raise ValueError(f'非法模型名称: {model_name}')
    path = os.path.join(MODEL_DIR, safe_name)
    # resolve 后必须仍在 MODEL_DIR 内
    rp = os.path.realpath(path)
    if not os.path.realpath(MODEL_DIR) in rp:
        raise ValueError(f'模型路径越界: {model_name}')
    if not os.path.exists(rp):
        raise FileNotFoundError(f'模型文件不存在: {safe_name}')
    try:
        from secure_safe_load import safe_load
        return safe_load(rp)
    except ImportError:
        # 安全加载器缺失时退回普通加载（仅本地可信环境）
        import warnings
        warnings.warn('secure_safe_load 不可用，退回普通 pickle 加载')
        with open(rp, 'rb') as f:
            return pickle.load(f)


def predict_with_model(model_data, filepath, close_col=None, predict_days=30):
    """使用保存的模型进行预测"""
    model = model_data['model']
    features = model_data['features']
    
    df = _load_data(filepath)
    cc = close_col or _find_col(df, 'close', '收盘')
    if not cc:
        raise ValueError('未找到收盘价列')
    
    df['return'] = df[cc].pct_change()
    for lag in [1, 2, 3, 5, 10]:
        if len(df) > lag:
            if f'close_lag_{lag}' not in df.columns:
                df[f'close_lag_{lag}'] = df[cc].shift(lag)
            if f'return_lag_{lag}' not in df.columns:
                df[f'return_lag_{lag}'] = df['return'].shift(lag)
    df = df.dropna()
    
    preds = model.predict(df[features][-predict_days:])
    
    if HAS_PLOTLY:
        fig = go.Figure()
        fig.add_trace(go.Scatter(y=df[cc].values[-100:], name='历史收盘价',
                                  line=dict(color='#2196f3', width=1.5)))
        fig.add_trace(go.Scatter(x=list(range(len(df)-predict_days, len(df))),
                                  y=preds, name=f'{model_data["name"]} 预测',
                                  line=dict(color='#ff9800', width=2, dash='dash')))
        fig.update_layout(
            title='模型预测',
            paper_bgcolor='#0f111c', plot_bgcolor='#151725',
            font=dict(color='#e0e0e0'), height=400,
            margin=dict(l=50, r=20, t=40, b=40),
        )
        chart = fig_to_html(fig)
    else:
        chart = None
    
    return {
        'predictions': preds.tolist(),
        'model_name': model_data['name'],
        'predict_days': predict_days,
        'chart': chart,
    }


def delete_model(model_name):
    """删除模型文件

    安全约束：仅允许删除 MODEL_DIR 下的普通文件，禁止目录、路径穿越与空名
    （历史上传空名会解析成目录本身，os.remove 目录将触发 WinError 5 且有误删风险）。
    """
    if not MODEL_DIR or not model_name or not isinstance(model_name, str):
        return False
    name = model_name.strip()
    # 拒绝路径分隔符与上级引用，只接受纯文件名
    if not name or os.path.sep in name or "/" in name or "\\" in name or name in (".", ".."):
        return False
    if name != os.path.basename(name):
        return False
    base = os.path.abspath(MODEL_DIR)
    path = os.path.abspath(os.path.join(base, name))
    # 必须严格位于 MODEL_DIR 之内（防穿越）且为文件（防删目录）
    if os.path.dirname(path) != base:
        return False
    if not os.path.isfile(path):
        return False
    os.remove(path)
    return True
