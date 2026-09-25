#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
yfinance_data.py — yfinance 金融数据源封装

yfinance 特点：免费、无需 token，覆盖美股/全球主要市场/外汇/美债收益率/加密行情（Yahoo 代码）。

支持接口：
- kline    : 历史 K 线（日/周/月/分钟，支持全球 Yahoo 代码）
- info     : 标的概况（行业、市值、PE、网站等）
- treasury : 美债收益率快照与近 1 年走势（^IRX/^FVX/^TNX/^TYX）
- indices  : 全球主要指数快照（标普/纳指/道指/恒生/上证等）
- fx       : 主要货币对最新价与近 1 月走势
- news     : 标的相关新闻标题列表

代码示例：AAPL / MSFT / BRK-B / 0700.HK / 000001.SS / 7203.T / ^GSPC / USDCNY=X / BTC-USD
"""
from datetime import datetime

import pandas as pd

# UI 周期 → yfinance interval
_INTERVALS = {
    "daily": "1d", "d": "1d", "1d": "1d",
    "weekly": "1wk", "w": "1wk", "1wk": "1wk",
    "monthly": "1mo", "m": "1mo", "1mo": "1mo",
    "5": "5m", "15": "15m", "30": "30m", "60": "60m",
}

# 美债收益率常用代码
TREASURY_TICKERS = {
    "^IRX": "13周美债收益率",
    "^FVX": "5年期美债收益率",
    "^TNX": "10年期美债收益率",
    "^TYX": "30年期美债收益率",
}

# 全球主要指数
MAJOR_INDICES = {
    "^GSPC": "标普500",
    "^DJI": "道琼斯工业",
    "^IXIC": "纳斯达克综合",
    "^RUT": "罗素2000",
    "^VIX": "VIX恐慌指数",
    "^HSI": "恒生指数",
    "000001.SS": "上证指数",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
    "^N225": "日经225",
    "^FTSE": "英国富时100",
    "^GDAXI": "德国DAX40",
    "^FCHI": "法国CAC40",
}

# 主要货币对（Yahoo 代码形如 USDCNY=X）
FX_PAIRS = {
    "USDCNY=X": "美元/人民币",
    "EURUSD=X": "欧元/美元",
    "USDJPY=X": "美元/日元",
    "GBPUSD=X": "英镑/美元",
    "USDHKD=X": "美元/港元",
    "AUDUSD=X": "澳元/美元",
    "USDCAD=X": "美元/加元",
    "USDCHF=X": "美元/瑞郎",
    "XAUUSD=X": "黄金/美元",
}


def _require():
    try:
        import yfinance as yf
        return yf
    except ImportError as e:
        raise RuntimeError("yfinance 未安装，请执行: pip install yfinance") from e


def _clean_symbol(symbol) -> str:
    s = str(symbol or "").strip().upper().replace(" ", "")
    if not s:
        raise ValueError("请提供标的代码（如 AAPL / 0700.HK / 000001.SS / ^GSPC / USDCNY=X）")
    return s


def _flatten_columns(df: pd.DataFrame) -> pd.DataFrame:
    """yfinance 多层列名压平为小写单层列"""
    if df is None or df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        cols = []
        for col in df.columns:
            parts = [str(p) for p in col if str(p) and str(p) != "nan"]
            cols.append("_".join(parts).lower())
        df = df.copy()
        df.columns = cols
    else:
        df = df.copy()
        df.columns = [str(c).lower() for c in df.columns]
    # 去掉 close 等字段里可能残留的 ticker 后缀（单标的下载时偶发 Adj Close 类名）
    df = df.rename(columns={
        "adj_close": "adj_close", "adj close": "adj_close",
        "price": "close",
    })
    return df


def _download(symbol: str, period: str = "daily", start_date: str = "", end_date: str = "",
              yf=None, interval_override: str = ""):
    yf = yf or _require()
    interval = interval_override or _INTERVALS.get(str(period or "daily").strip().lower(), "1d")
    intraday = interval in ("1m", "2m", "5m", "15m", "30m", "60m", "90m")
    kwargs = dict(interval=interval, auto_adjust=True, progress=False, threads=False)
    if start_date and end_date:
        kwargs["start"] = str(start_date).replace("/", "-")
        # yfinance end 为开区间，补一天避免漏掉结束日
        try:
            end_ts = pd.Timestamp(str(end_date).replace("/", "-"))
            kwargs["end"] = (end_ts + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        except Exception:
            kwargs["end"] = str(end_date).replace("/", "-")
    else:
        # 分钟线 Yahoo 仅保留近 60 天左右
        kwargs["period"] = "60d" if intraday else "5y"
    try:
        df = yf.download(symbol, **kwargs)
    except Exception as e:
        name = type(e).__name__
        msg = str(e)
        if "RateLimit" in name or "Too Many Requests" in msg or "Rate limited" in msg:
            raise RuntimeError(
                "Yahoo Finance 触发限流（Too Many Requests）。请稍等几分钟后重试，"
                "或改用 akshare / FRED 等其他数据源。"
            ) from e
        raise RuntimeError(f"yfinance 下载失败: {msg or name}") from e
    if df is None or (hasattr(df, "empty") and df.empty):
        # yfinance 限流时有时不抛异常、只打日志并返回空表
        raise RuntimeError(
            f"未取到 {symbol} 行情（可能代码错误、停牌，或 Yahoo 限流）。"
            "请检查代码（如 AAPL / 0700.HK / ^GSPC），稍后重试。"
        )
    df = _flatten_columns(df)
    if "date" not in df.columns and df.index.name is not None:
        df = df.reset_index()
        df = _flatten_columns(df)
    elif not isinstance(df.index, pd.RangeIndex):
        df = df.reset_index()
        df = _flatten_columns(df)
    # 列名常见变体统一
    df = df.rename(columns={
        "index": "date", "datetime": "date", "timestamp": "date",
        "open": "open", "high": "high", "low": "low",
        "volume": "volume", "dividends": "dividends", "stock splits": "stock_splits",
    })
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    return df


def get_kline(symbol, period="daily", start_date="", end_date="", **kwargs):
    """历史 K 线

    Args:
        symbol: Yahoo 代码（AAPL / 0700.HK / 000001.SS / ^GSPC / USDCNY=X）
        period: daily / weekly / monthly / 5 / 15 / 30 / 60（分钟）
        start_date/end_date: YYYY-MM-DD；不填则默认近 5 年（分钟线近 60 天）
    """
    return _download(_clean_symbol(symbol), period=period,
                     start_date=start_date, end_date=end_date)


def get_info(symbol, **kwargs):
    """标的概况（单行宽表）"""
    yf = _require()
    sym = _clean_symbol(symbol)
    t = yf.Ticker(sym)
    info = {}
    try:
        info = dict(t.info or {})
    except Exception:
        info = {}
    if not info:
        try:
            fi = dict(t.fast_info or {})
            info = {k: fi.get(k) for k in fi.keys()}
        except Exception:
            pass
    if not info:
        raise RuntimeError(f"未取到 {sym} 的概况信息，请检查代码是否正确")

    keep = [
        ("symbol", "symbol"), ("shortName", "name"), ("longName", "full_name"),
        ("sector", "sector"), ("industry", "industry"),
        ("marketCap", "market_cap"), ("trailingPE", "pe"), ("forwardPE", "forward_pe"),
        ("priceToBook", "pb"), ("trailingEps", "eps"), ("dividendYield", "dividend_yield"),
        ("fiftyTwoWeekHigh", "high_52w"), ("fiftyTwoWeekLow", "low_52w"),
        ("averageVolume", "avg_volume"), ("beta", "beta"),
        ("currency", "currency"), ("exchange", "exchange"), ("country", "country"),
        ("website", "website"), ("longBusinessSummary", "summary"),
        ("currentPrice", "price"), ("regularMarketPrice", "price_rt"),
    ]
    row = {"symbol": sym}
    for src, dst in keep:
        if src in info and info[src] is not None:
            row[dst] = info[src]
    for k in ("currentPrice", "regularMarketPrice", "regularMarketPreviousClose"):
        if k in info and info[k] is not None and "price" not in row:
            row["price"] = info[k]
    return pd.DataFrame([row])


def get_treasury(start_date="", end_date="", **kwargs):
    """美债收益率：近 1 年（或指定区间）日频走势，列 = date/IRX/FVX/TNX/TYX"""
    yf = _require()
    parts = []
    for tk in TREASURY_TICKERS:
        try:
            df = _download(tk, start_date=start_date, end_date=end_date, yf=yf)
            if df is None or "close" not in df.columns or df.empty:
                continue
            sub = df[["date", "close"]].copy()
            sub.columns = ["date", tk.replace("^", "")]
            parts.append(sub.set_index("date"))
        except Exception:
            continue
    if not parts:
        raise RuntimeError("美债收益率数据获取失败（Yahoo 可能暂时不可用）")
    out = pd.concat(parts, axis=1, join="outer").sort_index().reset_index()
    return out.dropna(how="all")


def get_indices(**kwargs):
    """全球主要指数快照（最新收盘 + 涨跌幅）"""
    yf = _require()
    codes = list(MAJOR_INDICES.keys())
    try:
        raw = yf.download(codes, period="10d", group_by="column",
                          auto_adjust=True, progress=False, threads=True)
    except Exception as e:
        raise RuntimeError(f"指数行情获取失败: {e}") from e
    if raw is None or raw.empty:
        raise RuntimeError("指数行情返回为空")

    rows = []
    for tk, name in MAJOR_INDICES.items():
        try:
            if isinstance(raw.columns, pd.MultiIndex):
                close = raw["Close"][tk].dropna()
                high = raw["High"][tk].dropna() if "High" in raw.columns.get_level_values(0) else close
                low = raw["Low"][tk].dropna() if "Low" in raw.columns.get_level_values(0) else close
                vol = raw["Volume"][tk].dropna() if "Volume" in raw.columns.get_level_values(0) else None
            else:
                close = raw[tk].dropna() if tk in raw.columns else pd.Series(dtype=float)
                high = low = vol = None
            if close is None or len(close) == 0:
                continue
            last, prev = float(close.iloc[-1]), (float(close.iloc[-2]) if len(close) > 1 else None)
            pct = round((last / prev - 1) * 100, 2) if prev else None
            rows.append({
                "symbol": tk, "name": name,
                "date": str(close.index[-1].date()),
                "close": round(last, 4),
                "pct_chg": pct,
                "high": round(float(high.iloc[-1]), 4) if high is not None and len(high) else None,
                "low": round(float(low.iloc[-1]), 4) if low is not None and len(low) else None,
                "volume": int(vol.iloc[-1]) if vol is not None and len(vol) and pd.notna(vol.iloc[-1]) else None,
            })
        except Exception:
            continue
    if not rows:
        raise RuntimeError("未取到任何指数数据")
    return pd.DataFrame(rows)


def get_fx(symbol="", **kwargs):
    """外汇/贵金属。

    - 不传 symbol：主要货币对最新快照
    - 传 symbol（如 USDCNY=X）：该货币对近 1 月/指定区间日线
    """
    yf = _require()
    sym = str(symbol or "").strip()
    if sym:
        df = _download(_clean_symbol(sym), start_date=kwargs.get("start_date", ""),
                       end_date=kwargs.get("end_date", ""), yf=yf)
        if df is None or df.empty:
            raise RuntimeError(f"未取到 {sym} 汇率数据")
        return df

    rows = []
    for pair, name in FX_PAIRS.items():
        try:
            df = _download(pair, period="daily", yf=yf)
            if df is None or "close" not in df.columns or df.empty:
                continue
            close = df["close"].dropna()
            if len(close) == 0:
                continue
            last = float(close.iloc[-1])
            prev = float(close.iloc[-2]) if len(close) > 1 else None
            rows.append({
                "pair": pair, "name": name,
                "date": str(df["date"].iloc[-1]) if "date" in df.columns else "",
                "rate": round(last, 6),
                "pct_chg": round((last / prev - 1) * 100, 4) if prev else None,
            })
        except Exception:
            continue
    if not rows:
        raise RuntimeError("外汇行情获取失败（Yahoo 可能暂时不可用）")
    return pd.DataFrame(rows)


def get_news(symbol, limit=10, **kwargs):
    """标的相关新闻（标题/来源/时间）"""
    yf = _require()
    sym = _clean_symbol(symbol)
    t = yf.Ticker(sym)
    try:
        raw = t.news or []
    except Exception as e:
        raise RuntimeError(f"新闻获取失败: {e}") from e
    rows = []
    for item in raw[: max(1, int(limit or 10))]:
        if not isinstance(item, dict):
            continue
        # yfinance 新旧两套结构
        content = item.get("content") if isinstance(item.get("content"), dict) else {}
        title = item.get("title") or content.get("title") or ""
        link = item.get("link") or item.get("url") or ""
        if not link and isinstance(content.get("canonicalUrl"), dict):
            link = content["canonicalUrl"].get("url") or ""
        publisher = ""
        if isinstance(content.get("provider"), dict):
            publisher = content["provider"].get("displayName") or ""
        publisher = publisher or item.get("publisher") or ""
        pub_time = item.get("providerPublishTime") or content.get("pubDate") or ""
        if isinstance(pub_time, (int, float)):
            try:
                pub_time = datetime.utcfromtimestamp(int(pub_time)).strftime("%Y-%m-%d %H:%M")
            except Exception:
                pub_time = str(pub_time)
        if title:
            rows.append({
                "symbol": sym, "title": title,
                "publisher": publisher, "time": pub_time, "link": link,
            })
    if not rows:
        return pd.DataFrame([{"symbol": sym, "title": "(暂无新闻)", "publisher": "", "time": "", "link": ""}])
    return pd.DataFrame(rows)


# 接口注册表（供路由分发 / catalog）
YF_FUNCS = {
    "kline": {
        "func": get_kline, "label": "历史K线（美股/全球/外汇）",
        "desc": "Yahoo 历史行情；代码 AAPL / 0700.HK / ^GSPC / USDCNY=X",
        "cat": "行情", "params": ["symbol", "period", "start_date", "end_date"],
    },
    "info": {
        "func": get_info, "label": "标的概况",
        "desc": "行业、市值、PE/PB、52周高低等基本信息",
        "cat": "行情", "params": ["symbol"],
    },
    "treasury": {
        "func": get_treasury, "label": "美债收益率（13周/5年/10年/30年）",
        "desc": "^IRX/^FVX/^TNX/^TYX 日频序列，可填起止日期",
        "cat": "利率", "params": ["start_date", "end_date"],
    },
    "indices": {
        "func": get_indices, "label": "全球主要指数快照",
        "desc": "标普/纳指/道指/恒生/上证/日经/DAX 等最新收盘与涨跌幅",
        "cat": "指数", "params": [],
    },
    "fx": {
        "func": get_fx, "label": "外汇与贵金属",
        "desc": "不填代码=主要货币对快照；填 USDCNY=X 则取该对日线",
        "cat": "外汇", "params": ["symbol", "start_date", "end_date"],
    },
    "news": {
        "func": get_news, "label": "标的新闻",
        "desc": "个股/指数相关新闻标题列表",
        "cat": "资讯", "params": ["symbol"],
    },
}
