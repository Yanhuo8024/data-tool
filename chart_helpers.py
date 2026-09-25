"""Chart helpers — improved label handling, Plotly interactive charts"""
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
from typing import List, Optional, Tuple


def calc_label_params(labels: list) -> dict:
    """根据标签数量和长度，自动计算 rotation、fontsize、是否用横向"""
    n = len(labels)
    avg_len = sum(len(str(v)) for v in labels) / max(n, 1)

    result = {
        "rotation": 0,
        "fontsize": 10,
        "ha": "center",
        "horizontal": False,
    }

    if n <= 5:
        return result

    if n <= 12:
        result["rotation"] = 30
        result["ha"] = "right"
    elif n <= 20:
        result["rotation"] = 45
        result["ha"] = "right"
        result["fontsize"] = 8
    else:
        result["rotation"] = 60
        result["ha"] = "right"
        result["fontsize"] = 6

    # 标签平均长度 > 8 字符 → 用横向图更清晰
    if avg_len > 8:
        result["horizontal"] = True
        result["rotation"] = 0
        result["ha"] = "left"
        result["fontsize"] = 9

    return result


def build_plotly_chart(df: pd.DataFrame, x_cols: List[str], y_cols: List[str],
                       chart_type: str = "scatter") -> str:
    """生成 Plotly 交互式图表的 HTML。

    返回完整的 HTML 片段，可直接嵌入 iframe 或页面。
    """
    if not x_cols and chart_type != "hist":
        return '<p style="color:red">请选择 X 轴列</p>'

    n_x, n_y = len(x_cols), len(y_cols)
    title = f"{' & '.join(x_cols[:3])} vs {' & '.join(y_cols[:3])}"

    if n_x == 1:
        xc = x_cols[0]

        if chart_type == "bar":
            if n_y == 0:
                return '<p style="color:red">请选择 Y 轴列</p>'
            yc = y_cols[0]
            bd = df.groupby(xc)[yc].mean().sort_values() if n_y == 1 else df.groupby(xc)[y_cols].mean()

            label_params = calc_label_params(list(bd.index))
            orient = 'h' if label_params["horizontal"] else 'v'

            if n_y == 1:
                fig = px.bar(x=bd.values if orient == 'h' else bd.index,
                            y=bd.index if orient == 'h' else bd.values,
                            orientation=orient, title=f'{xc} vs {yc}',
                            labels={'x': yc if orient == 'h' else xc,
                                    'y': xc if orient == 'h' else yc})
            else:
                fig = px.bar(bd, barmode='group', title=title)

            fig.update_layout(
                template='plotly_white',
                font=dict(family='Microsoft YaHei, SimHei, sans-serif'),
                height=max(400, 25 * len(bd)),
                margin=dict(l=150 if orient == 'h' else 50, r=30, t=50, b=80),
            )

        elif chart_type == "line":
            if n_y == 0:
                return '<p style="color:red">请选择 Y 轴列</p>'
            for i, yc in enumerate(y_cols):
                ps = df.groupby(xc)[yc].mean().sort_index()
                if i == 0:
                    fig = px.line(x=ps.index, y=ps.values, labels={'x': xc, 'y': yc})
                    fig.update_traces(name=yc, mode='lines+markers')
                else:
                    fig.add_scatter(x=ps.index, y=ps.values, mode='lines+markers',
                                    name=yc, line=dict(width=2), marker=dict(size=5))
            fig.update_layout(
                template='plotly_white',
                font=dict(family='Microsoft YaHei, SimHei, sans-serif'),
                title=f'折线图 ({xc})',
                height=450,
                margin=dict(l=50, r=30, t=50, b=80),
            )

        elif chart_type == "scatter":
            if n_y == 0:
                return '<p style="color:red">请选择 Y 轴列</p>'
            yc = y_cols[0]
            fig = px.scatter(df, x=xc, y=yc, title=f'{xc} vs {yc}',
                           opacity=0.6, size_max=12)
            fig.update_traces(marker=dict(size=8, color='#FF6B6B'))
            fig.update_layout(
                template='plotly_white',
                font=dict(family='Microsoft YaHei, SimHei, sans-serif'),
                height=450,
                margin=dict(l=50, r=30, t=50, b=50),
            )

        elif chart_type == "box":
            if n_y == 0:
                return '<p style="color:red">请选择 Y 轴列</p>'
            yc = y_cols[0]
            fig = px.box(df, x=xc, y=yc, title=f'{xc} vs {yc} 箱线图')
            fig.update_layout(
                template='plotly_white',
                font=dict(family='Microsoft YaHei, SimHei, sans-serif'),
                height=450,
                margin=dict(l=50, r=30, t=50, b=80),
            )

        elif chart_type == "hist":
            xc = x_cols[0] if x_cols else df.columns[0]
            fig = px.histogram(df, x=xc, nbins=30, title=f'{xc} 分布直方图',
                             opacity=0.8, color_discrete_sequence=['#FF6B6B'])
            fig.update_layout(
                template='plotly_white',
                font=dict(family='Microsoft YaHei, SimHei, sans-serif'),
                height=400,
                margin=dict(l=50, r=30, t=50, b=50),
            )

        else:
            # 默认散点图
            fig = px.scatter(df, x=xc, y=y_cols[0] if n_y > 0 else None, title=title)
            fig.update_layout(
                template='plotly_white',
                font=dict(family='Microsoft YaHei, SimHei, sans-serif'),
            )

        # 自动旋转 X 轴标签
        labels = list(df[xc].dropna().unique())[:50]
        lp = calc_label_params(labels)
        fig.update_xaxes(tickangle=lp["rotation"], tickfont=dict(size=lp["fontsize"]))

    else:
        # 多 X 列 → 子图
        fig = make_subplots(rows=n_x, cols=1, subplot_titles=x_cols,
                           vertical_spacing=0.08)
        for idx, xc in enumerate(x_cols):
            yc = y_cols[min(idx, n_y - 1)] if n_y > 0 else None
            if yc and yc in df.columns:
                fig.add_trace(
                    go.Scatter(x=df[xc], y=df[yc], mode='markers',
                              name=f'{xc} vs {yc}', marker=dict(size=6)),
                    row=idx + 1, col=1
                )
        fig.update_layout(
            template='plotly_white',
            font=dict(family='Microsoft YaHei, SimHei, sans-serif'),
            height=350 * n_x,
            showlegend=False,
            margin=dict(l=50, r=30, t=50, b=30),
        )

    return fig.to_html(
        include_plotlyjs='cdn',
        full_html=False,
        config={'displayModeBar': True, 'responsive': True, 'displaylogo': False}
    )
