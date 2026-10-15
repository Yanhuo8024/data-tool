#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
company_deep_assemble.py — 企业深析"组装分析卡"阶段（从 company_deep_pipeline 拆出）

stage_assemble：把各阶段结果（ctx）+ 草稿合并成与前端 company-deep.js state
完全对齐的分析卡。硬数据（财报/估值/行业聚合）覆盖草稿软信息。
"""
from __future__ import annotations

import logging
from datetime import datetime

import company_deep_rules as _rules
import company_deep_narrative as _narr
from company_deep_draft import _s, _deep_defaults, _merge, _append_text, _rule_draft, _as_dict

logger = logging.getLogger(__name__)


def stage_assemble(company: str, ctx: dict) -> dict:
    """把各阶段结果组装成前端 deep state。"""
    state = _deep_defaults()
    draft = ctx.get("draft") or _rule_draft(ctx)
    profile = _as_dict(ctx.get("profile"))
    # 🚨 必须是 dict：`ctx["market"]` 现由 stage_market 独占写入（resolve 只写 market_kind），
    #    该阶段失败/超时则键缺失；历史上的类型多态曾在此 `"a".get(...)` 打挂整个 assemble。
    market = _as_dict(ctx.get("market"))
    fa = _as_dict(_as_dict(ctx.get("financial_analysis")).get("ratios"))
    prof = _as_dict(fa.get("profitability"))
    grow = _as_dict(fa.get("growth"))
    solv = _as_dict(fa.get("solvency"))
    dup = fa.get("dupont") or {}
    val_raw = (ctx.get("valuation_raw") or {})
    peer = val_raw.get("peer") or {}

    # 公司
    state["company"].update({
        "name": ctx.get("name") or "",
        "code": ctx.get("code") or "",
        "industry": profile.get("industry") or "",
        "market": "港股" if market.get("mkt") == "港股" or str(ctx.get("market_kind")) == "hk" else "A股",
        "date": datetime.now().strftime("%Y-%m-%d"),
        "analyst": "自动流水线",
    })

    # 财务（硬数据优先）
    fin = state["finance"]
    if prof.get("revenue") is not None:
        fin["rev"] = str(prof["revenue"])
        # 毛利 ≈ 收入 × 毛利率
        gm = prof.get("gross_margin")
        if gm is not None:
            try:
                fin["gp"] = str(round(float(prof["revenue"]) * float(gm) / 100.0, 2))
            except Exception as _e:
                logger.debug("gp calc swallowed: %s", _e)
    if prof.get("net_profit") is not None:
        fin["np"] = str(prof["net_profit"])
    if prof.get("roe") is not None:
        fin["roe"] = str(prof["roe"])
    if grow.get("revenue_growth") is not None:
        fin["revG"] = str(grow["revenue_growth"])
    if grow.get("profit_growth") is not None:
        fin["npG"] = str(grow["profit_growth"])
    # 资产负债表 / 现金流量表绝对值（新浪三表）
    st = ctx.get("statements") or {}
    for k in ("assets", "equity", "cash", "idebt", "ocf", "fcf", "gw", "arG", "invG", "gmD", "payout"):
        if st.get(k) is not None:
            fin[k] = str(st[k])
    # 新浪三表缺失时，用资产负债率反推净资产兜底
    dr = solv.get("debt_ratio")
    if not fin.get("assets") and not fin.get("equity") and dr is not None \
            and prof.get("revenue") is not None:
        dup_em = (dup or {}).get("equity_multiplier")
        try:
            if dup_em and float(dup_em) > 0:
                rev_yi = float(prof["revenue"])
                ato = (dup or {}).get("asset_turnover")
                if ato and float(ato) > 0:
                    fin["assets"] = str(round(rev_yi / float(ato), 1))
                    fin["equity"] = str(round(rev_yi / float(ato) / float(dup_em), 1))
        except Exception as _e:
            logger.debug("assets backfill swallowed: %s", _e)

    # 模型毛利净利
    if prof.get("gross_margin") is not None:
        state["model"]["gm"] = str(prof["gross_margin"])
    if prof.get("net_margin") is not None:
        state["model"]["nm"] = str(prof["net_margin"])

    # 估值
    if market.get("pe") is not None:
        state["val"]["pe"] = str(market["pe"])
    if market.get("pb") is not None:
        state["val"]["pb"] = str(market["pb"])
    if ctx.get("ps") is not None:
        state["val"]["ps"] = str(ctx["ps"])
    if ctx.get("pe_pct") is not None:
        state["val"]["pePct"] = str(ctx["pe_pct"])
    if ctx.get("pb_pct") is not None:
        state["val"]["pbPct"] = str(ctx["pb_pct"])
    if ctx.get("dcf_fcf_yi") is not None:
        state["val"]["dcfFcf"] = str(round(float(ctx["dcf_fcf_yi"]), 1))
    if ctx.get("dcf_shares_yi") is not None:
        state["val"]["dcfShares"] = str(ctx["dcf_shares_yi"])
    if ctx.get("dcf_netcash_yi") is not None:
        state["val"]["dcfNetcash"] = str(round(float(ctx["dcf_netcash_yi"]), 1))
    # 同业估值中位数：优先 PEERS_BY_CODE 的可比公司，其次行业营收前 N（industry_peers 已带 PE）
    _peer_rows = list((peer.get("peers") or [])) if isinstance(peer, dict) else []
    if not _peer_rows:
        _peer_rows = [{"pe": c.get("pe"), "pb": c.get("pb")} for c in (ctx.get("competitors") or [])
                      if isinstance(c, dict)]
    try:
        pes = [float(p.get("pe")) for p in _peer_rows if p.get("pe") not in (None, "", "-")]
        if pes:
            state["val"]["peerPe"] = str(round(sorted(pes)[len(pes) // 2], 2))
        pbs = [float(p.get("pb")) for p in _peer_rows if p.get("pb") not in (None, "", "-")]
        if pbs:
            state["val"]["peerPb"] = str(round(sorted(pbs)[len(pbs) // 2], 2))
    except (TypeError, ValueError) as _e:
        logger.debug("同业估值中位数计算跳过: %s", _e)

    # 风险（规则）
    risk_map = {"高": "高", "中": "中", "低": "低"}
    for r in (ctx.get("risks_rule") or []):
        state["risks"].append({
            "type": "财务" if "财务" in _s(r.get("name")) or "杠杆" in _s(r.get("name")) else (
                "估值" if "估值" in _s(r.get("name")) else (
                    "经营" if "竞争" in _s(r.get("name")) or "需求" in _s(r.get("name")) else "经营"
                )
            ),
            "level": risk_map.get(_s(r.get("level"), "中"), "中"),
            "text": f"{_s(r.get('name'))}：{_s(r.get('desc'))}"[:200],
        })

    # 排雷自动勾选（基于规则风险）
    high_risk = [r for r in state["risks"] if r["level"] == "高"]
    if high_risk:
        state["red"]["verdict"] = "中度" if len(high_risk) < 2 else "重度"
        state["red"]["worst"] = high_risk[0]["text"][:80]
    else:
        state["red"]["verdict"] = "轻度" if state["risks"] else "干净"

    # 合并草稿（软信息）
    _merge(state, draft)

    # 二次校正：硬数据覆盖草稿
    if prof.get("revenue") is not None:
        state["finance"]["rev"] = str(prof["revenue"])
    if prof.get("net_profit") is not None:
        state["finance"]["np"] = str(prof["net_profit"])
    if prof.get("roe") is not None:
        state["finance"]["roe"] = str(prof["roe"])
    if market.get("pe") is not None:
        state["val"]["pe"] = str(market["pe"])
    if market.get("pb") is not None:
        state["val"]["pb"] = str(market["pb"])
    if prof.get("gross_margin") is not None:
        state["model"]["gm"] = str(prof["gross_margin"])
    if prof.get("net_margin") is not None:
        state["model"]["nm"] = str(prof["net_margin"])
    # DCF 每股内在价值追加进估值逻辑（需在草稿合并后执行，避免被覆盖）
    dcf = (ctx.get("valuation_raw") or {}).get("dcf") or {}
    if dcf.get("per_share") is not None:
        state["val"]["reason"] = _append_text(
            state["val"].get("reason"),
            f"DCF 每股内在价值约 {dcf['per_share']} 元（现价 {dcf.get('price', '—')} 元）")

    # 行业聚合文案（东财营收口径优先，巨潮利润口径兜底）已抽到 company_deep_narrative
    biz = state["business"]
    _narr.apply_industry(biz, ctx.get("industry_stats_em") or {}, ctx.get("industry_agg") or {})

    # 自动结论（若草稿未给）
    if not state["conclusions"].get("a1"):
        what = _s(state['business'].get('what'), '业务本质待补充').rstrip("。")
        state["conclusions"]["a1"] = (
            f"{_s(state['company'].get('name'), '该公司')}处于{_s(state['business'].get('lifecycle'), '待判')}阶段的"
            f"{_s(state['company'].get('industry'), '所属行业')}行业。{what}。"
            f"行业评分 {_s(state['business'].get('score'), '—')}/5。"
        )
    if not state["conclusions"].get("a2"):
        state["conclusions"]["a2"] = (
            f"护城河初评见六维打分；毛利率 {_s(prof.get('gross_margin'), '—')}%，"
            f"净利率 {_s(prof.get('net_margin'), '—')}%。{_s(state['moat'].get('whyChoose'), '壁垒需人工强化论证')}。"
        )
    if not state["conclusions"].get("a3"):
        ocf_txt = "经营现金流需人工补充"
        state["conclusions"]["a3"] = (
            f"财务健康度由营收/利润/ROE/负债率勾稽：ROE {_s(prof.get('roe'), '—')}%，"
            f"毛利率 {_s(prof.get('gross_margin'), '—')}%，资产负债率 {_s(solv.get('debt_ratio'), '—')}%。{ocf_txt}。"
        )
    if not state["conclusions"].get("a5"):
        state["conclusions"]["a5"] = (
            f"PE {_s(market.get('pe'), '—')}，PB {_s(market.get('pb'), '—')}；"
            f"{_s(state['val'].get('verdict'), '待估值校准')}。"
        )
    if not state["conclusions"].get("a4") and state["risks"]:
        state["conclusions"]["a4"] = "已识别风险：" + "；".join(r["text"][:40] for r in state["risks"][:3])

    # 信源提示
    if not state["sources"].get("ipo"):
        state["sources"]["ipo"] = "【自动】请补读招股书行业/风险章节；当前初稿来自公开行情与规则引擎。"
    if not state["sources"].get("mda"):
        state["sources"]["mda"] = (
            f"【自动】营收同比 {_s(grow.get('revenue_growth'), '—')}%，"
            f"净利同比 {_s(grow.get('profit_growth'), '—')}%；请对照年报 MD&A 核验。"
        )

    # ── 竞品兜底（P1 修复）────────────────────────────────────
    # `PEERS_BY_CODE` 只有硬编码的 8 个代码，是竞品/同业估值的唯一来源 →
    # 8 家之外 `pb.rival` 退化为占位符。现用"行业营收前 N / 竞品列表"兜底。
    _rival = _s(state["pb"].get("rival")).strip()
    if not _rival or _rival in ("行业主要竞争对手", "最大对手", "需人工核实"):
        _names = [str(c.get("name") or c) for c in (ctx.get("competitors") or []) if c]
        if not _names:
            _names = [str(x) for x in ((ctx.get("industry_stats_em") or {}).get("peers_top") or [])
                      if str(x).strip() and str(x) != _s(ctx.get("name"))]
        if _names:
            state["pb"]["rival"] = "、".join(_names[:3])

    # ── 排雷清单回写（P0 修复）────────────────────────────────
    # 把 `company_deep_rules.redflag_alerts()`（唯一实现）按清单 id 回写，
    # 并把完整列表放进 `state.red.alerts` 供网页与导出文档共用同一口径。
    alerts = ctx.get("redflag_alerts") or []
    if not alerts:
        alerts = _rules.redflag_alerts(_rules.norm_fin(fin))
    for a in alerts:
        if a.get("id"):
            state["red"]["fin"][a["id"]] = {"done": True, "note": a.get("note") or ""}
    state["red"]["alerts"] = [{"label": a.get("label"), "note": a.get("note"),
                               "level": a.get("level"), "id": a.get("id")} for a in alerts]
    _dangers = [a for a in alerts if a.get("level") == "danger"]
    if _dangers:
        # 有 danger 级信号时，结论不得停留在「轻度」
        state["red"]["verdict"] = "重度"
        state["red"]["worst"] = state["red"]["worst"] or _dangers[0]["label"]
    elif alerts and state["red"]["verdict"] == "干净":
        state["red"]["verdict"] = "轻度"

    meta = {
        "pipeline": True,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "code": ctx.get("code"),
        "name": ctx.get("name"),
        "draft_source": (ctx.get("draft") or {}).get("meta", {}).get("source", "rules"),
        "dcf": dcf,
        "news_count": len(ctx.get("news") or []),
        "competitor_count": len(ctx.get("competitors") or []),
    }
    # 不进入前端 state 字段映射，单独返回
    return {"state": state, "meta": meta}
