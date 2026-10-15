#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""company_deep_narrative.py — 企业深析的**叙述层**规则（SWOT / 行业聚合文案）

与 `company_deep_rules.py`（数值层：归一化 / 排雷 / 健康分 / 护城河）分开的原因：
两者都属"规则"，但数值层产出**结构化数字**、叙述层产出**中文文案**，
变动频率与审阅方式完全不同；且 `company_deep_rules` 已达 400 行门禁上限。

依赖方向：叶子模块（仅标准库 + 同层 `company_deep_rules` 的 `_ramp` 语义不复用）。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# ── SWOT：补上原先被忽略的 profile / dcf 两个维度 ──────────────
def generate_swot(market: dict, fin: dict, profile: dict = None, dcf: dict = None) -> dict:
    """规则化 SWOT。**旧实现在 `company_analysis.generate_swot`，其中 `profile` 与 `dcf`
    两个入参从未被使用**（"行业位置"与"DCF 估值"两个机会/风险维度实为空缺）；此处补齐。
    """
    market = market or {}
    fin = fin or {}
    profile = profile or {}
    dcf = dcf or {}
    strengths, weaknesses, opportunities, threats = [], [], [], []

    roe = market.get('roe')
    gm = market.get('gross_margin')
    nm = market.get('net_margin')
    dr = market.get('debt_ratio')
    rev_g = fin.get('rev_growth')
    pro_g = fin.get('profit_growth')
    mc = market.get('market_cap_yi')
    pe = market.get('pe')

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
    # profile 维度（原先被忽略）：行业地位与行业规模
    industry, share = profile.get('industry'), profile.get('share_rev') or market.get('share_rev')
    if industry and share is not None and share >= 15:
        strengths.append(f"在「{industry}」行业内营收份额约 {share}%，具备行业影响力")

    if gm is not None and gm < 20:
        weaknesses.append(f"毛利率偏低（{gm}%），议价能力有限或成本压力大")
    if nm is not None and nm < 8:
        weaknesses.append(f"净利率偏低（{nm}%），盈利空间较薄")
    if dr is not None and dr >= 60:
        weaknesses.append(f"资产负债率较高（{dr}%），财务杠杆偏重")
    if rev_g is not None and rev_g < 0:
        weaknesses.append(f"营收同比下滑（{rev_g}%）")
    if pro_g is not None and pro_g < 0:
        weaknesses.append(f"净利润同比下滑（{pro_g}%）")
    if roe is not None and roe < 8:
        weaknesses.append(f"ROE 偏低（{roe}%），资本回报效率不足")
    if pe is not None and pe and pe >= 50:
        weaknesses.append(f"PE 估值较高（{pe}），安全边际有限")
    if not industry:
        weaknesses.append("行业分类缺失，行业地位与竞争格局判断缺少基准")

    if pe is not None and pe and pe < 20:
        opportunities.append(f"当前 PE {pe} 相对历史与行业偏低，估值存在修复空间")
    if mc is not None and mc < 500:
        opportunities.append(f"市值约 {mc} 亿元，若成长兑现有望获得估值弹性")
    if rev_g is not None and rev_g >= 10:
        opportunities.append(f"营收保持 {rev_g}% 增长，行业景气向上")
    # dcf 维度（原先被忽略）：内在价值 vs 现价
    per_share, price = dcf.get('per_share'), dcf.get('price')
    try:
        if per_share is not None and price and float(price) > 0:
            up = (float(per_share) / float(price) - 1) * 100
            if up >= 15:
                opportunities.append(f"DCF 每股内在价值约 {per_share} 元，较现价 {price} 元有 {up:.0f}% 空间")
            elif up <= -15:
                threats.append(f"DCF 每股内在价值约 {per_share} 元，低于现价 {price} 元 {abs(up):.0f}%，估值偏贵")
    except (TypeError, ValueError) as e:
        logger.debug("generate_swot: DCF 每股价值比较跳过（per_share=%r price=%r）: %s",
                     per_share, price, e)

    if rev_g is not None and rev_g < 0:
        threats.append(f"行业需求趋缓（营收同比 {rev_g}%）")
    if dr is not None and dr >= 60:
        threats.append("高杠杆在利率上行/融资收紧时构成再融资压力")
    if pe is not None and pe and pe >= 40:
        threats.append("高估值对业绩不及预期高度敏感，回撤风险大")

    return {
        'strengths': strengths or ["暂缺充足数据"],
        'weaknesses': weaknesses or ["暂缺充足数据"],
        'opportunities': opportunities or ["暂缺充足数据"],
        'threats': threats or ["暂缺充足数据"],
    }


# ── 行业聚合文案（原在 company_deep_pipeline.stage_assemble 内，约 57 行）──
def _s(v, default='') -> str:
    return default if v is None else str(v)


def append_text(base, note):
    """追加说明文本：前文以句号结尾时换行，否则分号衔接。"""
    b = _s(base).strip()
    if not b:
        return note
    sep = "\n" if b.endswith(("。", "！", "？", ".")) else "；"
    return b + sep + note


def _r1(v) -> str:
    try:
        return str(round(float(v), 1))
    except (TypeError, ValueError):
        return _s(v)


def apply_industry(biz: dict, em: dict, agg: dict) -> dict:
    """把行业聚合结果写进 `state["business"]`（原地修改并返回）。

    口径优先级：**东财业绩报表（营收口径）优先，巨潮（利润口径）兜底**。
    口径差异必须写进 `position` 文案 —— 否则"行业增速""市占率"两个数字
    在两种口径下含义不同，用户无从分辨。
    """
    if not isinstance(biz, dict):
        return biz or {}
    em = em or {}
    agg = agg or {}
    if em.get("industry"):
        if em.get("industry_rev_yi") is not None:
            biz["size"] = str(em["industry_rev_yi"])
        if em.get("rev_yoy") is not None:
            biz["growth"] = str(em["rev_yoy"])
        if em.get("share_rev") is not None:
            biz["share"] = str(em["share_rev"])
        if em.get("cr3") is not None:
            biz["cr3"] = str(em["cr3"])
        if em.get("cr5") is not None:
            biz["cr5"] = str(em["cr5"])
        parts = [f"东财「{em['industry']}」{em.get('company_count', '—')} 家上市公司"]
        if em.get("industry_rev_yi") is not None:
            parts.append(f"营收合计 {_r1(em['industry_rev_yi'])} 亿元")
        if em.get("rev_yoy") is not None:
            parts.append(f"营收同比 {_r1(em['rev_yoy'])}%")
        if em.get("cr3") is not None:
            parts.append(f"CR3 {_r1(em['cr3'])}%")
        if em.get("cr5") is not None:
            parts.append(f"CR5 {_r1(em['cr5'])}%")
        if em.get("peers_top"):
            parts.append("营收前五：" + "、".join(str(x) for x in em["peers_top"][:5]))
        note = f"行业口径（东财业绩报表 {em.get('report_date', '')}，营收口径）：" + "、".join(parts) + "。"
        biz["position"] = append_text(biz.get("position"), note)
    elif agg:
        if agg.get("net_profit_yoy") is not None:
            biz["growth"] = str(agg["net_profit_yoy"])
        if agg.get("share_profit") is not None:
            biz["share"] = str(agg["share_profit"])
        parts = []
        if agg.get("company_count") is not None:
            parts.append(f"{_s(agg.get('industry'), '同行业')} {int(agg['company_count'])} 家上市公司")
        if agg.get("net_profit_yi") is not None:
            parts.append(f"净利润合计 {_r1(agg['net_profit_yi'])} 亿元")
        if agg.get("net_profit_yoy") is not None:
            parts.append(f"同比 {_r1(agg['net_profit_yoy'])}%")
        if agg.get("total_mcap_yi") is not None:
            parts.append(f"总市值 {_r1(agg['total_mcap_yi'])} 亿元")
        if parts:
            note = (f"行业口径（巨潮 {agg.get('report_date', '')}）：" + "、".join(parts) +
                    "；行业增速为行业净利润同比，市占率为公司利润占比（利润口径）。"
                    "CR3/CR5 需研报或行业协会口径人工填写。")
            biz["position"] = append_text(biz.get("position"), note)
            if agg.get("total_mcap_yi") is not None:
                cap_note = f"同行业上市公司总市值合计约 {_r1(agg['total_mcap_yi'])} 亿元（{agg.get('report_date', '')}）。"
                biz["ceiling"] = append_text(biz.get("ceiling"), cap_note)
    # 同业对比回填：行业营收前五（排除本公司）作为竞品兜底
    peers_top = [str(x) for x in (em.get("peers_top") or []) if str(x).strip()]
    if peers_top:
        biz["peers_top"] = peers_top[:5]
    return biz
