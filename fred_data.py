#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fred_data.py — FRED（圣路易斯联储）宏观与利率数据源封装

特点：
- 序列取数走 fredgraph.csv 公开下载接口，**无需 API key**
- 序列检索默认在内置精选目录本地过滤；若设置环境变量 FRED_API_KEY，可走官方搜索
- 覆盖：美债收益率与利差、通胀、就业、GDP、货币、信用利差、汇率、大宗商品

内置精选序列见 FRED_SERIES；也可直接用任意 FRED 序列 ID（如 MORTGAGE30US）取数。
"""
import io
import os
import re

import pandas as pd
import requests

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FRED_SEARCH_URL = "https://api.stlouisfed.org/fred/series/search"

# 序列 ID：字母开头，允许数字/点/下划线/连字符，≤20 位
_SERIES_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,19}$")

# 内置精选序列（label 中文 / cat 分类 / desc 英文系列名）
FRED_SERIES = {
    # ---- 利率与利差 ----
    "DGS2": {"label": "2年期美债收益率", "cat": "利率", "desc": "2-Year Treasury Constant Maturity Rate (%)"},
    "DGS5": {"label": "5年期美债收益率", "cat": "利率", "desc": "5-Year Treasury Constant Maturity Rate (%)"},
    "DGS10": {"label": "10年期美债收益率", "cat": "利率", "desc": "10-Year Treasury Constant Maturity Rate (%)"},
    "DGS30": {"label": "30年期美债收益率", "cat": "利率", "desc": "30-Year Treasury Constant Maturity Rate (%)"},
    "DTB3": {"label": "3个月国库券收益率", "cat": "利率", "desc": "3-Month Treasury Bill Secondary Market Rate (%)"},
    "DTB6": {"label": "6个月国库券收益率", "cat": "利率", "desc": "6-Month Treasury Bill Secondary Market Rate (%)"},
    "T10Y2Y": {"label": "10Y-2Y 美债利差", "cat": "利率", "desc": "10-Year Minus 2-Year Treasury Spread (%)"},
    "T10Y3M": {"label": "10Y-3M 美债利差", "cat": "利率", "desc": "10-Year Minus 3-Month Treasury Spread (%)"},
    "DFEDTARU": {"label": "联邦基金目标利率上限", "cat": "利率", "desc": "Federal Funds Target Range - Upper Limit (%)"},
    "SOFR": {"label": "担保隔夜融资利率 SOFR", "cat": "利率", "desc": "Secured Overnight Financing Rate (%)"},
    "MORTGAGE30US": {"label": "美国30年房贷利率", "cat": "利率", "desc": "30-Year Fixed Rate Mortgage Average (%)"},
    "T5YIE": {"label": "5年盈亏平衡通胀率", "cat": "利率", "desc": "5-Year Breakeven Inflation Rate (%)"},
    "T10YIE": {"label": "10年盈亏平衡通胀率", "cat": "利率", "desc": "10-Year Breakeven Inflation Rate (%)"},

    # ---- 通胀 ----
    "CPIAUCSL": {"label": "美国CPI（季调总指数）", "cat": "通胀", "desc": "Consumer Price Index for All Urban Consumers (Index 1982-84=100)"},
    "CPILFESL": {"label": "美国核心CPI", "cat": "通胀", "desc": "Core CPI: All Items less Food and Energy (Index 1982-84=100)"},
    "PCEPI": {"label": "美国PCE物价指数", "cat": "通胀", "desc": "Personal Consumption Expenditures Price Index"},
    "PCEPILFE": {"label": "美国核心PCE", "cat": "通胀", "desc": "Core PCE Price Index"},

    # ---- 就业 ----
    "UNRATE": {"label": "美国失业率", "cat": "就业", "desc": "Civilian Unemployment Rate (%)"},
    "PAYEMS": {"label": "非农就业总人数", "cat": "就业", "desc": "All Employees, Total Nonfarm (Thousands)"},
    "ICSA": {"label": "初请失业金人数", "cat": "就业", "desc": "Initial Claims (Thousands, Weekly)"},
    "UMCSENT": {"label": "密歇根消费者信心指数", "cat": "就业", "desc": "University of Michigan Consumer Sentiment Index"},

    # ---- 增长与产出 ----
    "GDP": {"label": "美国GDP（现价季末值）", "cat": "增长", "desc": "Gross Domestic Product (Billions of Dollars)"},
    "GDPC1": {"label": "美国实际GDP（季末值）", "cat": "增长", "desc": "Real Gross Domestic Product (Billions of Chained 2017 Dollars)"},
    "INDPRO": {"label": "工业生产指数", "cat": "增长", "desc": "Industrial Production: Total Index"},
    "HOUST": {"label": "新屋开工", "cat": "增长", "desc": "New Privately-Owned Housing Units Started (Thousands of Units)"},
    "RSAFS": {"label": "零售销售额", "cat": "增长", "desc": "Retail and Food Services Sales (Millions of Dollars)"},

    # ---- 货币 ----
    "M2SL": {"label": "M2 货币供应量", "cat": "货币", "desc": "M2 Money Stock (Billions of Dollars)"},
    "WALCL": {"label": "美联储总资产（周度）", "cat": "货币", "desc": "Federal Reserve Total Assets (Millions of Dollars)"},

    # ---- 信用与市场 ----
    "BAMLH0A0HYM2": {"label": "高收益债利差 OAS", "cat": "信用市场", "desc": "ICE BofA US High Yield Index Option-Adjusted Spread (%)"},
    "BAMLC0A0CM": {"label": "投资级公司债利差 OAS", "cat": "信用市场", "desc": "ICE BofA US Corporate Index Option-Adjusted Spread (%)"},
    "VIXCLS": {"label": "VIX 恐慌指数", "cat": "信用市场", "desc": "CBOE Volatility Index: VIX"},
    "SP500": {"label": "标普500指数（收盘）", "cat": "信用市场", "desc": "S&P 500 Composite Price Index"},

    # ---- 汇率与大宗商品 ----
    "DEXUSEU": {"label": "美元/欧元汇率", "cat": "汇率商品", "desc": "U.S. / Euro Foreign Exchange Rate"},
    "DEXJPUS": {"label": "日元/美元汇率", "cat": "汇率商品", "desc": "Japan / U.S. Foreign Exchange Rate (Yen per USD)"},
    "DEXCHUS": {"label": "人民币/美元汇率", "cat": "汇率商品", "desc": "China / U.S. Foreign Exchange Rate (Yuan per USD)"},
    "DCOILWTICO": {"label": "WTI 原油现货价", "cat": "汇率商品", "desc": "Crude Oil Prices: West Texas Intermediate (Dollars per Barrel)"},
    "DCOILBRENTEU": {"label": "布伦特原油现货价", "cat": "汇率商品", "desc": "Crude Oil Prices: Brent (Dollars per Barrel)"},
    "GOLDAMGBD228NLBM": {"label": "伦敦金定盘价", "cat": "汇率商品", "desc": "Gold Fixing Price 3:00 PM (London time)"},
    "DTWEXBGS": {"label": "美元广义贸易加权指数", "cat": "汇率商品", "desc": "Nominal Broad U.S. Dollar Index"},
}


def get_fred_api_key() -> str:
    return (os.environ.get("FRED_API_KEY") or "").strip()


def _validate_series_id(series_id: str) -> str:
    sid = str(series_id or "").strip().upper()
    if not sid or not _SERIES_ID_RE.match(sid):
        raise ValueError(f"非法 FRED 序列 ID: {series_id!r}（示例：DGS10 / CPIAUCSL / UNRATE）")
    return sid


def get_series(series_id, start_date="", end_date="", **kwargs):
    """拉取 FRED 序列观测值（fredgraph.csv，无需 API key）

    Args:
        series_id: FRED 序列 ID，如 DGS10 / CPIAUCSL / UNRATE
        start_date/end_date: YYYY-MM-DD，不填则由 FRED 返回默认长度
    """
    sid = _validate_series_id(series_id)
    params = {"id": sid}
    if start_date:
        params["cosd"] = str(start_date).replace("/", "-")
    if end_date:
        params["coed"] = str(end_date).replace("/", "-")

    try:
        resp = requests.get(FRED_CSV_URL, params=params, timeout=30,
                            headers={"User-Agent": "data.tool/1.0"})
        resp.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(f"FRED 请求失败: {e}") from e

    text = resp.text or ""
    if not text.strip():
        raise RuntimeError(f"FRED 序列 {sid} 返回为空")
    low = text.lstrip().lower()
    if low.startswith("<!doctype") or low.startswith("<html"):
        raise RuntimeError(f"FRED 序列 {sid} 不存在或暂不可用")

    try:
        df = pd.read_csv(io.StringIO(text))
    except Exception as e:
        raise RuntimeError(f"FRED CSV 解析失败: {e}") from e
    if df is None or df.empty:
        raise RuntimeError(f"FRED 序列 {sid} 暂无数据")

    # 列名兼容：observation_date / DATE / date
    date_col = None
    for c in df.columns:
        if str(c).lower() in ("observation_date", "date", "datetime"):
            date_col = c
            break
    if date_col is None:
        date_col = df.columns[0]
    val_col = df.columns[1] if len(df.columns) > 1 else df.columns[0]

    out = pd.DataFrame({
        "date": pd.to_datetime(df[date_col], errors="coerce").dt.strftime("%Y-%m-%d"),
        "value": pd.to_numeric(df[val_col].replace(".", pd.NA), errors="coerce"),
    })
    out.insert(0, "series_id", sid)
    meta = FRED_SERIES.get(sid)
    if meta:
        out.insert(1, "name", meta["label"])
    out = out.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    out = out.dropna(subset=["value"])
    if out.empty:
        raise RuntimeError(f"FRED 序列 {sid} 在给定区间无有效观测值")
    return out


def list_popular(**kwargs):
    """内置精选序列目录（不联网）"""
    rows = []
    for sid, meta in FRED_SERIES.items():
        rows.append({
            "series_id": sid,
            "name": meta["label"],
            "cat": meta["cat"],
            "desc": meta["desc"],
        })
    return pd.DataFrame(rows)


def search_series(query="", **kwargs):
    """序列检索：优先本地精选目录；配置 FRED_API_KEY 时附加官方搜索结果

    Args:
        query: 关键词（匹配 ID / 中文名 / 英文名 / 分类）
    """
    q = str(query or "").strip().lower()
    rows = []
    for sid, meta in FRED_SERIES.items():
        hay = f"{sid} {meta['label']} {meta['desc']} {meta['cat']}".lower()
        if not q or q in hay:
            rows.append({
                "series_id": sid, "name": meta["label"],
                "cat": meta["cat"], "desc": meta["desc"], "source": "builtin",
            })

    key = get_fred_api_key()
    if key and q:
        try:
            resp = requests.get(FRED_SEARCH_URL, params={
                "search_text": q, "api_key": key, "file_type": "json", "limit": 15,
            }, timeout=20)
            if resp.ok:
                for item in (resp.json() or {}).get("seriess", []):
                    sid = item.get("id")
                    if not sid:
                        continue
                    rows.append({
                        "series_id": sid,
                        "name": item.get("title") or sid,
                        "cat": "官方搜索",
                        "desc": item.get("notes") or "",
                        "source": "fred_api",
                    })
        except Exception:
            pass

    if not rows:
        return pd.DataFrame([{
            "series_id": "", "name": f"未匹配: {query}", "cat": "",
            "desc": "可尝试 GDP / CPI / 利率 / DGS10 等关键词", "source": "hint",
        }])
    # 去重（保留先出现的 builtin）
    seen = set()
    dedup = []
    for r in rows:
        if r["series_id"] in seen:
            continue
        seen.add(r["series_id"])
        dedup.append(r)
    return pd.DataFrame(dedup)


def get_series_pack(series_ids=None, start_date="", end_date="", **kwargs):
    """多序列并排拼接（按日期 outer join），便于利差/对比作图"""
    ids = list(series_ids or ["DGS10", "DGS2", "T10Y2Y"])
    frames = []
    for sid in ids:
        try:
            df = get_series(sid, start_date=start_date, end_date=end_date)
        except Exception:
            continue
        sub = df[["date", "value"]].copy()
        sub.columns = ["date", str(sid).strip().upper()]
        frames.append(sub.set_index("date"))
    if not frames:
        raise RuntimeError("多序列拉取全部失败")
    out = pd.concat(frames, axis=1, join="outer").sort_index().reset_index()
    return out


# 接口注册表（供路由分发；func=series/search/list/pack）
FRED_FUNCS = {
    "series": {
        "func": get_series, "label": "单序列取数",
        "desc": "按 FRED 序列 ID 拉观测值（免 token）",
        "cat": "基础", "params": ["series_id", "start_date", "end_date"],
    },
    "search": {
        "func": search_series, "label": "序列检索",
        "desc": "关键词搜索内置精选序列",
        "cat": "基础", "params": ["query"],
    },
    "list": {
        "func": list_popular, "label": "精选序列目录",
        "desc": "列出内置常用宏观/利率序列",
        "cat": "基础", "params": [],
    },
    "pack": {
        "func": get_series_pack, "label": "多序列并排",
        "desc": "默认 DGS10/DGS2/T10Y2Y；可传 series_ids 列表",
        "cat": "基础", "params": ["start_date", "end_date"],
    },
}
