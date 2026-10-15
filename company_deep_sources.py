#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
company_deep_sources.py — 企业深析 · 信源检索

把「企业深析 · 信息收集」清单里的信源变成可获取的接口：
按信源 id + 公司信息构造检索词，复用 web_search（百度→必应→DuckDuckGo 链），
返回标题 / 链接 / 摘要。跳转 URL 由前端拼装（company-deep.js / tab-datasources.html），
本模块只负责"获取信息"半边。

出境开关：ALLOW_WEB_SEARCH=0 时 web_search 内部直接拒绝外呼，本模块原样透传。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_CNINFO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "http://www.cninfo.com.cn/",
}
_CNINFO_ORG_CACHE = {}

# 信源定义：kw 是拼进检索词的关键词；site 为空则不限定域名
SOURCE_DEFS = {
    "ipo":      {"title": "招股说明书",       "kw": "招股说明书"},
    "ar":       {"title": "年报·财务指标",    "kw": "年度报告 主要财务指标"},
    "mda":      {"title": "年报·MD&A",       "kw": "年度报告 经营情况讨论与分析"},
    "cninfo":   {"title": "巨潮资讯网",       "kw": "公告"},
    "exchange": {"title": "交易所官网",       "kw": "上市公司 监管信息"},
    "ir":       {"title": "投资者关系页",     "kw": "投资者关系 业绩说明会"},
    "edgar":    {"title": "SEC EDGAR",       "kw": "annual report 10-K"},
    "qcc":      {"title": "企查查 / 天眼查",  "kw": "工商信息 股东 股权结构"},
    "court":    {"title": "裁判文书 / 执行信息", "kw": "裁判文书 被执行 失信"},
    "ip":       {"title": "知识产权局",       "kw": "专利 发明"},
    "report":   {"title": "券商 / 行业研报",  "kw": "研究报告 券商点评"},
    "wind":     {"title": "Wind / 同花顺",   "kw": "估值 市盈率 同业对比"},
    "assoc":    {"title": "行业协会 / 统计局", "kw": "行业协会 统计数据"},
}


def _market_of(market: str, code: str) -> str:
    """归一化市场：hk / us / a / other"""
    m = (market or "").strip().lower()
    c = (code or "").strip().upper()
    if m in ("港股", "hk", "hkex"):
        return "hk"
    if m in ("美股", "us"):
        return "us"
    if m in ("a股", "a", "a-share", "ashare"):
        return "a"
    if c.startswith("HK") or (c.isdigit() and len(c) == 5):
        return "hk"
    if c.isdigit() and len(c) == 6:
        return "a"
    return m or "a"


def _a_board(code: str) -> str:
    """A股板块：sh / sz / neeq"""
    c = (code or "").strip()
    if not (c.isdigit() and len(c) == 6):
        return ""
    if c[0] == "6":
        return "sh"
    if c[0] in ("0", "2", "3"):
        return "sz"
    if c[0] in ("4", "8"):
        return "neeq"
    return ""


def build_query(sid: str, name: str = "", code: str = "", industry: str = "", market: str = "") -> str | None:
    """按信源构造检索词；未知信源返回 None"""
    defn = SOURCE_DEFS.get(sid)
    if not defn:
        return None
    kw = defn["kw"]
    subject = (name or code or "").strip()
    mkt = _market_of(market, code)
    board = _a_board(code)

    if sid == "edgar" or (mkt == "us" and sid in ("ipo", "ar", "mda", "cninfo", "exchange")):
        subject = (code or name or "").strip()
        return f"{subject} {kw} site:sec.gov".strip()
    if mkt == "hk" and sid in ("ipo", "ar", "mda", "cninfo", "exchange"):
        return f"{subject} {'年報 業務回顧' if sid == 'mda' else kw} site:hkexnews.hk".strip()
    if sid == "assoc":
        subj = (industry or subject).strip()
        return f"{subj} {kw}".strip()
    if sid == "exchange":
        if board == "sh":
            return f"{subject} {code} site:sse.com.cn"
        if board == "sz":
            return f"{subject} {code} site:szse.cn"
        if board == "neeq":
            return f"{subject} {code} site:neeq.com.cn"
        return f"{subject} {kw}".strip()
    # 披露类信源限定巨潮
    if sid in ("ipo", "ar", "mda", "cninfo"):
        return f"{subject} {kw} site:cninfo.com.cn".strip()
    return f"{subject} {kw}".strip()


def _cninfo_org_id(code: str) -> str | None:
    """巨潮：代码 → orgId（进程内缓存）"""
    code = str(code).strip()
    if code in _CNINFO_ORG_CACHE:
        return _CNINFO_ORG_CACHE[code]
    import requests
    r = requests.post(
        "http://www.cninfo.com.cn/new/information/topSearch/query",
        data={"keyWord": code, "maxNum": 10}, headers=_CNINFO_HEADERS, timeout=8,
    )
    for item in (r.json() or []):
        if str(item.get("code")) == code:
            _CNINFO_ORG_CACHE[code] = item.get("orgId")
            return item.get("orgId")
    return None


def _cninfo_announcements(sid: str, code: str, top_k: int) -> list[dict]:
    """巨潮公开公告接口：A股披露类信源的一手结果（标题 / 公告 PDF 链接 / 日期）"""
    org_id = _cninfo_org_id(code)
    if not org_id:
        return []
    category = "category_ndbg_szsh" if sid in ("ar", "mda") else ""
    searchkey = "招股" if sid == "ipo" else ""
    import re as _re
    from datetime import datetime as _dt
    import requests

    data = {
        "pageNum": 1, "pageSize": max(1, min(top_k, 10)),
        "column": "sse" if str(code).startswith("6") else "szse",
        "tabName": "fulltext", "plate": "", "stock": f"{code},{org_id}",
        "searchkey": searchkey, "secid": "", "category": category,
        "trade": "", "seDate": "", "sortName": "", "sortType": "", "isHLtitle": "true",
    }
    r = requests.post(
        "http://www.cninfo.com.cn/new/hisAnnouncement/query",
        data=data, headers=_CNINFO_HEADERS, timeout=8,
    )
    out = []
    for a in (r.json().get("announcements") or [])[: max(1, min(top_k, 10))]:
        title = _re.sub(r"</?em>", "", a.get("announcementTitle") or "")
        adj = a.get("adjunctUrl") or ""
        ts = a.get("announcementTime")
        try:
            date = _dt.fromtimestamp(int(ts) / 1000).strftime("%Y-%m-%d") if ts else ""
        except Exception:
            date = ""
        out.append({
            "title": title[:200],
            "url": ("http://static.cninfo.com.cn/" + adj) if adj else "",
            "snippet": ((a.get("secName") or "") + (" · 公告日期 " + date if date else ""))[:300],
        })
    return out


def fetch_source(sid: str, name: str = "", code: str = "", industry: str = "",
                 market: str = "", top_k: int = 6) -> dict:
    """抓取单个信源下该公司的公开资料，返回 {ok, items: [{title,url,snippet}], query}"""
    defn = SOURCE_DEFS.get(sid)
    if not defn:
        return {"ok": False, "error": f"未知信源：{sid}"}
    subject = (name or code or "").strip()
    if not subject:
        return {"ok": False, "error": "请先填写公司名称或代码"}

    # A 股披露类信源优先走巨潮公开公告接口（一手、结构化、稳定），失败退回网页搜索
    board = _a_board(code)
    if sid in ("ipo", "ar", "mda", "cninfo") and board:
        try:
            items = _cninfo_announcements(sid, code, max(1, min(int(top_k or 6), 10)))
            if items:
                return {
                    "ok": True,
                    "source_id": sid,
                    "title": defn["title"],
                    "query": f"巨潮资讯网 · {subject}",
                    "items": items,
                    "via": "cninfo",
                }
        except Exception as e:
            logger.warning("cninfo 接口失败（退回网页搜索）%s: %s", sid, e)

    query = build_query(sid, name=name, code=code, industry=industry, market=market)
    if not query:
        return {"ok": False, "error": f"未知信源：{sid}"}

    from web_search import web_search
    res = web_search(query, top_k=max(1, min(int(top_k or 6), 10)))
    if not res.get("ok"):
        return {
            "ok": False,
            "source_id": sid,
            "title": defn["title"],
            "query": query,
            "error": res.get("error") or "检索失败",
            "items": [],
        }
    return {
        "ok": True,
        "source_id": sid,
        "title": defn["title"],
        "query": query,
        "items": res.get("results") or [],
    }


def list_sources() -> list[dict]:
    """信源清单（供调试 / 前端展示）"""
    return [{"id": k, **v} for k, v in SOURCE_DEFS.items()]


# ── 同行业可比公司（供护城河 / 同业估值对比回填）────────────────────
_PEER_CACHE = {}


def industry_peers(code: str, name: str = "", top_k: int = 3) -> list[dict]:
    """按东财业绩报表的「所处行业」找出同行业**营收前 N 名**（排除自己），带 PE/PB。

    为什么需要它：`company_analysis.PEERS_BY_CODE` 是**硬编码的 8 个代码**，
    且是 `get_competitors()` 与 `get_dcf_valuation()` 取可比公司的**唯一来源**
    → 这 8 家之外，`pb.rival` 退化为占位符、同级估值中位数为空、竞品数为 0。
    行业成分其实早就拿到了（`get_industry_stats_em()` 的 `peers_top`），
    只是从未回填到竞品/估值对比里。

    返回 `[{name, code, pe, pb}]`；任一环节不可达返回 `[]`（不猜、不占位）。
    """
    code = str(code or '').strip()
    if not code:
        return []
    ck = (code, int(top_k))
    if ck in _PEER_CACHE:
        return _PEER_CACHE[ck]
    out: list[dict] = []
    try:
        from company_analysis import _yjbb_df, _recent_report_date, _num_or_none
        cur_d = _recent_report_date()
        df = _yjbb_df(cur_d) if cur_d else None
        if df is None or '所处行业' not in df.columns or '股票代码' not in df.columns:
            _PEER_CACHE[ck] = out
            return out
        row = df[df['股票代码'].astype(str) == code]
        if row.empty:
            _PEER_CACHE[ck] = out
            return out
        ind_name = str(row.iloc[-1].get('所处行业') or '').strip()
        if not ind_name:
            _PEER_CACHE[ck] = out
            return out
        ind = df[df['所处行业'].astype(str) == ind_name].copy()
        rev_col = '营业总收入-营业总收入'
        ind['_rev'] = ind[rev_col].map(_num_or_none) if rev_col in ind.columns else None
        ind = ind[ind['_rev'].notna() & (ind['_rev'] > 0)]
        top = ind.nlargest(max(2, int(top_k) + 1), '_rev')
        from valuation import _snapshot
        for _, r in top.iterrows():
            pcode = str(r.get('股票代码') or '').strip()
            if not pcode or pcode == code:
                continue
            item = {'name': str(r.get('股票简称') or '').strip(), 'code': pcode, 'pe': None, 'pb': None}
            try:
                sn = _snapshot(pcode) or {}
                item['pe'] = sn.get('pe')
                item['pb'] = sn.get('pb')
            except Exception as e:
                logger.debug("industry_peers: 取 %s 快照失败: %s", pcode, e)
            out.append(item)
            if len(out) >= int(top_k):
                break
    except Exception as e:
        logger.warning("industry_peers 失败: %s", e)
    _PEER_CACHE[ck] = out
    return out
