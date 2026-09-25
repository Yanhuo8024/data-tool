#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
company_analysis.py - 公司全方位分析
输入公司名（中文/代码）→ 聚合多维度真实数据 → 输出结构化报告 + 生成 Word 文档

覆盖章节：公司概况 / 财务分析 / 市场表现 / 最新新闻与舆情 / 竞争对手对比 / SWOT / 风险提示

数据源：akshare（A股/港股）。复用 financial_statement / valuation 现有函数。
"""
import re
import io
import base64
from pathlib import Path
from datetime import datetime

try:
    from core_config import OUT_DIR  # 统一入口，避免多处定义分叉
except ImportError:
    OUT_DIR = Path(__file__).parent / "output"  # core_config 不可用时兜底
OUT_DIR.mkdir(exist_ok=True)


def _resolve_cn_name(code: str):
    """股票代码 → 中文名称。依次尝试东财全市场快照表 / 新浪日线，失败返回空。"""
    try:
        import akshare as ak
        raw = code[2:] if str(code).startswith('HK') else code
        if str(code).startswith('HK'):
            df = ak.stock_zh_a_spot_em(symbol='港股')
            if df is not None and not df.empty:
                hit = df[df['代码'].astype(str).str.zfill(5).str.endswith(raw)]
                if not hit.empty:
                    nm = str(hit.iloc[0].get('名称', ''))
                    if nm:
                        return nm
            df = ak.stock_hk_spot_em()
            if df is not None and not df.empty:
                hit = df[df['代码'].astype(str).str.zfill(5).str.endswith(raw)]
                if not hit.empty:
                    c = df.columns
                    nm = str(hit.iloc[0].get('名称', '')) or str(hit.iloc[0].get(c[1], ''))
                    if nm:
                        return nm
            return ""
        df = ak.stock_zh_a_spot_em()
        if df is not None and not df.empty:
            hit = df[df['代码'].astype(str) == raw]
            if not hit.empty:
                nm = str(hit.iloc[0].get('名称', ''))
                if nm:
                    return nm
        sina = ('sh' if raw.startswith('6') else 'sz') + raw
        kdf = ak.stock_zh_a_daily(symbol=sina, adjust="")
        if kdf is not None and hasattr(kdf, 'attrs') and kdf.attrs.get('name'):
            return kdf.attrs['name']
    except Exception:
        pass
    return ""

import sys
sys.path.insert(0, str(Path(__file__).parent))

from financial_statement import fetch_financial_data, calculate_ratios, _resolve_stock_code, _resolve_stock_name
from valuation import _snapshot, dcf_valuation, peer_comparison

# 常见公司名→代码（覆盖 AI 助手主力标的；扩展时同步 ai_assistant._STOCK_ALIASES）
STOCK_ALIASES = {
    '贵州茅台': '600519', '茅台': '600519', '五粮液': '000858', '泸州老窖': '000568',
    '山西汾酒': '600809', '宁德时代': '300750', '隆基绿能': '601012', '中国平安': '601318',
    '招商银行': '600036', '腾讯': 'HK0700', '阿里巴巴': 'HK9988', '美团': 'HK03690',
    '小米': 'HK01810', '比亚迪': '002594', '中兴通讯': '000063', '兆易创新': '603986',
    '韦尔股份': '603501', '寒武纪': '688256', '海光信息': '688041', '中芯国际': '688981',
    '恒瑞医药': '600276', '海康威视': '002415', '东方财富': '300059', '科大讯飞': '002230',
    '工业富联': '601138', '紫光国微': '002049', '三安光电': '600703', '北方华创': '002371',
    '立讯精密': '002475', '牧原股份': '002714', '万科': '000002', '京东方': '000725',
    '星宇股份': '601799', '星宇': '601799',
}

# 内置可比公司映射（同行业对比用）
PEERS_BY_CODE = {
    '600519': ['000858', '000568', '600809'], '000858': ['600519', '000568', '600809'],
    '600036': ['601398', '601939', '000001'], '601318': ['601398', '601939', '600036'],
    'HK0700': ['HK9988', 'HK03690', 'HK01810'], '300750': ['002594', '601127'],
    '600941': ['600050', '601728'], '002594': ['300750', '601127'],
}


def resolve_ticker(company_name: str):
    """公司名/代码 → 统一标识（HK0700 / 6位A股）。返回 (code, market) 或 (None,None)"""
    name = (company_name or '').strip()
    if not name:
        return None, None
    # 直接是代码
    m = re.search(r'HK\s?(\d{4})', name, re.I)
    if m:
        return 'HK' + m.group(1), 'hk'
    m = re.search(r'\b(\d{6})\b', name)
    if m:
        c = m.group(1)
        return c, 'a'
    # 公司名映射
    for k, code in STOCK_ALIASES.items():
        if k in name or name in k:
            return ('HK' + code[2:]) if str(code).startswith('HK') else code, ('hk' if str(code).startswith('HK') else 'a')
    return None, None


def code_to_name(code: str) -> str:
    """股票代码反转名称（优先取别名表最长匹配，保证中文名稳定显示；网络解析作为补充）"""
    for k, c in STOCK_ALIASES.items():
        if c == code:
            # 取最长名（避免 茅台/贵州茅台 取舍）
            return max([k for kk, cc in STOCK_ALIASES.items() if cc == code], key=len)
    return code


def get_company_profile(code: str, market: str) -> dict:
    """公司概况：名称/行业/市值/上市时间/股本等（东财个股信息，失败则回退名称）"""
    out = {"name": code, "industry": "", "market_cap_yi": None, "list_time": "",
           "total_share_yi": None, "desc": ""}
    try:
        import akshare as ak
        q = ak.stock_individual_info_em(symbol=code)
        if isinstance(q, dict) and q:
            out["name"] = q.get("股票简称") or q.get("股票名称") or out["name"]
            out["industry"] = q.get("行业") or ""
            out["list_time"] = str(q.get("上市时间") or "")
            mc = q.get("总市值(元)")
            if mc:
                try:
                    out["market_cap_yi"] = round(float(mc) / 1e8, 1)
                except (ValueError, TypeError):
                    out["market_cap_yi"] = None
            ts = q.get("总股本(股)")
            if ts:
                try:
                    out["total_share_yi"] = round(float(ts) / 1e8, 2)
                except (ValueError, TypeError):
                    out["total_share_yi"] = None
    except Exception:
        pass
    # 名称兜底
    if not out["name"] or out["name"] == code:
        try:
            cn = _resolve_cn_name(code)
            if cn:
                out["name"] = cn
        except Exception:
            pass
    return out


def get_financial_analysis(code: str, market: str) -> dict:
    """财务分析：三表关键数据 + 财务比率 + 杜邦"""
    out = {"ok": False, "ratios": None, "dupont": None}
    fd = fetch_financial_data(code)
    if not fd or not fd.get('ok'):
        return out
    rr = calculate_ratios(fd)
    if rr and rr.get('ok'):
        out["ok"] = True
        out["ratios"] = rr["ratios"]
        out["dupont"] = rr["ratios"].get("dupont")
    return out


def get_market_data(code: str) -> dict:
    """市场表现：现价/PE/PB/52周高低/市值（快照 + 历史K线高低）"""
    out = {"price": None, "pe": None, "pb": None, "roe": None, "gross_margin": None,
           "net_margin": None, "debt_ratio": None, "revenue_yi": None, "net_profit_yi": None,
           "market_cap_yi": None, "high_52": None, "low_52": None, "mkt": "A股"}
    sn = _snapshot(code)
    if not sn or not sn.get("price"):
        return out
    out["price"] = sn.get("price")
    out["pe"] = sn.get("pe")
    out["pb"] = sn.get("pb")
    out["roe"] = round(sn["roe"], 2) if sn.get("roe") else None
    out["gross_margin"] = round(sn["gross_margin"], 2) if sn.get("gross_margin") else None
    out["net_margin"] = round(sn["net_margin"], 2) if sn.get("net_margin") else None
    out["debt_ratio"] = round(sn["debt_ratio"], 2) if sn.get("debt_ratio") else None
    out["revenue_yi"] = round(sn["revenue"] / 1e8, 2) if sn.get("revenue") else None
    out["net_profit_yi"] = round(sn["net_profit"] / 1e8, 2) if sn.get("net_profit") else None
    out["mkt"] = "港股" if sn.get("market") == "hk" else "A股"
    if sn.get("shares"):
        try:
            out["market_cap_yi"] = round(float(sn["price"]) * float(sn["shares"]) / 1e8, 1)
        except Exception:
            out["market_cap_yi"] = None
    # 52周高低（近250交易日K线）
    try:
        import akshare as ak
        if out["mkt"] == "A股":
            sina = ('sh' if code.startswith('6') else 'sz') + code
            df = ak.stock_zh_a_daily(symbol=sina, adjust="")
        else:
            df = ak.stock_hk_daily(symbol=code[2:], adjust="")
        if df is not None and not df.empty:
            tail = df.tail(250)
            out["high_52"] = round(float(tail["high"].max()), 2)
            out["low_52"] = round(float(tail["low"].min()), 2)
    except Exception:
        pass
    return out


def get_dcf_valuation(code: str) -> dict:
    """DCF 绝对估值 + 相对估值（可比公司）"""
    out = {"dcf": None, "peer": None}
    dcf = dcf_valuation(code)
    if dcf and not dcf.get("error"):
        out["dcf"] = dcf
    peers = PEERS_BY_CODE.get(code)
    if peers:
        pc = peer_comparison(code, peers=peers)
        if pc and not pc.get("error"):
            out["peer"] = pc
    return out


def get_news(code: str, limit: int = 6) -> list:
    """最新新闻与舆情：东财个股新闻"""
    news = []
    try:
        import akshare as ak
        df = ak.stock_news_em(symbol=code)
        if df is not None and not df.empty:
            cols = [c for c in ['标题', '时间', '文章来源', '新闻内容'] if c in df.columns]
            for _, row in df.head(limit).iterrows():
                item = {}
                item["title"] = str(row.get('标题', '')).strip()
                item["time"] = str(row.get('时间', '')).strip()
                item["source"] = str(row.get('文章来源', '')).strip()
                item["content"] = str(row.get('新闻内容', '')).strip().replace('\n', ' ')
                if item["title"]:
                    news.append(item)
    except Exception:
        pass
    return news


def get_competitors(code: str) -> list:
    """竞争对手对比：同行业可比公司名称列表"""
    peers = PEERS_BY_CODE.get(code, [])
    rows = []
    for p in peers:
        try:
            sn = _snapshot(p)
            nm = sn.get("name") or code_to_name(p) or p
            rows.append({"code": p, "name": nm if nm != p else code_to_name(p)})
        except Exception:
            rows.append({"code": p, "name": code_to_name(p)})
    return rows


# ==================== SWOT 与风险（规则判定，基于真实财务/市场数据） ====================

def generate_swot(market: dict, fin: dict, profile: dict, dcf: dict) -> dict:
    """基于真实数据生成 SWOT，规则化判定（避免臆断）"""
    strengths, weaknesses = [], []
    opportunities, threats = [], []

    roe = market.get("roe")
    gm = market.get("gross_margin")
    nm = market.get("net_margin")
    dr = market.get("debt_ratio")
    rev_g = fin.get("rev_growth")
    pro_g = fin.get("profit_growth")
    mc = market.get("market_cap_yi")
    pe = market.get("pe")

    # 优势
    if roe is not None and roe >= 15:
        strengths.append(f"盈利能力突出（ROE {roe}%，高于15%的优质企业线）")
    elif roe is not None:
        strengths.append(f"净资产收益率为 {roe}%")
    if gm is not None and gm >= 40:
        strengths.append(f"高毛利率 {gm}%，具备较强定价权与品牌溢价")
    if mc is not None and mc >= 1000:
        strengths.append(f"龙头地位明显，总市值约 {mc} 亿元")
    if nm is not None and nm >= 20:
        strengths.append(f"净利率 {nm}%，经营效率与费用控制良好")

    # 劣势
    if gm is not None and gm < 20:
        weaknesses.append(f"毛利率偏低（{gm}%），议价能力有限或成本压力大")
    if nm is not None and nm < 8:
        weaknesses.append(f"净利率偏低（{nm}%），盈利空间较薄")
    if dr is not None and dr >= 60:
        weaknesses.append(f"资产负债率较高（{dr}%），财务杠杆偏重")
    if rev_g is not None and rev_g < 0:
        weaknesses.append(f"营收同比下滑（{rev_g}%）")
    if pro_g is not None and pro_g is not None and pro_g < 0:
        weaknesses.append(f"净利润同比下滑（{pro_g}%）")
    if roe is not None and roe < 8:
        weaknesses.append(f"ROE 偏低（{roe}%），资本回报效率不足")
    if pe is not None and pe and pe >= 50:
        weaknesses.append(f"PE 估值较高（{pe}），安全边际有限")

    # 机会
    if pe is not None and pe and pe < 20:
        opportunities.append(f"当前 PE {pe} 相对历史与行业偏低，估值存在修复空间")
    if mc is not None and mc < 500:
        opportunities.append(f"市值约 {mc} 亿元，若成长兑现有望获得估值弹性")
    if rev_g is not None and rev_g >= 10:
        opportunities.append(f"营收保持 {rev_g}% 增长，行业景气向上")

    # 威胁
    if rev_g is not None and rev_g < 0:
        threats.append(f"行业需求趋缓（营收同比 {rev_g}%）")
    if dr is not None and dr >= 60:
        threats.append(f"高杠杆在利率上行/融资收紧时构成再融资压力")
    if pe is not None and pe and pe >= 40:
        threats.append(f"高估值对业绩不及预期高度敏感，回撤风险大")

    # 横幅标题
    swot = {
        "strengths": strengths or ["暂缺充足数据",],
        "weaknesses": weaknesses or ["暂缺充足数据",],
        "opportunities": opportunities or ["暂缺充足数据",],
        "threats": threats or ["暂缺充足数据",],
    }
    return swot


def generate_risks(market: dict, fin: dict) -> list:
    """风险提示：基于真实数据归纳"""
    risks = []
    dr = market.get("debt_ratio")
    gm = market.get("gross_margin")
    rev_g = fin.get("rev_growth")
    pro_g = fin.get("profit_growth")
    pe = market.get("pe")

    if dr is not None and dr >= 60:
        risks.append({"level": "高", "name": "财务杠杆风险",
                      "desc": f"资产负债率达 {dr}%，偿债与再融资压力较大。若利率上行或融资环境收紧，财务费用将侵蚀利润。"})
    elif dr is not None and dr >= 40:
        risks.append({"level": "中", "name": "财务杠杆风险", "desc": f"资产负债率 {dr}%，财务结构尚可但需关注负债变化趋势。"})
    if pro_g is not None and pro_g < 0:
        risks.append({"level": "高", "name": "盈利下滑风险", "desc": f"净利润同比下滑 {pro_g}%，成长动能减弱。"})
    if rev_g is not None and rev_g < 0:
        risks.append({"level": "中", "name": "需求放缓风险", "desc": f"营收同比下滑 {rev_g}%，行业景气度承压。"})
    if pe is not None and pe and pe >= 40:
        risks.append({"level": "高", "name": "估值风险", "desc": f"PE {pe} 估值偏高，隐含较高增长预期，业绩不及预期时回撤显著。"})
    if gm is not None and gm < 15:
        risks.append({"level": "中", "name": "竞争议价风险", "desc": f"毛利率 {gm}% 偏低，产品或面临激烈竞争、成本上升压力。"})
    if not risks:
        risks.append({"level": "低", "name": "综合风险", "desc": "当前数据未显示显著风险，但仍需关注行业政策、宏观经济与竞争格局变化。"})
    return risks


# ==================== 综合分析入口 ====================

def analyze_company(company_name: str):
    """公司全方位分析主入口：聚合各维度数据 → 返回结构化 report dict"""
    code, market = resolve_ticker(company_name)
    if not code:
        return {"error": f"无法识别公司：{company_name}。请提供公司名（如 贵州茅台/腾讯）或股票代码（如 600519/HK0700）。"}

    profile = get_company_profile(code, market)
    name = code_to_name(code)
    if name == code:
        name = profile["name"] or code
    profile["name"] = name

    # 财务分析
    fa = get_financial_analysis(code, market)
    ratios = fa.get("ratios") if fa.get("ok") else {}
    fin = {
        "rev_growth": (ratios.get("growth") or {}).get("revenue_growth"),
        "profit_growth": (ratios.get("growth") or {}).get("profit_growth"),
        "rev_yi": (ratios.get("profitability") or {}).get("revenue"),
        "net_profit_yi": (ratios.get("profitability") or {}).get("net_profit"),
        "eps": (ratios.get("profitability") or {}).get("eps"),
        "roe": (ratios.get("profitability") or {}).get("roe"),
        "dupont": ratios.get("dupont"),
    }

    # 市场表现
    market_data = get_market_data(code)

    # 估值
    val = get_dcf_valuation(code)
    dcf, peer = val.get("dcf"), val.get("peer")

    # 新闻
    news = get_news(code)

    # 竞争对手
    competitors = get_competitors(code)

    # SWOT + 风险（规则）
    swot = generate_swot(market_data, fin, profile, val)
    risks = generate_risks(market_data, fin)

    report = {
        "ok": True,
        "name": name,
        "code": code,
        "analyzed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "profile": profile,
        "financial": fin,
        "market": market_data,
        "valuation": {"dcf": dcf, "peer": peer},
        "news": news or [],
        "competitors": competitors,
        "swot": swot,
        "risks": risks,
    }
    return report


# ==================== Word 报告生成（python-docx） ====================

def _add_kv_table(doc, rows):
    """key-value 表格"""
    table = doc.add_table(rows=1, cols=2)
    try:
        table.style = 'Light Grid Accent 1'
    except Exception:
        pass
    hdr = table.rows[0].cells
    hdr[0].text, hdr[1].text = '指标', '数值'
    for k, v in rows:
        cells = table.add_row().cells
        cells[0].text = str(k)
        cells[1].text = str(v if v is not None else '-')
    doc.add_paragraph("")


def generate_docx(report: dict) -> bytes:
    """生成 Word 报告，返回 docx bytes"""
    from docx import Document
    from docx.shared import Pt, RGBColor

    doc = Document()
    name = report.get("name", "")
    doc.add_heading(f"{name} 全方位分析报告", 0)
    doc.add_paragraph(f"分析时间：{report.get('analyzed_at', '')}    股票代码：{report.get('code', '')}")
    doc.add_paragraph("数据来源：akshare 公开金融数据接口（A股/港股）。仅供研究参考，不构成投资建议。")

    # 一、公司概况
    doc.add_heading("一、公司概况", 1)
    p = report.get("profile", {})
    _add_kv_table(doc, [
        ("公司名称", p.get("name")), ("所属行业", p.get("industry")),
        ("总市值", f"{p.get('market_cap_yi')} 亿元" if p.get('market_cap_yi') else None),
        ("总股本", f"{p.get('total_share_yi')} 亿股" if p.get('total_share_yi') else None),
        ("上市时间", p.get("list_time")),
    ])

    # 二、财务分析
    doc.add_heading("二、财务分析", 1)
    f = report.get("financial", {})
    _add_kv_table(doc, [
        ("营业收入(亿)", f.get("rev_yi")), ("净利润(亿)", f.get("net_profit_yi")),
        ("营收同比", f"{f.get('rev_growth')}%" if f.get('rev_growth') is not None else None),
        ("净利同比", f"{f.get('profit_growth')}%" if f.get('profit_growth') is not None else None),
        ("EPS(元)", f.get("eps")), ("ROE(%)", f.get("roe")),
    ])
    dup = f.get("dupont")
    if dup:
        doc.add_heading("杜邦分析", 2)
        _add_kv_table(doc, [
            ("ROE(%)", dup.get("roe")), ("净利率(%)", dup.get("net_margin")),
            ("总资产周转率", dup.get("asset_turnover")), ("权益乘数", dup.get("equity_multiplier")),
        ])
        doc.add_paragraph("杜邦拆解：ROE = 净利率 × 总资产周转率 × 权益乘数，可定位盈利能力来源。", style="Intense Quote")


    # 三、市场表现
    doc.add_heading("三、市场表现", 1)
    m = report.get("market", {})
    _add_kv_table(doc, [
        ("当前股价(元)", m.get("price")), ("市场", m.get("mkt")),
        ("总市值(亿)", m.get("market_cap_yi")), ("PE", m.get("pe")),
        ("PB", m.get("pb")), ("ROE(%)", m.get("roe")),
        ("毛利率(%)", m.get("gross_margin")), ("净利率(%)", m.get("net_margin")),
        ("资产负债率(%)", m.get("debt_ratio")), ("52周最高", m.get("high_52")),
        ("52周最低", m.get("low_52")),
    ])

    # 四、估值
    doc.add_heading("四、估值分析", 1)
    dcf = report.get("valuation", {}).get("dcf")
    if dcf:
        doc.add_heading("DCF 绝对估值", 2)
        _add_kv_table(doc, [
            ("现价(元)", dcf.get("price")), ("每股内在价值(元)", dcf.get("per_share")),
            ("偏差(%)", dcf.get("spread_pct")), ("结论", dcf.get("verdict_cn")),
        ])
    peer = report.get("valuation", {}).get("peer")
    if peer and peer.get("verdict"):
        doc.add_heading("相对估值（可比公司）", 2)
        doc.add_paragraph(peer["verdict"]["label"])
        rows = peer.get("peers", [])
        if rows:
            table = doc.add_table(rows=1, cols=5)
            try:
                table.style = 'Light Grid Accent 1'
            except Exception:
                pass
            hdr = table.rows[0].cells
            for i, h in enumerate(["代码", "名称", "PE", "PB", "ROE(%)"]):
                hdr[i].text = h
            for r in rows:
                cells = table.add_row().cells
                cells[0].text = str(r.get("code", "-"))
                cells[1].text = str(r.get("name", "-"))
                cells[2].text = str(r.get("pe", "-"))
                cells[3].text = str(r.get("pb", "-"))
                cells[4].text = str(r.get("roe", "-"))
            doc.add_paragraph("")

    # 五、最新新闻与舆情
    doc.add_heading("五、最新新闻与舆情", 1)
    news = report.get("news", [])
    if news:
        for i, n in enumerate(news[:6], 1):
            doc.add_paragraph(f"{i}. {n.get('time','')} [{n.get('source','')}] {n.get('title','')}", style="List Number")
            if n.get("content"):
                doc.add_paragraph(f"   {n['content'][:120]}", style="Intense Quote")
    else:
        doc.add_paragraph("未获取到相关新闻（数据源暂时不可用或无覆盖）。")

    # 六、竞争对手对比
    doc.add_heading("六、竞争对手对比", 1)
    comps = report.get("competitors", [])
    if comps:
        for c in comps:
            doc.add_paragraph(f"{c.get('name')}（{c.get('code')}）")
    else:
        doc.add_paragraph("暂无已配置的同行业竞争对手映射。")

    # 七、SWOT 分析
    doc.add_heading("七、SWOT 分析", 1)
    sw = report.get("swot", {})
    for label, key in [("优势 (Strengths)", "strengths"), ("劣势 (Weaknesses)", "weaknesses"),
                        ("机会 (Opportunities)", "opportunities"), ("威胁 (Threats)", "threats")]:
        doc.add_heading(label, 2)
        for item in sw.get(key, []):
            doc.add_paragraph(f"• {item}")

    # 八、风险提示
    doc.add_heading("八、风险提示", 1)
    table = doc.add_table(rows=1, cols=3)
    try:
        table.style = 'Light Grid Accent 1'
    except Exception:
        pass
    hdr = table.rows[0].cells
    for i, h in enumerate(["风险等级", "风险项", "说明"]):
        hdr[i].text = h
    for r in report.get("risks", []):
        doc_row = table.add_row().cells
        doc_row[0].text = str(r.get("level", "-"))
        doc_row[1].text = str(r.get("name", "-"))
        doc_row[2].text = str(r.get("desc", "-"))
    doc.add_paragraph("")

    # 免责声明
    para = doc.add_paragraph()
    para.add_run("\n免责声明：本报告基于 akshare 公开数据自动生成，数据可能存在延迟或误差，仅供研究参考，不构成任何投资建议。投资有风险，入市需谨慎。")
    para.runs[0].font.size = Pt(9)
    para.runs[0].font.color.rgb = RGBColor(0x88, 0x88, 0x88)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def generate_docx_report_file(report: dict) -> str:
    """生成 docx 并保存到 output 目录，返回文件路径"""
    data = generate_docx(report)
    fname = f"{report.get('name')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_analysis.docx"
    path = OUT_DIR / fname
    path.write_bytes(data)
    return str(path)