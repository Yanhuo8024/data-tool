# -*- coding: utf-8 -*-
"""
财务报表分析模块 — 利润表/资产负债表/现金流量表 + 财务比率 + 杜邦分析 + 可视化
数据源：akshare (sina + THS)
"""

import io, base64, traceback
import pandas as pd
import numpy as np

from finance_data import normalize_hk_code

try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib import rcParams
    rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'DejaVu Sans']
    rcParams['axes.unicode_minus'] = False
    HAS_MPL = True
except ImportError:
    HAS_MPL = False


def _get_ak():
    try:
        import akshare as ak
        return ak
    except ImportError:
        return None


def _resolve_stock_code(symbol):
    """解析股票代码，返回 (sina_code, raw_code, market)
    market: 'a' A股 / 'hk' 港股（支持 HK+4位 如 HK0700，或 5位 如 00700）"""
    s = symbol.strip()
    s = s.replace('.SH', '').replace('.SZ', '').replace('.HK', '').replace('.hk', '')
    s = s.replace('sh.', '').replace('sz.', '').replace('hk.', '')
    s = s.replace('SH', '').replace('SZ', '').replace('HK', '')
    # 港股：HK+4位（HK0700/HK9988）或 5位纯数字（00700）
    hk = normalize_hk_code(s)
    if hk:
        return 'hk' + hk, hk, 'hk'
    # 6开头是上海, 0/3/2开头是深圳
    if s.startswith('6'):
        sina = 'sh' + s
    else:
        sina = 'sz' + s
    return sina, s, 'a'


def _resolve_stock_name(market, raw_code, sina_code=None):
    """根据代码查询股票名称（A股/港股），失败返回空字符串（不阻塞主流程）"""
    ak = _get_ak()
    if ak is None:
        return ""
    try:
        if market == "hk":
            # 港股无统一的个股信息接口，best-effort 从行情快照表匹配
            try:
                df = ak.stock_hk_spot_em()
                code_col = "代码" if "代码" in df.columns else df.columns[0]
                name_col = "名称" if "名称" in df.columns else (df.columns[1] if len(df.columns) > 1 else None)
                if name_col:
                    hit = df[df[code_col].astype(str).str.zfill(5).str.endswith(raw_code)]
                    if not hit.empty:
                        return str(hit.iloc[0][name_col])
            except Exception:
                pass
            return ""
        info = ak.stock_individual_info_em(symbol=raw_code)
        if isinstance(info, dict):
            return (info.get("股票简称") or info.get("股票名称")
                    or info.get("name") or "")
    except Exception:
        pass
    return ""


def fetch_financial_data(symbol):
    """拉取财务报表数据：利润表 + 资产负债表 + 现金流量表（支持 A股 / 港股）"""
    ak = _get_ak()
    if ak is None:
        return {"error": "akshare 未安装，请 pip install akshare"}

    sina_code, raw_code, market = _resolve_stock_code(symbol)

    # --- 港股：东方财富数据中心三大报表 ---
    if market == "hk":
        return _fetch_financial_data_hk(ak, raw_code, sina_code)

    results = {}

    # --- 利润表 (Sina) ---
    try:
        income = ak.stock_financial_report_sina(stock=sina_code, symbol="利润表")
        if income is not None and not income.empty:
            results["income"] = _records_from_df(income)
    except Exception:
        pass

    # --- 资产负债表 (Sina) ---
    try:
        balance = ak.stock_financial_report_sina(stock=sina_code, symbol="资产负债表")
        if balance is not None and not balance.empty:
            results["balance"] = _records_from_df(balance)
    except Exception:
        pass

    # --- 现金流量表 (Sina) ---
    try:
        cashflow = ak.stock_financial_report_sina(stock=sina_code, symbol="现金流量表")
        if cashflow is not None and not cashflow.empty:
            results["cashflow"] = _records_from_df(cashflow)
    except Exception:
        pass

    if not results:
        return {"error": f"未获取到 {raw_code} 的财务数据，请检查代码是否正确"}

    stock_name = _resolve_stock_name("a", raw_code, sina_code)
    return {"ok": True, "symbol": raw_code, "sina_code": sina_code, "market": "a",
            "stock_name": stock_name, "data": results}


# ---- 港股财报（东方财富数据中心，年度口径） ----

def _fetch_financial_data_hk(ak, code, sina_code):
    """港股三大报表：stock_financial_hk_report_em（长表 → 透视宽表）"""
    sheets = [("利润表", "income"), ("资产负债表", "balance"), ("现金流量表", "cashflow")]
    results = {}
    for sheet, key in sheets:
        try:
            df = ak.stock_financial_hk_report_em(stock=code, symbol=sheet, indicator="年度")
            if df is None or df.empty:
                continue
            df = df.copy()
            df["REPORT_DATE"] = df["REPORT_DATE"].astype(str).str[:10]
            piv = df.pivot_table(index="REPORT_DATE", columns="STD_ITEM_NAME",
                                 values="AMOUNT", aggfunc="first").reset_index()
            piv = piv.rename(columns={"REPORT_DATE": "报告日"})
            piv = piv.sort_values("报告日", ascending=False)
            records = _records_from_df(piv, keep_all=True)
            if records:
                results[key] = records
        except Exception:
            continue
    if not results:
        return {"error": f"未获取到 {code} 的港股财务数据，请检查代码是否正确（5位，如00700）"}
    stock_name = _resolve_stock_name("hk", code, sina_code)
    return {"ok": True, "symbol": code, "sina_code": sina_code, "market": "hk",
            "stock_name": stock_name, "data": results}


def _calc_ratios_hk(data):
    """港股财务比率（东方财富数据中心财务指标，取最新报告期）"""
    ak = _get_ak()
    if ak is None:
        return {"error": "akshare 未安装"}
    raw = data.get("symbol", "")
    raw = normalize_hk_code(raw) or raw
    if not raw:
        return {"error": "无效的股票代码"}
    df = ak.stock_financial_hk_analysis_indicator_em(symbol=raw)
    if df is None or df.empty:
        return {"error": "无法获取港股财务指标"}
    df = df.sort_values("REPORT_DATE")  # 升序，末行为最新报告期
    r = df.iloc[-1]

    def f(key, default=0.0):
        v = r.get(key)
        if v is None:
            return default
        try:
            return float(v)
        except (ValueError, TypeError):
            return default

    debt = f("DEBT_ASSET_RATIO")
    net_margin_pct = f("NET_PROFIT_RATIO")
    roe = f("ROE_YEARLY") if f("ROE_YEARLY") else f("ROE_AVG")
    em = round(1 / (1 - debt / 100), 2) if debt < 100 else 0.0
    ato = round(roe / max(net_margin_pct * em, 0.001), 2) if net_margin_pct > 0 and em > 0 else 0.0

    ratios = {
        "profitability": {
            "revenue": round(f("OPERATE_INCOME") / 1e8, 2),
            "net_profit": round(f("HOLDER_PROFIT") / 1e8, 2),
            "net_margin": round(net_margin_pct, 2),
            "gross_margin": round(f("GROSS_PROFIT_RATIO"), 2),
            "roe": round(roe, 2),
            "eps": round(f("BASIC_EPS"), 2),
        },
        "solvency": {
            "debt_ratio": round(debt, 2),
            "equity_ratio": round(100 - debt, 2),
            "current_ratio": round(f("CURRENT_RATIO"), 2),
        },
        "growth": {
            "revenue_growth": round(f("OPERATE_INCOME_YOY"), 2),
            "profit_growth": round(f("HOLDER_PROFIT_YOY"), 2),
        },
        "operational": {},
        "dupont": {
            "roe": round(roe, 2),
            "net_margin": round(net_margin_pct, 2),
            "asset_turnover": ato,
            "equity_multiplier": em,
        },
    }
    return {"ok": True, "ratios": ratios}


def calculate_ratios(data):
    """从报表数据中提取关键财务比率（A股走同花顺摘要，港股走东财指标）"""
    try:
        if data.get("market") == "hk":
            return _calc_ratios_hk(data)

        ak = _get_ak()
        if ak is None:
            return {"error": "akshare 未安装"}

        # 从 data 中获取原始代码
        raw_code = data.get("symbol", "")
        sina_code = data.get("sina_code", "")
        if not raw_code:
            return {"error": "无效的股票代码"}

        # 使用 THS 财务摘要数据获取已计算的比率
        ths = ak.stock_financial_abstract_ths(symbol=raw_code, indicator="按报告期")
        if ths is None or ths.empty:
            return {"error": "无法获取财务指标数据"}

        latest = ths.iloc[-1]  # 最新报告期（最后一行）

        def val(key, default=0):
            v = latest.get(key, default)
            if v is None or v == 'False' or v == '':
                return default
            try:
                # 处理百分比字符串如 "35.49%"
                if isinstance(v, str) and v.endswith('%'):
                    return float(v.replace('%', ''))
                # 处理带单位的如 "1787.53万"
                if isinstance(v, str) and '万' in v:
                    return float(v.replace('万', '').replace(',', '')) * 10000
                if isinstance(v, str) and '亿' in v:
                    return float(v.replace('亿', '').replace(',', '')) * 1e8
                return float(v)
            except (ValueError, TypeError):
                return default

        ratios = {
            "profitability": {},
            "solvency": {},
            "growth": {},
            "operational": {},
            "dupont": {},
        }

        # 盈利能力
        ratios["profitability"]["revenue"] = round(val("营业总收入", 0) / 1e8, 2)
        ratios["profitability"]["net_profit"] = round(val("净利润", 0) / 1e8, 2)
        ratios["profitability"]["net_margin"] = round(val("销售净利率", 0), 2)
        ratios["profitability"]["gross_margin"] = round(val("销售毛利率", 0), 2)
        ratios["profitability"]["roe"] = round(val("净资产收益率", 0), 2)
        ratios["profitability"]["roe_diluted"] = round(val("净资产收益率-摊薄", 0), 2)
        ratios["profitability"]["eps"] = round(val("基本每股收益", 0), 2)

        # 偿债能力
        ratios["solvency"]["current_ratio"] = round(val("流动比率", 0), 2)
        ratios["solvency"]["quick_ratio"] = round(val("速动比率", 0), 2)
        ratios["solvency"]["debt_ratio"] = round(val("资产负债率", 0), 2)
        ratios["solvency"]["equity_ratio"] = round(100 - val("资产负债率", 0), 2)

        # 成长能力
        ratios["growth"]["revenue_growth"] = round(val("营业总收入同比增长率", 0), 2)
        ratios["growth"]["profit_growth"] = round(val("净利润同比增长率", 0), 2)

        # 营运能力
        ratios["operational"]["inventory_turnover"] = round(val("存货周转率", 0), 2)
        ratios["operational"]["inventory_days"] = round(val("存货周转天数", 0), 2)
        ratios["operational"]["receivable_days"] = round(val("应收账款周转天数", 0), 2)

        # 杜邦分析: ROE = 净利润率 x 总资产周转率 x 权益乘数
        # 从已有数据近似：权益乘数 = 1/(1-资产负债率)
        net_margin = ratios["profitability"].get("net_margin", 0) / 100
        debt_pct = ratios["solvency"].get("debt_ratio", 50)
        em = round(1 / (1 - debt_pct / 100), 2) if debt_pct < 100 else 0
        # 反推资产周转率 = ROE / (净利率 * 权益乘数)
        roe_pct = ratios["profitability"].get("roe", 0)
        ato = round(roe_pct / max(net_margin * em * 100, 0.001), 2) if net_margin > 0 and em > 0 else 0

        ratios["dupont"] = {
            "roe": round(roe_pct, 2),
            "net_margin": round(net_margin * 100, 2),
            "asset_turnover": ato,
            "equity_multiplier": em,
        }

        return {"ok": True, "ratios": ratios}

    except Exception as e:
        return {"error": str(e), "trace": traceback.format_exc()}


def _records_from_df(df, keep_all=False):
    """将 DataFrame 转为字典列表，大数字自动转换为万/亿单位"""
    if df is None or df.empty:
        return []

    # 优先显示的关键列
    key_cols = [
        "报告日", "报告期",
        # 利润表
        "营业总收入", "营业收入", "营业成本", "净利润", "利润总额",
        "营业利润", "销售费用", "管理费用", "财务费用", "研发费用",
        "基本每股收益", "稀释每股收益",
        # 资产负债表
        "资产总计", "负债合计", "货币资金", "流动资产合计",
        "非流动资产合计", "流动负债合计", "非流动负债合计",
        "所有者权益合计", "所有者权益(或股东权益)合计", "实收资本", "未分配利润", "固定资产",
        "存货", "应收账款", "无形资产", "商誉",
        "短期借款", "一年内到期的非流动负债", "长期借款", "应付债券",
        # 现金流量表
        "经营活动产生的现金流量净额", "投资活动产生的现金流量净额",
        "筹资活动产生的现金流量净额",
        "购建固定资产、无形资产和其他长期资产支付的现金",
        "购建固定资产、无形资产和其他长期资产所支付的现金",
        "分配股利、利润或偿付利息支付的现金",
        "分配股利、利润或偿付利息所支付的现金",
        # 港股科目（东方财富）
        "营业额", "毛利", "股东应占溢利", "每股基本盈利", "每股摊薄盈利",
        "总资产", "总负债", "股东权益", "现金及等价物",
        "经营业务现金净额", "经营溢利", "除税前溢利",
        "销售及分销费用", "行政开支", "融资成本", "折旧及摊销",
    ]
    if keep_all:
        keep_cols = list(df.columns)
    else:
        keep_cols = [c for c in key_cols if c in df.columns]
        if len(keep_cols) < 3:
            keep_cols = list(df.columns[:10])

    subset = df[keep_cols].head(20).copy()

    # 数值列统一按量纲自动换算单位（消除一长串 0）
    for col in subset.columns:
        col_values = subset[col]
        numeric = pd.to_numeric(col_values, errors='coerce')
        if numeric.isna().all():
            continue  # 非数值列（如日期字符串 YYYY-MM-DD）保持原样

        # 报告日/报告期/日期类列：按日期码展示，不做金额单位换算
        if any(kw in col for kw in ['报告日', '报告期', '日期', '时间', 'REPORT', 'DATE']):
            def _fmt_period(x):
                if pd.isna(x):
                    return ''
                try:
                    iv = int(round(float(x)))
                except Exception:
                    return str(x)
                s = str(iv)
                if iv > 19000000 and len(s) >= 8:      # YYYYMMDD
                    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
                if iv > 190000 and len(s) >= 6:        # YYYYMM
                    return f"{s[:4]}-{s[4:6]}"
                return str(iv)
            subset[col] = numeric.apply(_fmt_period)
            continue

        max_val = numeric.abs().max()
        if max_val >= 1e8:
            subset[col] = numeric.apply(lambda x: f"{x/1e8:.2f}亿" if pd.notna(x) else '')
        elif max_val >= 1e4:
            subset[col] = numeric.apply(lambda x: f"{x/1e4:.2f}万" if pd.notna(x) else '')
        else:
            subset[col] = numeric.apply(lambda x: f"{x:.2f}" if pd.notna(x) else '')

    records = []
    for _, row in subset.iterrows():
        records.append({str(k): str(v) if pd.notna(v) else '' for k, v in row.items()})
    return records


def generate_charts(data):
    """生成财务报表分析图表"""
    if not HAS_MPL:
        return None

    ratios = data.get("ratios", {})
    charts = {}

    try:
        # ========== 1. 综合雷达图 ==========
        radar_data = {}
        for cat in ["profitability", "solvency", "growth", "operational"]:
            for k, v in ratios.get(cat, {}).items():
                if isinstance(v, (int, float)) and v != 0:
                    radar_data[k] = v

        if radar_data:
            fig, ax = plt.subplots(figsize=(7, 7), subplot_kw=dict(polar=True))
            categories = list(radar_data.keys())
            values = [abs(radar_data[k]) for k in categories]
            N = len(categories)
            angles = [n / N * 2 * np.pi for n in range(N)]
            values += values[:1]
            angles += angles[:1]

            ax.fill(angles, values, alpha=0.25, color='#FF6B6B')
            ax.plot(angles, values, 'o-', color='#FF6B6B', linewidth=2)
            ax.set_xticks(angles[:-1])
            ax.set_xticklabels(categories, fontsize=9, color='#a0a5b8')
            ax.set_yticklabels([])
            ax.set_title('财务指标综合雷达图', fontsize=14, color='#e8e9ec', pad=20)
            ax.set_facecolor('#0a0a0f')
            fig.patch.set_facecolor('#0a0a0f')

            buf = io.BytesIO()
            fig.savefig(buf, format='png', dpi=120, bbox_inches='tight',
                       facecolor='#0a0a0f', edgecolor='none')
            buf.seek(0)
            charts["radar"] = base64.b64encode(buf.read()).decode()
            plt.close(fig)

        # ========== 2. 盈利能力柱状图 ==========
        profitability = ratios.get("profitability", {})
        p_keys = ['net_margin', 'roa', 'roe']
        p_labels = ['净利率', 'ROA', 'ROE']
        p_vals = [profitability.get(k, 0) for k in p_keys if profitability.get(k, 0) != 0]
        p_labs = [l for k, l in zip(p_keys, p_labels) if profitability.get(k, 0) != 0]

        if p_vals:
            fig, ax = plt.subplots(figsize=(8, 4))
            ax.barh(range(len(p_labs)), p_vals, color=['#FF6B6B', '#8b5cf6', '#10b981'][:len(p_labs)])
            ax.set_yticks(range(len(p_labs)))
            ax.set_yticklabels(p_labs, fontsize=11, color='#e8e9ec')
            ax.set_xlabel('%', color='#a0a5b8')
            for i, v in enumerate(p_vals):
                ax.text(v + max(p_vals)*0.02, i, f'{v:.1f}%', fontsize=10, color='#a0a5b8')
            ax.set_title('盈利能力指标', fontsize=14, color='#e8e9ec')
            ax.set_facecolor('#0a0a0f')
            fig.patch.set_facecolor('#0a0a0f')
            ax.spines['top'].set_visible(False)
            ax.spines['right'].set_visible(False)
            ax.spines['left'].set_color('#333')
            ax.spines['bottom'].set_color('#333')
            ax.tick_params(colors='#a0a5b8')

            buf = io.BytesIO()
            fig.savefig(buf, format='png', dpi=120, bbox_inches='tight',
                       facecolor='#0a0a0f', edgecolor='none')
            buf.seek(0)
            charts["profitability"] = base64.b64encode(buf.read()).decode()
            plt.close(fig)

    except Exception as e:
        charts["error"] = str(e)

    return charts
