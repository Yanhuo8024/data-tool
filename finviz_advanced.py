"""
金融可视化增强模块 — Plotly 交互式图表
支持：资金流向 / 筹码分布 / 热力图 / 收益曲线 / 最大回撤
      多股对比 / 样式自定义 / 批量导出
"""

import json, base64, io, zipfile
from datetime import datetime, timedelta

import pandas as pd
import numpy as np

# 尝试导入 plotly
try:
    import plotly.graph_objects as go
    import plotly.express as px
    from plotly.subplots import make_subplots
    HAS_PLOTLY = True
except ImportError:
    HAS_PLOTLY = False


def _check_imports():
    if not HAS_PLOTLY:
        raise ImportError("plotly 未安装，请运行: pip install plotly")


def _load_data(filepath, date_col=None):
    """加载 CSV 数据"""
    df = pd.read_csv(filepath)
    # 尝试解析日期
    if date_col and date_col in df.columns:
        df[date_col] = pd.to_datetime(df[date_col], errors='coerce')
        df = df.sort_values(date_col).reset_index(drop=True)
    return df


def _default_layout(title, width=900, height=500, colors=None, font=None, **extra):
    """生成统一的 Plotly layout"""
    _check_imports()
    
    default_colors = {
        'bg': 'rgba(20,22,35,0.9)',
        'paper_bg': 'rgba(15,17,28,0.95)',
        'text': '#e0e0e0',
        'grid': 'rgba(255,255,255,0.05)',
        'up': '#ef5350',
        'down': '#26a69a',
    }
    if colors:
        default_colors.update(colors)
    
    layout = go.Layout(
        title=dict(text=title, font=dict(size=16, color=default_colors['text'], family=font or 'Arial')),
        width=width,
        height=height,
        paper_bgcolor=default_colors['paper_bg'],
        plot_bgcolor=default_colors['bg'],
        font=dict(color=default_colors['text'], size=12, family=font or 'Arial'),
        xaxis=dict(
            gridcolor=default_colors['grid'],
            zerolinecolor=default_colors['grid'],
            showgrid=True,
        ),
        yaxis=dict(
            gridcolor=default_colors['grid'],
            zerolinecolor=default_colors['grid'],
            showgrid=True,
        ),
        hovermode='x unified',
        margin=dict(l=60, r=30, t=50, b=60),
        dragmode='zoom',
    )
    return layout, default_colors


def fig_to_html(fig):
    """将 Plotly Figure 转为可嵌入的 HTML div"""
    return fig.to_html(
        include_plotlyjs='cdn',
        full_html=False,
        config={
            'scrollZoom': True,
            'displayModeBar': True,
            'modeBarButtonsToAdd': ['drawline', 'eraseshape'],
            'modeBarButtonsToRemove': ['sendDataToCloud'],
            'displaylogo': False,
            'responsive': True,
        }
    )


# ===================== 图表类型函数 =====================

def money_flow_chart(filepath, date_col='date', high_col='high', low_col='low',
                     close_col='close', volume_col='volume', **kwargs):
    """
    资金流向图 — 基于量价关系的资金流入/流出估算
    使用简化模型：资金流向 = 成交量 × (收盘价 - 均价)
    """
    _check_imports()
    df = _load_data(filepath, date_col)
    
    # 自动匹配列名
    col_map = {}
    for c in df.columns:
        cl = c.lower()
        if 'high' in cl or '最高' in cl: col_map['high'] = c
        if 'low' in cl or '最低' in cl: col_map['low'] = c
        if 'close' in cl or '收盘' in cl or '收' == c: col_map['close'] = c
        if 'volume' in cl or '成交量' in cl or 'vol' in cl: col_map['volume'] = c
        if 'date' in cl or '日期' in cl or 'time' in cl: col_map['date'] = c
    
    hc = col_map.get('high', high_col)
    lc = col_map.get('low', low_col)
    cc = col_map.get('close', close_col)
    vc = col_map.get('volume', volume_col)
    dc = col_map.get('date', date_col)
    
    # 计算典型价和资金流向
    df['typical'] = (df[hc] + df[lc] + df[cc]) / 3
    df['money_flow'] = df[vc] * df['typical']  # 原始资金流量
    df['money_flow_ma'] = df['money_flow'].rolling(5).mean()
    
    # 判断流入/流出
    df['direction'] = np.where(df[cc] >= df[cc].shift(1), '流入', '流出')
    
    layout, clrs = _default_layout('资金流向分析', **kwargs)
    
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        vertical_spacing=0.08,
                        row_heights=[0.6, 0.4])
    
    # 上：K线简化 + 资金流向柱
    colors_bar = [clrs['up'] if d == '流入' else clrs['down'] for d in df['direction']]
    fig.add_trace(go.Bar(x=df[dc], y=df['money_flow'], name='资金流量',
                         marker_color=colors_bar, opacity=0.7), row=1, col=1)
    fig.add_trace(go.Scatter(x=df[dc], y=df['money_flow_ma'], name='5期均值',
                             line=dict(color='#ffd54f', width=2)), row=1, col=1)
    
    # 下：累计资金流向
    df['cum_flow'] = df['money_flow'].cumsum()
    fig.add_trace(go.Scatter(x=df[dc], y=df['cum_flow'], name='累计资金流向',
                             fill='tozeroy', line=dict(color='#7c4dff', width=2),
                             fillcolor='rgba(124,77,255,0.2)'), row=2, col=1)
    
    fig.update_layout(layout, showlegend=True, 
                      yaxis=dict(title='资金流量'),
                      yaxis2=dict(title='累计'))
    fig.update_xaxes(title_text='日期', row=2, col=1)
    
    stats = {
        'total_inflow': float(df.loc[df['direction']=='流入', 'money_flow'].sum()),
        'total_outflow': float(df.loc[df['direction']=='流出', 'money_flow'].sum()),
        'net_flow': float(df['money_flow'].sum()),
        'periods': len(df),
    }
    return fig_to_html(fig), stats


def chip_distribution_chart(filepath, date_col='date', close_col='close',
                            volume_col='volume', bins=30, **kwargs):
    """
    筹码分布图 — 成本分布直方图
    模拟个股的持仓成本分布
    """
    _check_imports()
    df = _load_data(filepath, date_col)
    
    # 自动匹配列
    col_map = {}
    for c in df.columns:
        cl = c.lower()
        if 'close' in cl or '收盘' in cl or '收' == c: col_map['close'] = c
        if 'volume' in cl or '成交量' in cl or 'vol' in cl: col_map['volume'] = c
        if 'date' in cl or '日期' in cl: col_map['date'] = c
    
    cc = col_map.get('close', close_col)
    vc = col_map.get('volume', volume_col)
    dc = col_map.get('date', date_col)
    
    # 用收盘价和成交量模拟筹码分布
    prices = df[cc].values
    volumes = df[vc].values / df[vc].max()  # 归一化
    
    # 生成加权分布
    price_min, price_max = prices.min(), prices.max()
    hist_bins = np.linspace(price_min, price_max, bins)
    weighted_hist = np.zeros(bins - 1)
    
    for i in range(len(prices) - 1):
        idx = np.searchsorted(hist_bins, prices[i]) - 1
        if 0 <= idx < len(weighted_hist):
            weighted_hist[idx] += volumes[i]
    
    # 平滑
    weighted_hist = np.convolve(weighted_hist, np.ones(3)/3, mode='same')
    
    layout, clrs = _default_layout('筹码分布 (成本分布)', **kwargs)
    
    # 找到成本密集区
    peak_idx = np.argmax(weighted_hist)
    support = hist_bins[peak_idx] if peak_idx > 0 else price_min
    resistance = hist_bins[peak_idx + 1] if peak_idx + 1 < len(hist_bins) else price_max
    concentration = (support + resistance) / 2
    
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=hist_bins[:-1] + (hist_bins[1] - hist_bins[0])/2,
        y=weighted_hist,
        name='持仓成本',
        marker_color='rgba(124,77,255,0.6)',
        marker_line_color='rgba(124,77,255,0.3)',
        marker_line_width=1,
        hovertemplate='价格: %{x:.2f}<br>筹码量: %{y:.1%}<extra></extra>',
    ))
    
    # 标注成本密集区
    fig.add_vline(x=concentration, line_dash='dash', line_color='#ffd54f',
                  annotation_text=f'密集区 {concentration:.2f}')
    
    fig.update_layout(
        layout,
        xaxis_title='价格',
        yaxis_title='筹码分布比例',
        bargap=0.1,
        showlegend=False,
    )
    
    stats = {
        'price_min': float(price_min),
        'price_max': float(price_max),
        'concentration_price': float(concentration),
        'current_price': float(df[cc].iloc[-1]),
    }
    return fig_to_html(fig), stats


def interactive_heatmap(filepath, method='pearson', **kwargs):
    """
    交互式相关性热力图
    支持 Pearson / Spearman
    """
    _check_imports()
    df = _load_data(filepath)
    
    # 仅选择数值列
    numeric_df = df.select_dtypes(include=[np.number])
    if numeric_df.shape[1] < 2:
        return '<div style="color:var(--error);">数据中数值列不足，无法生成热力图</div>', {}
    
    if method == 'spearman':
        corr = numeric_df.corr(method='spearman')
    else:
        corr = numeric_df.corr(method='pearson')
    
    labels = corr.columns.tolist()
    
    layout, clrs = _default_layout(f'相关性热力图 ({method})', **kwargs)
    
    fig = go.Figure(data=go.Heatmap(
        z=corr.values,
        x=labels,
        y=labels,
        colorscale='RdBu_r',
        zmin=-1, zmax=1,
        text=np.round(corr.values, 2),
        texttemplate='%{text}',
        textfont=dict(size=10, color=clrs['text']),
        hovertemplate='%{x} vs %{y}<br>相关系数: %{z:.3f}<extra></extra>',
    ))
    
    fig.update_layout(
        layout,
        xaxis=dict(tickangle=45, side='bottom'),
        yaxis=dict(tickangle=0, autorange='reversed'),
        margin=dict(l=100, r=30, t=50, b=120),
    )
    
    # 找出最强相关性
    corr_vals = corr.values
    mask = np.ones_like(corr_vals, dtype=bool)
    np.fill_diagonal(mask, 0)
    max_idx = np.unravel_index(np.argmax(np.abs(corr_vals * mask)), corr_vals.shape)
    
    stats = {
        'n_cols': len(labels),
        'max_corr_pair': f'{labels[max_idx[0]]} vs {labels[max_idx[1]]}',
        'max_corr_value': float(corr_vals[max_idx]),
        'method': method,
    }
    return fig_to_html(fig), stats


def return_curve_chart(filepath, date_col='date', close_col='close', **kwargs):
    """
    收益曲线 — 累计收益率 + 日收益率柱状图
    """
    _check_imports()
    df = _load_data(filepath, date_col)
    
    col_map = {}
    for c in df.columns:
        cl = c.lower()
        if 'close' in cl or '收盘' in cl or '收' == c: col_map['close'] = c
        if 'date' in cl or '日期' in cl: col_map['date'] = c
    
    cc = col_map.get('close', close_col)
    dc = col_map.get('date', date_col)
    
    df['daily_return'] = df[cc].pct_change()
    df['cum_return'] = (1 + df['daily_return']).cumprod() - 1
    
    layout, clrs = _default_layout('收益曲线分析', **kwargs)
    
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        vertical_spacing=0.08, row_heights=[0.6, 0.4])
    
    # 上：累计收益
    fig.add_trace(go.Scatter(x=df[dc], y=df['cum_return'] * 100,
                             name='累计收益率',
                             fill='tozeroy',
                             line=dict(color='#4caf50', width=2),
                             fillcolor='rgba(76,175,80,0.15)',
                             hovertemplate='%{x}<br>累计收益: %{y:.2f}%<extra></extra>'),
                  row=1, col=1)
    
    # 标注最大回撤点
    max_drawdown = 0
    peak = df['cum_return'].iloc[0]
    for i, v in enumerate(df['cum_return']):
        if v > peak:
            peak = v
        dd = (peak - v) / (1 + peak)
        if dd > max_drawdown:
            max_drawdown = dd
            max_dd_idx = i
    
    fig.add_vline(x=df[dc].iloc[max_dd_idx], line_dash='dot',
                  line_color='#ef5350', line_width=1,
                  annotation_text=f'最大回撤点 ({max_drawdown*100:.1f}%)',
                  row=1, col=1)
    
    # 下：日收益率
    colors_bar = [clrs['up'] if r >= 0 else clrs['down'] for r in df['daily_return']]
    fig.add_trace(go.Bar(x=df[dc], y=df['daily_return'] * 100,
                         name='日收益率',
                         marker_color=colors_bar, opacity=0.7,
                         hovertemplate='%{x}<br>日收益: %{y:.2f}%<extra></extra>'),
                  row=2, col=1)
    
    fig.update_layout(layout, showlegend=False,
                      yaxis=dict(title='累计收益 (%)', tickformat='.1f'),
                      yaxis2=dict(title='日收益 (%)', tickformat='.1f'))
    fig.update_xaxes(title_text='日期', row=2, col=1)
    
    total_return = float(df['cum_return'].iloc[-1] * 100)
    annual_return = total_return / max(len(df) / 252, 0.1)
    
    stats = {
        'total_return_pct': round(total_return, 2),
        'annual_return_pct': round(annual_return, 2),
        'max_drawdown_pct': round(max_drawdown * 100, 2),
        'daily_volatility_pct': round(float(df['daily_return'].std() * 100), 2),
        'sharpe_ratio': round(float(df['daily_return'].mean() / df['daily_return'].std() * np.sqrt(252)), 2) if df['daily_return'].std() > 0 else 0,
        'positive_days': int((df['daily_return'] > 0).sum()),
        'negative_days': int((df['daily_return'] < 0).sum()),
    }
    return fig_to_html(fig), stats


def max_drawdown_chart(filepath, date_col='date', close_col='close', **kwargs):
    """
    最大回撤曲线（水下曲线）
    展示净值曲线和回撤幅度
    """
    _check_imports()
    df = _load_data(filepath, date_col)
    
    col_map = {}
    for c in df.columns:
        cl = c.lower()
        if 'close' in cl or '收盘' in cl or '收' == c: col_map['close'] = c
        if 'date' in cl or '日期' in cl: col_map['date'] = c
    
    cc = col_map.get('close', close_col)
    dc = col_map.get('date', date_col)
    
    # 计算回撤
    df['daily_return'] = df[cc].pct_change()
    df['cum_return'] = (1 + df['daily_return']).cumprod()
    df['peak'] = df['cum_return'].cummax()
    df['drawdown'] = (df['cum_return'] - df['peak']) / df['peak'] * 100
    
    layout, clrs = _default_layout('最大回撤分析 (水下曲线)', **kwargs)
    
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        vertical_spacing=0.08, row_heights=[0.5, 0.5])
    
    # 上：净值曲线
    fig.add_trace(go.Scatter(x=df[dc], y=df['cum_return'],
                             name='净值曲线',
                             line=dict(color='#4caf50', width=2),
                             hovertemplate='%{x}<br>净值: %{y:.4f}<extra></extra>'),
                  row=1, col=1)
    fig.add_trace(go.Scatter(x=df[dc], y=df['peak'],
                             name='历史峰值',
                             line=dict(color='#ffd54f', width=1, dash='dash')),
                  row=1, col=1)
    
    # 下：回撤曲线（水下）
    fig.add_trace(go.Scatter(x=df[dc], y=df['drawdown'],
                             name='回撤幅度',
                             fill='tozeroy',
                             line=dict(color='#ef5350', width=2),
                             fillcolor='rgba(239,83,80,0.2)',
                             hovertemplate='%{x}<br>回撤: %{y:.2f}%<extra></extra>'),
                  row=2, col=1)
    
    # 标注最大回撤
    max_dd_idx = df['drawdown'].idxmin()
    fig.add_vline(x=df[dc].iloc[max_dd_idx], line_dash='dot',
                  line_color='#ef5350',
                  annotation_text=f"最大回撤 {df['drawdown'].iloc[max_dd_idx]:.1f}%")
    
    fig.update_layout(layout, showlegend=True,
                      yaxis=dict(title='净值'),
                      yaxis2=dict(title='回撤 (%)'))
    fig.update_xaxes(title_text='日期', row=2, col=1)
    
    max_dd = float(df['drawdown'].min())
    max_dd_date = str(df[dc].iloc[max_dd_idx].date()) if hasattr(df[dc].iloc[max_dd_idx], 'date') else str(df[dc].iloc[max_dd_idx])
    
    stats = {
        'max_drawdown_pct': round(max_dd, 2),
        'max_drawdown_date': max_dd_date,
        'current_drawdown_pct': round(float(df['drawdown'].iloc[-1]), 2),
        'recovery_days': None,  # 简化处理
    }
    return fig_to_html(fig), stats


# ===================== 对比图 =====================

def comparison_chart(stock_files, metric='close', stock_names=None,
                     date_col='date', **kwargs):
    """
    多只股票同画布对比
    stock_files: [{'file': path, 'name': label}, ...]
    metric: 对比指标列名
    """
    _check_imports()
    
    layout, clrs = _default_layout('多股走势对比', **kwargs)
    
    fig = go.Figure()
    colors_palette = ['#4caf50', '#2196f3', '#ff9800', '#e91e63',
                      '#9c27b0', '#00bcd4', '#ff5722', '#607d8b']
    
    for i, sf in enumerate(stock_files):
        df = _load_data(sf['file'], date_col)
        
        col_map = {}
        for c in df.columns:
            cl = c.lower()
            if metric.lower() in cl or (metric == 'close' and ('收盘' in cl or '收' == c)):
                col_map['metric'] = c
            if 'date' in cl or '日期' in cl:
                col_map['date'] = c
        
        mc = col_map.get('metric', metric)
        dc = col_map.get('date', date_col)
        
        # 归一化到100做基准对比
        base_val = df[mc].iloc[0] if len(df) > 0 else 1
        normalized = df[mc] / base_val * 100
        
        name = stock_names[i] if stock_names and i < len(stock_names) else sf.get('name', f'股票{i+1}')
        color = colors_palette[i % len(colors_palette)]
        
        fig.add_trace(go.Scatter(
            x=df[dc], y=normalized,
            name=name,
            line=dict(width=2, color=color),
            hovertemplate='%{x}<br>' + name + ': %{y:.2f}<extra></extra>',
        ))
    
    fig.update_layout(
        layout,
        xaxis_title='日期',
        yaxis_title=f'{metric} (基准=100)',
        hovermode='x unified',
        legend=dict(
            orientation='h',
            yanchor='bottom',
            y=1.02,
            xanchor='right',
            x=1,
            font=dict(size=11),
        ),
    )
    
    return fig_to_html(fig)


def batch_export_charts(filepath, chart_types, output_format='png', **kwargs):
    """
    批量导出图表 — 生成多种 K 线指标图并打包为 zip
    注意：Plotly 静态导出需要 kaleido，这里使用 HTML 方式
    """
    _check_imports()
    
    zip_buffer = io.BytesIO()
    
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for chart_type in chart_types:
            try:
                if chart_type == 'money_flow':
                    html, _ = money_flow_chart(filepath, **kwargs)
                elif chart_type == 'chip':
                    html, _ = chip_distribution_chart(filepath, **kwargs)
                elif chart_type == 'heatmap':
                    html, _ = interactive_heatmap(filepath, **kwargs)
                elif chart_type == 'return_curve':
                    html, _ = return_curve_chart(filepath, **kwargs)
                elif chart_type == 'drawdown':
                    html, _ = max_drawdown_chart(filepath, **kwargs)
                else:
                    continue
                
                fname = f'{chart_type}.html'
                zf.writestr(fname, html)
            except Exception as e:
                zf.writestr(f'{chart_type}_error.txt', str(e))
    
    zip_buffer.seek(0)
    return zip_buffer


# ===================== 辅助函数 =====================

def get_available_stocks(output_dir):
    """获取可用的股票数据文件列表"""
    import glob, os
    files = sorted(
        glob.glob(str(output_dir / "*.csv")),
        key=os.path.getmtime, reverse=True
    )
    result = []
    for f in files:
        name = os.path.basename(f).replace('.csv', '')
        result.append({'file': f, 'name': name})
    return result
