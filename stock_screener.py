# -*- coding: utf-8 -*-
"""股票筛选器 - 按 PE/ROE/市值/行业等条件筛选 A 股，支持导出 CSV"""

import requests, time, pandas as pd, io, logging
from pathlib import Path

logger = logging.getLogger(__name__)

# 最近一次数据获取失败的原因（供上层接口返回可诊断的提示，避免笼统的"获取失败"）
LAST_ERROR = {"reason": ""}

try:
    from core_config import OUT_DIR  # 统一入口，避免多处定义分叉
except ImportError:
    OUT_DIR = Path(__file__).parent / "output"  # core_config 不可用时兜底
OUT_DIR.mkdir(exist_ok=True)

EM_FIELDS = {
    "最新价": "f2", "涨跌幅": "f3", "涨跌额": "f4", "成交量": "f5",
    "成交额": "f6", "振幅": "f7", "换手率": "f8", "市盈率(动态)": "f9",
    "量比": "f10", "股票代码": "f12", "股票名称": "f14", "最高": "f15",
    "最低": "f16", "今开": "f17", "昨收": "f18", "总市值": "f20",
    "流通市值": "f21", "市净率": "f23", "60日涨跌幅": "f24",
    "行业": "f100", "市盈率TTM": "f115", "净资产收益率": "f117",
    "主营业务收入": "f129", "净利润": "f135",
}


def fetch_all_stocks():
    """从东方财富获取全量A股数据（带简单位数据）"""
    all_data = []
    field_str = ",".join(EM_FIELDS.values())
    # 全市场: m:0+t:6(沪A) + m:0+t:80(深A) + m:1+t:2(创业板) + m:1+t:23(科创板)
    fs = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
    # 东方财富对无 UA / 无 Referer 的请求会直接断连（RemoteDisconnected）
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        "Referer": "https://quote.eastmoney.com/",
    }
    errors = []

    for page in range(1, 25):  # 最多25页 x 100 = 2500支，覆盖主要A股
        try:
            r = requests.get(
                "http://push2.eastmoney.com/api/qt/clist/get",
                params={
                    "pn": str(page), "pz": "100",
                    "po": "1", "np": "1", "fltt": "2", "invt": "2",
                    "fid": "f3", "fs": fs,
                    "fields": field_str,
                },
                headers=headers,
                timeout=20,
            )
            data = r.json()
            items = data.get("data", {}).get("diff", [])
            if not items:
                break
            all_data.extend(items)
            total = data["data"]["total"]
            if page * 2000 >= total:
                break
            time.sleep(0.5)  # 避免请求过快
        except Exception as e:
            # 记录失败原因（原实现静默 continue，全部失败时上层无从诊断）
            errors.append(f"第{page}页 {type(e).__name__}: {e}")
            continue

    if not all_data:
        reason = errors[0] if errors else "接口未返回数据（可能被风控拦截）"
        LAST_ERROR["reason"] = reason
        logger.warning(f"fetch_all_stocks 获取失败: {reason}")
        return None
    LAST_ERROR["reason"] = ""

    df = pd.DataFrame(all_data)
    # 重命名列
    col_map = {}
    for cn_name, field_id in EM_FIELDS.items():
        if field_id in df.columns:
            col_map[field_id] = cn_name
    df = df.rename(columns=col_map)

    # 数值转换 + 过滤无效数据
    for col in ["股票代码", "股票名称", "行业"]:
        if col in df.columns:
            df[col] = df[col].astype(str)

    for col in ["总市值", "流通市值", "净利润", "主营业务收入"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    for col in ["市盈率(动态)", "市盈率TTM", "市净率", "净资产收益率", "最新价", "涨跌幅", "换手率", "成交量"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # 去掉无效数据
    df = df[df["最新价"] > 0].copy()
    # 替换 NaN/Inf 为 None (JSON 兼容)
    df = df.where(pd.notnull(df), None)

    return df


def screen_stocks(df, filters):
    """
    按条件筛选
    filters: {
        pe_min: float, pe_max: float,
        roe_min: float,
        mcap_min: float, mcap_max: float,  # 亿
        industry: str,  # 行业关键词
        pb_max: float,
        price_min: float, price_max: float,
        change_min: float,  # 涨跌幅下限
    }
    """
    result = df.copy()

    if 'pe_min' in filters and filters['pe_min'] is not None:
        pe_col = "市盈率TTM" if "市盈率TTM" in result.columns else "市盈率(动态)"
        if pe_col in result.columns:
            result = result[result[pe_col] >= filters['pe_min']]
    if 'pe_max' in filters and filters['pe_max'] is not None:
        pe_col = "市盈率TTM" if "市盈率TTM" in result.columns else "市盈率(动态)"
        if pe_col in result.columns:
            result = result[result[pe_col] <= filters['pe_max']]

    if 'roe_min' in filters and filters['roe_min'] is not None:
        if "净资产收益率" in result.columns:
            result = result[result["净资产收益率"] >= filters['roe_min']]

    if 'mcap_min' in filters and filters['mcap_min'] is not None:
        cap = filters['mcap_min'] * 1e8
        if "总市值" in result.columns:
            result = result[result["总市值"] >= cap]
    if 'mcap_max' in filters and filters['mcap_max'] is not None:
        cap = filters['mcap_max'] * 1e8
        if "总市值" in result.columns:
            result = result[result["总市值"] <= cap]

    if 'pb_max' in filters and filters['pb_max'] is not None:
        if "市净率" in result.columns:
            result = result[result["市净率"] <= filters['pb_max']]

    if 'industry' in filters and filters['industry'] and "行业" in result.columns:
        kw = filters['industry'].strip()
        if kw:
            result = result[result["行业"].str.contains(kw, na=False)]

    if 'price_min' in filters and filters['price_min'] is not None:
        result = result[result["最新价"] >= filters['price_min']]
    if 'price_max' in filters and filters['price_max'] is not None:
        result = result[result["最新价"] <= filters['price_max']]

    if 'change_min' in filters and filters['change_min'] is not None:
        result = result[result["涨跌幅"] >= filters['change_min']]

    # 格式化用于展示
    display_cols = ['股票代码', '股票名称', '最新价', '涨跌幅', '市盈率(动态)', '市净率', '总市值', '行业']
    avail_cols = [c for c in display_cols if c in result.columns]
    display = result[avail_cols].copy()

    # 市值转亿
    if '总市值' in display.columns:
        display['总市值'] = (display['总市值'] / 1e8).round(1)

    return display


def export_csv(df, filename="筛选结果.csv"):
    """导出筛选结果为 CSV"""
    path = OUT_DIR / filename
    df.to_csv(str(path), index=False, encoding="utf-8-sig")
    return str(path)


def get_industries():
    """获取行业列表"""
    try:
        r = requests.get(
            "http://push2.eastmoney.com/api/qt/clist/get",
            params={
                "pn": "1", "pz": "10",
                "po": "1", "np": "1", "fltt": "2", "invt": "2",
                "fid": "f3",
                "fs": "m:90+t:2",
                "fields": "f14",
            },
            timeout=15,
        )
        return {"ok": True, "note": "行业筛选可在获取数据后使用关键词过滤"}
    except Exception:
        return {"ok": True, "industries": []}
