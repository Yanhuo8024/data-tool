#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
factor_model.py — Fama-French 五因子模型（MKT / SMB / HML / RMW / CMA）

数据源：baostock（免费、无限频）
- 股票池：沪深主板 A 股（排除 ST / 次新股）
- 财务：query_profit_data（ROE/净利/总股本）、query_growth_data（总资产增速）
- 日线：query_history_k_data_plus（前复权）

因子构造（季度末截面分组，组内日频等权收益）：
- 市值 50% 分位 → S / B
- BM（净资产/市值，用 net_profit/roeAvg 推净资产）30%/70% → L / H
- 盈利（ROE）30%/70% → W / R
- 投资（总资产增速 YOYAsset）30%/70% → C / A
- SMB = (SH+SL+SR+SW+SC+SA)/6 - (BH+BL+BR+BW+BC+BA)/6
- HML = (SH+BH)/2 - (SL+BL)/2
- RMW = (SR+BR)/2 - (SW+BW)/2
- CMA = (SC+BC)/2 - (SA+BA)/2
- MKT = 全样本等权日收益
"""
import base64
from datetime import datetime, timedelta
from io import BytesIO

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import baostock as bs

TRADING_DAYS_PER_YEAR = 244


def _query_all(rs):
    rows = []
    while rs.next():
        rows.append(rs.get_row_data())
    return pd.DataFrame(rows, columns=rs.fields)


def _num(v):
    try:
        if v is None or str(v).strip() == '':
            return np.nan
        return float(v)
    except (TypeError, ValueError):
        return np.nan


# ------------------------------------------------------------------
# 数据获取
# ------------------------------------------------------------------

def fetch_stock_pool(max_stocks=40, exclude_st=True, min_listed_days=365):
    """沪深主板 A 股样本（排除 ST / 次新股），最多 max_stocks 只"""
    rs = bs.query_stock_basic()
    df = _query_all(rs)
    stocks = df[(df['type'] == '1') & (df['status'] == '1')].copy()
    # 沪深主板：sh.6 开头 / sz.0 开头
    stocks = stocks[stocks['code'].str.match(r'^(sh\.6|sz\.0)')]
    if exclude_st:
        stocks = stocks[~stocks['code_name'].str.contains('ST', na=False)]
    # 剔除次新股
    cutoff = (datetime.now() - timedelta(days=min_listed_days)).strftime('%Y-%m-%d')
    stocks = stocks[stocks['ipoDate'] <= cutoff]
    return stocks.head(max_stocks)


def fetch_stock_daily(code, start, end):
    """前复权日线：date/close/pctChg"""
    rs = bs.query_history_k_data_plus(
        code, 'date,close,pctChg,amount',
        start_date=start, end_date=end, frequency='d', adjustflag='2')
    if rs.error_code != '0':
        return None
    df = _query_all(rs)
    if df.empty:
        return None
    df['date'] = pd.to_datetime(df['date'])
    for col in ['close', 'pctChg', 'amount']:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    return df[['date', 'close', 'pctChg']].dropna(subset=['close'])


def fetch_stock_financials(code, quarters):
    """拉取若干 (year, quarter) 的财务数据，返回 DataFrame"""
    records = []
    for year, quarter in quarters:
        rs_p = bs.query_profit_data(code=code, year=year, quarter=quarter)
        if rs_p.error_code != '0':
            continue
        df_p = _query_all(rs_p)
        if df_p.empty:
            continue
        row = df_p.iloc[-1]
        yoy_asset = np.nan
        rs_g = bs.query_growth_data(code=code, year=year, quarter=quarter)
        if rs_g.error_code == '0':
            df_g = _query_all(rs_g)
            if not df_g.empty:
                yoy_asset = _num(df_g.iloc[-1].get('YOYAsset'))
        records.append({
            'stat_date': str(row['statDate']),
            'pub_date': str(row['pubDate']),
            'roe': _num(row.get('roeAvg')),
            'net_profit': _num(row.get('netProfit')),
            'total_share': _num(row.get('totalShare')),
            'yoy_asset': yoy_asset,
        })
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records)
    df['stat_date'] = pd.to_datetime(df['stat_date'])
    df['pub_date'] = pd.to_datetime(df['pub_date'])
    return df


def _quarters_between(start_date, end_date):
    """start_date~end_date 覆盖的 (year, quarter) 集合（含前推一个季度保证数据可得）"""
    years = set()
    for d in [start_date, end_date]:
        years.add(d.year)
    if start_date.month <= 3:
        years.add(start_date.year - 1)
    quarters = []
    for y in sorted(years):
        for q in (1, 2, 3, 4):
            d = datetime(y, q * 3, 1)
            if start_date - timedelta(days=120) <= d <= end_date:
                quarters.append((y, q))
    return quarters[-8:]  # 最多 8 个季度


# ------------------------------------------------------------------
# 因子构造
# ------------------------------------------------------------------

def _group_breakpoints(s):
    """30%/70% 分位（含下限/上限兜底）"""
    q30, q70 = s.quantile(0.3), s.quantile(0.7)
    return q30, q70


def _quarter_end_dates(daily_dates):
    """给定交易日序列，返回每个季度最后一个交易日（作为分组日）"""
    s = pd.Series(sorted(set(daily_dates)))
    groups = s.groupby([s.dt.year, s.dt.quarter])
    return [g.iloc[-1] for _, g in groups]


def build_factor_returns(stock_data, start_date, end_date):
    """
    stock_data: {code: {'daily': df(date,close,pctChg), 'fin': df(stat_date,pub_date,roe,net_profit,total_share,yoy_asset)}}
    返回: (factor_returns: DataFrame[date, MKT,SMB,HML,RMW,CMA], info: dict)
    """
    # 统一交易日历
    all_dates = []
    for code, d in stock_data.items():
        if d.get('daily') is not None and not d['daily'].empty:
            all_dates.extend(d['daily']['date'].tolist())
    all_dates = sorted(set(all_dates))
    if len(all_dates) < 30:
        raise ValueError("有效交易日不足（<30 天），请扩大时间范围")

    # 每日收益矩阵：date × code
    ret_matrix = {}
    for code, d in stock_data.items():
        daily = d.get('daily')
        if daily is None or daily.empty:
            continue
        ret_matrix[code] = daily.set_index('date')['pctChg'] / 100.0
    ret_df = pd.DataFrame(ret_matrix).reindex(all_dates)

    # 每日收盘价矩阵（算市值用）
    close_matrix = {}
    for code, d in stock_data.items():
        daily = d.get('daily')
        if daily is None or daily.empty:
            continue
        close_matrix[code] = daily.set_index('date')['close']
    close_df = pd.DataFrame(close_matrix).reindex(all_dates)

    # 分组日 = 季度最后交易日（去掉首个，保证每组有下一期收益）
    group_dates = _quarter_end_dates(all_dates)
    group_dates = [g for g in group_dates if start_date <= g <= end_date]
    if len(group_dates) < 2:
        raise ValueError("分组期不足（<2 个季度末），请扩大时间范围")

    # 每只股票：财务按 pub_date <= 分组日 的最新一期
    factor_segments = []  # 每个分段的日频因子收益

    for i in range(len(group_dates) - 1):
        t0, t1 = group_dates[i], group_dates[i + 1]
        # ---- 截面指标 ----
        codes, mcap, bm, roe, invest = [], [], [], [], []
        for code, d in stock_data.items():
            daily = d.get('daily')
            fin = d.get('fin')
            if daily is None or fin is None or daily.empty or fin.empty:
                continue
            # 最新已披露财务（pub_date <= t0）
            avail = fin[fin['pub_date'] <= t0]
            if avail.empty:
                continue
            frow = avail.iloc[-1]
            # 当日收盘
            try:
                price = close_df.loc[t0, code]
            except KeyError:
                continue
            if pd.isna(price):
                continue
            m = frow['total_share'] * price if not pd.isna(frow['total_share']) else np.nan
            r = frow['roe'] if not pd.isna(frow['roe']) else np.nan
            np_ = frow['net_profit'] if not pd.isna(frow['net_profit']) else np.nan
            # 净资产 ≈ 净利润 / ROE（ROE 为平均净资产收益率）
            bm_val = np.nan
            if not pd.isna(m) and m > 0 and not pd.isna(r) and r != 0 and not pd.isna(np_):
                equity = np_ / r
                bm_val = equity / m
            yoy = frow['yoy_asset'] if not pd.isna(frow['yoy_asset']) else np.nan
            codes.append(code)
            mcap.append(m)
            bm.append(bm_val)
            roe.append(r)
            invest.append(yoy)

        if len(codes) < 6:
            continue
        cross = pd.DataFrame({'code': codes, 'mcap': mcap, 'bm': bm, 'roe': roe, 'invest': invest})

        # ---- 分组 ----
        def _label(series, q30, q70):
            return pd.cut(series, bins=[-np.inf, q30, q70, np.inf], labels=[0, 1, 2])

        m_med = cross['mcap'].median()
        cross['size'] = np.where(cross['mcap'] >= m_med, 'B', 'S')
        if cross['bm'].notna().sum() >= 6:
            b30, b70 = _group_breakpoints(cross['bm'].dropna())
            cross['bm_g'] = _label(cross['bm'], b30, b70)
        else:
            cross['bm_g'] = 1
        if cross['roe'].notna().sum() >= 6:
            r30, r70 = _group_breakpoints(cross['roe'].dropna())
            cross['roe_g'] = _label(cross['roe'], r30, r70)
        else:
            cross['roe_g'] = 1
        if cross['invest'].notna().sum() >= 6:
            i30, i70 = _group_breakpoints(cross['invest'].dropna())
            cross['inv_g'] = _label(cross['invest'], i30, i70)
        else:
            cross['inv_g'] = 1

        # ---- 区间收益（t0 之后的交易日，含 t0 当日收益） ----
        seg_ret = ret_df.loc[t0:t1, :]
        if seg_ret.empty:
            continue

        # 2x3 组合（市值 × BM）
        combos = {}
        for size_lbl in ['S', 'B']:
            for g_lbl in [0, 1, 2]:
                key = f"{size_lbl}_{g_lbl}"
                mem = cross[(cross['size'] == size_lbl) & (cross['bm_g'] == g_lbl)]['code'].tolist()
                combos[key] = seg_ret[mem].mean(axis=1) if mem else pd.Series(np.nan, index=seg_ret.index)
        # SMB / HML（Fama-French 5 因子中 SMB 用三组合成）
        SMB = (combos['S_0'] + combos['S_1'] + combos['S_2']) / 3 - (combos['B_0'] + combos['B_1'] + combos['B_2']) / 3
        HML = (combos['S_0'] + combos['B_0']) / 2 - (combos['S_2'] + combos['B_2']) / 2

        # 市值 × 盈利（RMW）
        combos_r = {}
        for size_lbl in ['S', 'B']:
            for g_lbl in [0, 1, 2]:
                mem = cross[(cross['size'] == size_lbl) & (cross['roe_g'] == g_lbl)]['code'].tolist()
                combos_r[f"{size_lbl}_{g_lbl}"] = seg_ret[mem].mean(axis=1) if mem else pd.Series(np.nan, index=seg_ret.index)
        RMW = (combos_r['S_2'] + combos_r['B_2']) / 2 - (combos_r['S_0'] + combos_r['B_0']) / 2

        # 市值 × 投资（CMA）
        combos_i = {}
        for size_lbl in ['S', 'B']:
            for g_lbl in [0, 1, 2]:
                mem = cross[(cross['size'] == size_lbl) & (cross['inv_g'] == g_lbl)]['code'].tolist()
                combos_i[f"{size_lbl}_{g_lbl}"] = seg_ret[mem].mean(axis=1) if mem else pd.Series(np.nan, index=seg_ret.index)
        CMA = (combos_i['S_0'] + combos_i['B_0']) / 2 - (combos_i['S_2'] + combos_i['B_2']) / 2

        # 市场因子：全样本等权
        MKT = seg_ret.mean(axis=1)

        seg = pd.DataFrame({
            'MKT': MKT, 'SMB': SMB, 'HML': HML, 'RMW': RMW, 'CMA': CMA,
        }, index=seg_ret.index)
        factor_segments.append(seg)

    if not factor_segments:
        raise ValueError("未能构造任何因子组合，请检查数据量")

    factor_returns = pd.concat(factor_segments)
    factor_returns = factor_returns[~factor_returns.index.duplicated()].sort_index()
    factor_returns = factor_returns.dropna(how='all')
    factor_returns = factor_returns.fillna(0.0)

    info = {
        'stocks': len(stock_data),
        'groups': len(group_dates) - 1,
        'start': str(factor_returns.index.min().date()),
        'end': str(factor_returns.index.max().date()),
        'days': len(factor_returns),
    }
    return factor_returns, info


# ------------------------------------------------------------------
# 统计与图表
# ------------------------------------------------------------------

def factor_statistics(factor_returns, rf_annual=0.02):
    """因子统计：均值 / 年化 / 波动 / t 值"""
    rf_daily = rf_annual / TRADING_DAYS_PER_YEAR
    stats = []
    for col in factor_returns.columns:
        s = factor_returns[col] - rf_daily
        n = s.notna().sum()
        mean = s.mean()
        std = s.std()
        tval = mean / (std / np.sqrt(n)) if std and std > 0 and n > 1 else 0.0
        stats.append({
            'factor': col,
            'mean_daily': round(float(mean), 5),
            'annual': round(float(mean) * TRADING_DAYS_PER_YEAR, 4),
            'std_daily': round(float(std), 5),
            'annual_vol': round(float(std) * np.sqrt(TRADING_DAYS_PER_YEAR), 4),
            't_value': round(float(tval), 3),
            'obs': int(n),
        })
    return stats


def cumulative_curve_chart(factor_returns):
    """因子累计收益曲线（base64 PNG）"""
    cum = (1 + factor_returns).cumprod()
    fig, ax = plt.subplots(figsize=(10, 5))
    fig.patch.set_facecolor('#0f111c')
    ax.set_facecolor('#151725')
    colors = {'MKT': '#f87171', 'SMB': '#60a5fa', 'HML': '#fbbf24', 'RMW': '#34d399', 'CMA': '#a78bfa'}
    for col in cum.columns:
        ax.plot(cum.index, cum[col], label=col, color=colors.get(col, '#e0e0e0'), linewidth=1.6)
    ax.axhline(1.0, color='#3a3f52', linestyle='--', linewidth=0.8)
    ax.legend(fontsize=9, facecolor='#151725', labelcolor='#e0e0e0')
    ax.set_title('Fama-French 五因子累计收益', color='#e0e0e0', fontsize=12)
    ax.tick_params(colors='#a0a0b0', labelsize=9)
    ax.grid(True, alpha=0.15)
    plt.tight_layout()
    buf = BytesIO()
    fig.savefig(buf, format='png', dpi=110, bbox_inches='tight')
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def correlation_html(factor_returns):
    """因子相关系数表（HTML）"""
    corr = factor_returns.corr().round(3)
    html = '<table><tr><th></th>' + ''.join(f'<th>{c}</th>' for c in corr.columns) + '</tr>'
    for idx, row in corr.iterrows():
        html += f'<tr><td><b>{idx}</b></td>' + ''.join(f'<td>{v}</td>' for v in row) + '</tr>'
    html += '</table>'
    return html


# ------------------------------------------------------------------
# 主入口
# ------------------------------------------------------------------

def run_fama_french(max_stocks=40, months=12, rf_annual=0.02):
    """运行 Fama-French 五因子模型"""
    end = datetime.now()
    start = end - timedelta(days=int(months) * 31)

    lg = bs.login()
    if lg.error_code != '0':
        return {"error": f"baostock 登录失败: {lg.error_msg}", "status": "failed"}
    try:
        pool = fetch_stock_pool(max_stocks=max_stocks)
        if pool.empty:
            return {"error": "未获取到股票池", "status": "failed"}

        quarters = _quarters_between(start, end)
        stock_data = {}
        start_str, end_str = start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d')
        skipped = 0
        for _, row in pool.iterrows():
            code = row['code']
            try:
                daily = fetch_stock_daily(code, start_str, end_str)
                if daily is None or len(daily) < 20:
                    skipped += 1
                    continue
                fin = fetch_stock_financials(code, quarters)
                if fin.empty:
                    skipped += 1
                    continue
                stock_data[code] = {'daily': daily, 'fin': fin}
            except Exception:
                skipped += 1

        if len(stock_data) < 10:
            return {"error": f"有效股票不足（{len(stock_data)}<10），请检查网络或调整参数", "status": "failed"}

        factor_returns, info = build_factor_returns(stock_data, start, end)
        stats = factor_statistics(factor_returns, rf_annual)
        chart = cumulative_curve_chart(factor_returns)
        corr_html = correlation_html(factor_returns)

        return {
            "ok": True,
            "info": {**info, 'skipped': skipped},
            "factors": factor_returns.reset_index().rename(columns={'index': 'date'}).to_dict('records')[-120:],
            "stats": stats,
            "chart": chart,
            "corr_html": corr_html,
        }
    finally:
        bs.logout()
