# -*- coding: utf-8 -*-
"""
估值分析模块 — 绝对估值（DCF 现金流折现）+ 相对估值（可比公司分析）
用于判断公司股价是否合理。
数据源：A股-新浪日K+同花顺摘要+新浪报表；港股-新浪日K+东财数据中心。
"""
import traceback
from concurrent.futures import ThreadPoolExecutor

from finance_data import normalize_hk_code


def _get_ak():
    try:
        import akshare as ak
        return ak
    except ImportError:
        return None


def _num(v, default=0.0):
    """稳健数值解析：兼容字符串带 万/亿/% 单位、逗号、-- 等"""
    if v is None:
        return default
    try:
        if isinstance(v, str):
            s = v.strip().replace(",", "")
            if not s or s in ("--", "-", "False", "None", "nan"):
                return default
            mult = 1.0
            if "亿" in s:
                mult, s = 1e8, s.replace("亿", "")
            elif "万" in s:
                mult, s = 1e4, s.replace("万", "")
            s = s.replace("%", "").strip()
            return float(s) * mult
        return float(v)
    except (ValueError, TypeError):
        return default


def _resolve(symbol):
    """返回 (market, raw_code) — a: A股 / hk: 港股（5位）"""
    s = symbol.strip()
    s = s.replace('.SH', '').replace('.SZ', '').replace('.HK', '').replace('.hk', '')
    s = s.replace('sh.', '').replace('sz.', '').replace('hk.', '')
    s = s.replace('SH', '').replace('SZ', '').replace('HK', '')
    hk = normalize_hk_code(s)
    if hk:
        return 'hk', hk
    return 'a', s


def _get_price_shares(ak, market, code):
    """返回 (现价, 总股本股)。A股股本取日K outstanding_share，港股股本由股东权益/BPS推算"""
    if market == 'a':
        sina = ('sh' if code.startswith('6') else 'sz') + code
        df = ak.stock_zh_a_daily(symbol=sina, adjust="")
        last = df.iloc[-1]
        return float(last["close"]), float(last.get("outstanding_share", 0) or 0)
    df = ak.stock_hk_daily(symbol=code, adjust="")
    last = df.iloc[-1]
    return float(last["close"]), 0.0  # 股本稍后在快照中推算


def _snapshot(symbol):
    """获取一只股票的基本面快照（指标 + 现价 + 股本 + 净负债）"""
    ak = _get_ak()
    market, code = _resolve(symbol)
    sn = {"market": market, "code": code, "price": None, "shares": None,
          "revenue": None, "net_profit": None, "revenue_growth": None,
          "profit_growth": None, "eps": None, "bps": None, "roe": None,
          "gross_margin": None, "net_margin": None, "debt_ratio": None,
          "ocf_ps": None, "net_debt": None, "name": code}

    if market == 'a':
        sina = ('sh' if code.startswith('6') else 'sz') + code
        # 现价 + 股本
        try:
            df = ak.stock_zh_a_daily(symbol=sina, adjust="")
            sn["price"] = float(df.iloc[-1]["close"])
            sn["shares"] = float(df.iloc[-1].get("outstanding_share", 0) or 0)
        except Exception:
            pass
        # 公司名称（东财个股信息：股票简称）。避免相对估值/可比公司里 A 股「名称」列空或显示代码
        try:
            info = ak.stock_individual_info_em(symbol=code)
            if info is not None and not info.empty and 'item' in info.columns:
                hit = info[info['item'].astype(str) == '股票简称']
                if not hit.empty:
                    sn["name"] = str(hit.iloc[0].get('value', code) or code)
        except Exception:
            pass
        # 财务指标（同花顺摘要，优先取最近年报 12-31，避免单季度数据误作年度基数）
        try:
            ths = ak.stock_financial_abstract_ths(symbol=code, indicator="按报告期")
            if ths is not None and not ths.empty:
                r = ths.iloc[-1]
                try:
                    ann = ths[ths["报告期"].astype(str).str.contains("12-31")]
                    if not ann.empty:
                        r = ann.iloc[-1]
                except Exception:
                    pass
                sn["revenue"] = _num(r.get("营业总收入"))
                sn["net_profit"] = _num(r.get("净利润"))
                sn["revenue_growth"] = _num(r.get("营业总收入同比增长率"))
                sn["profit_growth"] = _num(r.get("净利润同比增长率"))
                sn["eps"] = _num(r.get("基本每股收益"))
                sn["bps"] = _num(r.get("每股净资产"))
                sn["roe"] = _num(r.get("净资产收益率"))
                sn["gross_margin"] = _num(r.get("销售毛利率"))
                sn["net_margin"] = _num(r.get("销售净利率"))
                sn["debt_ratio"] = _num(r.get("资产负债率"))
                sn["ocf_ps"] = _num(r.get("每股经营现金流"))
        except Exception:
            pass
        # 净负债 = 负债合计 - 货币资金（最新期）
        try:
            bs = ak.stock_financial_report_sina(stock=sina, symbol="资产负债表")
            if bs is not None and not bs.empty:
                bs = bs.sort_values("报告日")
                r = bs.iloc[-1]
                debt, cash = _num(r.get("负债合计")), _num(r.get("货币资金"))
                if debt or cash:
                    sn["net_debt"] = debt - cash
        except Exception:
            pass
    else:
        # 港股：现价
        try:
            df = ak.stock_hk_daily(symbol=code, adjust="")
            sn["price"] = float(df.iloc[-1]["close"])
        except Exception:
            pass
        # 财务指标（东财数据中心）
        try:
            ind = ak.stock_financial_hk_analysis_indicator_em(symbol=code)
            if ind is not None and not ind.empty:
                ind = ind.sort_values("REPORT_DATE")
                r = ind.iloc[-1]
                sn["revenue"] = _num(r.get("OPERATE_INCOME"))
                sn["net_profit"] = _num(r.get("HOLDER_PROFIT"))
                sn["revenue_growth"] = _num(r.get("OPERATE_INCOME_YOY"))
                sn["profit_growth"] = _num(r.get("HOLDER_PROFIT_YOY"))
                sn["eps"] = _num(r.get("BASIC_EPS"))
                sn["bps"] = _num(r.get("BPS"))
                roe = _num(r.get("ROE_YEARLY"))
                if not roe:
                    roe = _num(r.get("ROE_AVG"))
                sn["roe"] = roe
                sn["gross_margin"] = _num(r.get("GROSS_PROFIT_RATIO"))
                sn["net_margin"] = _num(r.get("NET_PROFIT_RATIO"))
                sn["debt_ratio"] = _num(r.get("DEBT_ASSET_RATIO"))
                sn["ocf_ps"] = _num(r.get("PER_NETCASH_OPERATE"))
                sn["name"] = str(r.get("SECURITY_NAME_ABBR") or code)
        except Exception:
            pass
        # 资产负债表：净负债 + 股本推算
        try:
            bs = ak.stock_financial_hk_report_em(stock=code, symbol="资产负债表", indicator="年度")
            if bs is not None and not bs.empty:
                bs = bs.copy()
                bs["REPORT_DATE"] = bs["REPORT_DATE"].astype(str).str[:10]
                latest = bs[bs["REPORT_DATE"] == bs["REPORT_DATE"].max()]
                def _amt(name):
                    row = latest[latest["STD_ITEM_NAME"] == name]
                    return float(row["AMOUNT"].values[0]) if not row.empty else 0.0
                debt, cash, equity = _amt("总负债"), _amt("现金及等价物"), _amt("股东权益")
                if debt or cash:
                    sn["net_debt"] = debt - cash
                if equity and sn["bps"]:
                    sn["shares"] = equity / sn["bps"]
        except Exception:
            pass

    # 派生估值倍数
    if sn["price"] and sn["eps"]:
        sn["pe"] = round(sn["price"] / sn["eps"], 2) if sn["eps"] > 0 else None
    else:
        sn["pe"] = None
    if sn["price"] and sn["bps"]:
        sn["pb"] = round(sn["price"] / sn["bps"], 2) if sn["bps"] > 0 else None
    else:
        sn["pb"] = None
    return sn


def _fmt_yi(v):
    """金额转亿显示"""
    if v is None:
        return None
    return round(v / 1e8, 2)


# ======================= 绝对估值：DCF =======================

def dcf_valuation(symbol, params=None):
    """两阶段现金流折现模型：高增长期(默认5年) + 永续期，输出每股内在价值 vs 现价"""
    params = params or {}
    sn = _snapshot(symbol)
    if not sn["price"]:
        return {"error": "无法获取现价，请检查代码是否正确"}
    if sn["shares"] is None or sn["shares"] <= 0:
        return {"error": "无法获取总股本（可手动输入股本计算）"}

    # 基数：净利润 或 经营现金流
    base_mode = params.get("base", "net_profit")
    base = None
    if base_mode == "ocf" and sn["ocf_ps"]:
        base = sn["ocf_ps"] * sn["shares"]
    if base is None:
        base = sn["net_profit"] or (sn["ocf_ps"] * sn["shares"] if sn["ocf_ps"] else None)
    if not base:
        return {"error": "无法获取盈利基数（净利润/经营现金流）"}

    # 增长率：优先用户输入，否则用净利/营收同比（百分比数值，如 15.85 表示 15.85%）
    g1 = params.get("g1")
    if g1 is None or g1 == "":
        g1 = sn["profit_growth"] or sn["revenue_growth"] or 0
        g1 = max(float(g1), 0.0)
    else:
        g1 = float(g1)
    r = float(params.get("r", 9) or 9) / 100.0          # 折现率（百分数→小数）
    g2 = float(params.get("g2", 2.5) or 2.5) / 100.0    # 永续增长率（百分数→小数）
    years = int(params.get("years", 5) or 5)
    if g2 >= r:
        return {"error": "永续增长率必须小于折现率"}
    if r <= 0:
        return {"error": "折现率必须大于 0"}
    g1_rate = g1 / 100.0  # 高增长率（百分数→小数）

    # 两阶段 DCF
    fcf = base
    pv = 0.0
    flows = []
    for t in range(1, years + 1):
        fcf = base * (1 + g1_rate) ** t
        pv_t = fcf / (1 + r) ** t
        flows.append({"year": t, "fcf": round(fcf, 0), "pv": round(pv_t, 0)})
        pv += pv_t
    tv = base * (1 + g1_rate) ** years * (1 + g2) / (r - g2)     # 终值
    pv_tv = tv / (1 + r) ** years
    ev = pv + pv_tv
    equity_value = ev - (sn["net_debt"] or 0)
    per_share = equity_value / sn["shares"]
    spread = (per_share - sn["price"]) / sn["price"] * 100
    if spread > 20:
        verdict, verdict_cn = "undervalued", "低估（内在价值高于现价 20% 以上）"
    elif spread < -20:
        verdict, verdict_cn = "overvalued", "高估（内在价值低于现价 20% 以上）"
    else:
        verdict, verdict_cn = "fair", "估值合理（内在价值与现价差距在 ±20% 内）"

    # 敏感性矩阵：增长率 × 折现率（键统一字符串，避免 jsonify sort_keys 混型比较）
    g_grid = [round(g1 + d, 1) for d in (-4, -2, 0, 2, 4)]
    r_grid = [round(r * 100 + d, 1) for d in (-2, 0, 2)]
    matrix = []
    for rg in g_grid:
        row = {"g": rg}
        rg_rate = rg / 100.0
        for rr in r_grid:
            if rr <= g2 * 100:
                row[str(rr)] = None
                continue
            rr_d = rr / 100.0
            f = base
            p = 0.0
            for t in range(1, years + 1):
                f = base * (1 + rg_rate) ** t
                p += f / (1 + rr_d) ** t
            tvv = base * (1 + rg_rate) ** years * (1 + g2) / (rr_d - g2)
            p += tvv / (1 + rr_d) ** years
            row[str(rr)] = round((p - (sn["net_debt"] or 0)) / sn["shares"], 2)
        matrix.append(row)
    r_grid = [str(x) for x in r_grid]

    return {
        "ok": True,
        "symbol": symbol,
        "market": "港股" if sn["market"] == "hk" else "A股",
        "name": sn["name"],
        "price": sn["price"],
        "shares": sn["shares"],
        "base_fcf": round(base, 0),
        "base_fcf_yi": _fmt_yi(base),
        "base_mode": base_mode,
        "g1": round(g1, 2),
        "r": round(r * 100, 2),
        "g2": round(g2 * 100, 2),
        "years": years,
        "net_debt_yi": _fmt_yi(sn["net_debt"]) if sn["net_debt"] is not None else None,
        "flows": flows,
        "tv_yi": round(tv / 1e8, 2),
        "ev_yi": round(ev / 1e8, 2),
        "equity_yi": round(equity_value / 1e8, 2),
        "per_share": round(per_share, 2),
        "spread_pct": round(spread, 1),
        "verdict": verdict,
        "verdict_cn": verdict_cn,
        "sensitivity": {"g_grid": g_grid, "r_grid": r_grid, "matrix": matrix},
    }


# ======================= 相对估值：可比公司分析 =======================

def peer_comparison(symbol, peers=None):
    """目标公司 vs 可比公司：PE/PB/ROE/毛利率对比 + 行业中位数判断高估低估"""
    peers = peers or []
    if not peers:
        return {"error": "请至少输入一家可比公司代码（A股6位 / 港股HK+4位，逗号分隔）"}

    target = _snapshot(symbol)
    if not target["price"]:
        return {"error": "无法获取目标公司现价，请检查代码是否正确"}

    # 目标公司 + 可比公司（最多 5 家）并发拉取
    codes = peers[:5]
    rows = [target]
    with ThreadPoolExecutor(max_workers=4) as ex:
        for sn in ex.map(lambda c: _snapshot(c), codes):
            if sn["price"]:
                rows.append(sn)

    def row_out(sn):
        return {
            "code": sn["code"], "name": sn["name"], "market": "港股" if sn["market"] == "hk" else "A股",
            "price": sn["price"], "eps": round(sn["eps"], 2) if sn["eps"] else None,
            "pe": sn["pe"], "bps": round(sn["bps"], 2) if sn["bps"] else None,
            "pb": sn["pb"], "roe": round(sn["roe"], 2) if sn["roe"] else None,
            "gross_margin": round(sn["gross_margin"], 2) if sn["gross_margin"] else None,
            "net_margin": round(sn["net_margin"], 2) if sn["net_margin"] else None,
            "debt_ratio": round(sn["debt_ratio"], 2) if sn["debt_ratio"] else None,
            "revenue_yi": _fmt_yi(sn["revenue"]) if sn["revenue"] else None,
            "net_profit_yi": _fmt_yi(sn["net_profit"]) if sn["net_profit"] else None,
        }

    out = [row_out(t) for t in rows]
    # 中位数（排除目标与空值）
    def median(key):
        vals = sorted(r[key] for r in out[1:] if r.get(key) is not None)
        if not vals:
            return None
        n = len(vals)
        return vals[n // 2] if n % 2 else round((vals[n // 2 - 1] + vals[n // 2]) / 2, 2)

    med_pe, med_pb = median("pe"), median("pb")
    t_pe, t_pb = out[0]["pe"], out[0]["pb"]

    verdict = None
    if t_pe is not None and med_pe:
        gap = round((t_pe - med_pe) / med_pe * 100, 1)
        if gap > 20:
            v = f"目标公司 PE({t_pe}) 高于行业中位数 PE({med_pe}) {gap}%，相对估值偏高"
        elif gap < -20:
            v = f"目标公司 PE({t_pe}) 低于行业中位数 PE({med_pe}) {abs(gap)}%，相对估值偏低"
        else:
            v = f"目标公司 PE({t_pe}) 与行业中位数 PE({med_pe}) 接近，相对估值合理"
        verdict = {"label": v, "pe_gap_pct": gap}
    elif t_pe is not None and med_pe is None:
        verdict = {"label": "可比公司 PE 数据不足，无法比较", "pe_gap_pct": None}

    return {
        "ok": True,
        "symbol": symbol,
        "target": out[0],
        "peers": out[1:],
        "med_pe": med_pe,
        "med_pb": med_pb,
        "verdict": verdict,
    }


if __name__ == "__main__":
    import json
    print(json.dumps(dcf_valuation("HK0700"), ensure_ascii=False, default=str)[:800])
