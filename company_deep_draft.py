#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
company_deep_draft.py — 企业深析草稿层（从 company_deep_pipeline 拆出）

内容：
  - 共享小工具（_f/_s/_round/_append_text）
  - state 默认骨架（_deep_defaults/_merge）
  - 规则草稿（_rule_draft）/ LLM 草稿（_ai_draft）/ 草稿阶段（stage_draft）

pipeline（stage_resolve…run_pipeline）只负责取数与编排；文案生成集中在本模块。
"""
from __future__ import annotations

import logging

import company_deep_rules as _rules
import company_deep_narrative as _narr

logger = logging.getLogger(__name__)

def _f(v, default=None):
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default

def _s(v, default=""):
    if v is None:
        return default
    return str(v)

def _round(v, nd=1):
    x = _f(v)
    return None if x is None else round(x, nd)


def _append_text(base, note):
    """追加说明文本（实现已收敛到 company_deep_narrative.append_text）。"""
    return _narr.append_text(base, note)


def _as_dict(v) -> dict:
    """把 ctx 里可能不是 dict 的值安全归一化。

    🚨 必要性：`ctx["market"]` 历史上是**多态**的 —— `stage_resolve` 先写入市场标签
    字符串（`'a'`/`'hk'`），`stage_market` 再覆写成行情 dict。一旦 `stage_market`
    失败或被跳过（例如本次新增的"阶段超时"路径），`ctx["market"]` 就还是字符串，
    而 `"a" or {}` 是**真值**、不会回落 → 下游 `.get()` 直接 AttributeError。
    规则草稿是**最后一道兜底**，绝不能因为上游类型异常而崩掉（否则 warn 变 error）。
    """
    return v if isinstance(v, dict) else {}


def _as_list(v) -> list:
    return v if isinstance(v, list) else []


def _rule_draft(ctx: dict) -> dict:
    """无 LLM 时的规则文案草稿，保证流水线可离线跑通（并对上游类型异常免疫）。"""
    name = ctx.get("name") or "该公司"
    profile = _as_dict(ctx.get("profile"))
    market = _as_dict(ctx.get("market"))
    fa = _as_dict(_as_dict(ctx.get("financial_analysis")).get("ratios"))
    prof = _as_dict(fa.get("profitability"))
    grow = _as_dict(fa.get("growth"))
    solv = _as_dict(fa.get("solvency"))
    oper = _as_dict(fa.get("operational"))
    statements = _as_dict(ctx.get("statements"))
    competitors = _as_list(ctx.get("competitors"))
    news = _as_list(ctx.get("news"))
    swot = _as_dict(ctx.get("swot"))
    payout = statements.get("payout")

    industry = profile.get("industry") or "所属行业"
    if not profile.get("industry"):
        industry = "所属行业"
    gm = prof.get("gross_margin")
    nm = prof.get("net_margin")
    roe = prof.get("roe")
    pe = market.get("pe")
    dr = market.get("debt_ratio")
    rev_g = grow.get("revenue_growth")
    np_g = grow.get("profit_growth")

    biz_type = "品牌消费" if (gm or 0) >= 50 else ("制造/渠道" if (gm or 0) >= 25 else "低毛利/代工")

    # 回款模式推断：应收账款周转天数是应收政策的最直接观测
    rdays = oper.get("receivable_days")
    if rdays is None or rdays <= 10:
        payment = "先款后货"
    elif rdays <= 45:
        payment = "现款现货"
    elif rdays <= 120:
        payment = "信用期垫资"
    else:
        payment = "项目制回款"
    # 护城河六维：0-10 连续打分（单一实现在 company_deep_rules，规则层/报告层/AI 工具共用）
    _mf = _rules.norm_fin({
        "gross_margin": prof.get("gross_margin"), "net_margin": prof.get("net_margin"),
        "roe": prof.get("roe"), "debt_ratio": solv.get("debt_ratio"),
        "asset_turnover": (fa.get("dupont") or {}).get("asset_turnover"),
        "receivable_days": rdays})
    _mt = _rules.moat_scores(
        _mf, {"industry": profile.get("industry"), "market_cap_yi": market.get("market_cap_yi")},
        ctx.get("industry_stats_em") or {})

    price_ok = "合理"
    if pe:
        if pe < 15:
            price_ok = "低估"
        elif pe < 30:
            price_ok = "合理"
        elif pe < 50:
            price_ok = "偏高"
        else:
            price_ok = "高估"

    rival = ""
    if competitors:
        try:
            rival = "、".join(
                str(c.get("name") or c.get("名称") or c) for c in competitors[:3] if c
            )
        except Exception:
            rival = str(competitors[:3])

    news_line = ""
    if news:
        try:
            news_line = "；".join(str(n.get("title") or n.get("标题") or "") for n in news[:3] if n)
        except Exception:
            news_line = ""

    is_hk = str(ctx.get("code") or "").startswith("HK")
    hk_note = ("港股说明：免费数据渠道暂无港股行业聚合数据，"
               "行业规模/增速/市占率/CR3/CR5 需人工从行业研报或公司年报补充。") if is_hk else ""

    what = f"依托{industry}赛道，向客户提供产品/服务并获取收入"
    if is_hk and not profile.get("industry"):
        what = "港股暂无行业分类数据源，行业名称需人工填写（同花顺/富途个股页可查）"
    if gm is not None:
        what += f"；毛利率 {gm}%"
    what += "。"

    desc = str(profile.get("desc") or "").strip()
    if len(desc) > 110:
        desc = desc[:110] + "…"

    # 收入结构：优先取真实分部数据。A 股可直取；港股无公开接口 → 给出**可执行缺口**
    # （缺什么/去哪取/期望字段），不再写"需人工补充"这种无法落地的兜底。
    try:
        from segment_data import fetch_segments
        _seg = fetch_segments(ctx.get("code") or "", market="hk" if is_hk else "a")
        if _seg.get("ok") and _seg.get("segments"):
            _top = " / ".join(
                f"{x['name']} {x['share']}%" for x in _seg["segments"][:4]
                if x.get("share") is not None)
            rev_mix = f"{_top}（报告期 {_seg.get('period')}；来源：东方财富主营构成）"
        else:
            _g = _seg.get("gap") or {}
            rev_mix = (f"分部收入未能自动获取（{_g.get('reason', '未知原因')}）。"
                       f"应查：{_g.get('source', '年报「分部资料」附注')}；"
                       f"需补字段：{' / '.join(_g.get('fields') or [])}。")
    except Exception as _e:
        logger.debug('分部数据获取失败: %s', _e)
        rev_mix = desc or "收入结构需对照年报分部信息补充"

    # 净利变动归因：下滑或大幅波动时**必须给原因**；取不到给可执行缺口，不编数字、不臆测页码。
    try:
        from profit_attribution import analyze as _pa_analyze
        _pa = _pa_analyze(ctx.get("snapshot") or ctx, market="hk" if is_hk else "a")
        profit_attr = _pa if (_pa.get("diagnosis") or {}).get("needed") else None
    except Exception as e:
        logger.debug("净利归因失败: %s", e)
        profit_attr = None

    # 现金流：接口已获取就必须展示（用户要求）——只降级缺失项，不整段隐藏
    try:
        from cash_flow import analyze as _cf_analyze
        _cf = _cf_analyze(ctx)
        cash_flow = _cf if _cf.get("ok") else None
    except Exception as e:
        logger.debug("现金流体检失败: %s", e)
        cash_flow = None

    swot_s = swot.get("strengths") or []
    swot_w = swot.get("weaknesses") or []
    dividend_txt = (
        f"最近年报分红支出占净利润约 {payout}%（现金流量表口径，含利息支付，近似值）。" if payout is not None
        else "分红政策需人工核实（年报「利润分配」章节）。"
    )

    return {
        # 净利变动归因（下滑/大幅波动时非空；含「已尝试—未获取—建议查」三段式缺口）
        "profitAttribution": profit_attr,
        # 现金流体检（OCF/FCF/消耗速度；OCF 取到即非空）
        "cashFlow": cash_flow,
        "company": {
            "oneliner": f"{name}主营{industry}相关业务，通过产品与渠道实现盈利。",
        },
        "business": {
            "what": what,
            "ceiling": f"行业空间与市占率提升共同决定天花板；当前市值约 {market.get('market_cap_yi') or '—'} 亿元。",
            "kfs": "渠道与客户资源、成本控制、产品力与品牌、政策与牌照（如适用）。",
            "position": f"PE {pe if pe is not None else '—'}，ROE {roe if roe is not None else '—'}%，处于行业中游水平（需人工校准）。" + hk_note,
            "lifecycle": "成熟期" if (rev_g is not None and rev_g < 8) else "成长期",
            "policy": "中性",
            "score": "3",
        },
        "model": {
            "customer": "终端客户 / 渠道客户（需人工细化）",
            "value": f"产品与服务满足{industry}需求；净利率 {nm if nm is not None else '—'}%。",
            "channel": "直销与经销结合（需人工校准）",
            "revenue": "主营收入为主，其他业务为辅",
            "revMix": rev_mix,
            "cost": "营业成本 + 销售/管理/研发费用",
            "cash": "先款后货或信用期（需人工核对回款）",
            "weakest": "客户/供应商集中度与回款质量（需人工核实）",
            "formula": "收入 × 毛利率 − 费用 = 净利润；增长看量价与费用率",
            "gm": _s(gm),
            "nm": _s(nm),
            "bizType": biz_type,
            "modelScore": "3",
            "pricing": "3",
            "payment": payment,
        },
        "moat": {
            **_rules.strip_metrics(_mt),
            "shareTrend": "市占率变化需结合行业数据人工补充",
            "marginPeers": f"毛利率 {gm if gm is not None else '—'}%，与同行差距请对照可比公司",
            "whyChoose": "基于产品力/渠道/服务形成选择理由（AI 草稿为初稿）",
            "whyBlocked": "规模、渠道、品牌或牌照形成进入壁垒（初稿）",
        },
        "gov": {
            "controller": "需工商穿透后填写",
            "structure": "结合企查查/年报股权章节补充",
            "stable": "3",
            "related": "关联交易规模需人工核对年报关联交易章节",
            "dividend": dividend_txt,
            "promise": "历史承诺兑现情况需人工检索公告核实",
            "incentive": "股权激励/员工持股计划需人工检索公告核实",
            "score": "3",
        },
        "val": {
            "pe": _s(market.get("pe")),
            "pb": _s(market.get("pb")),
            "verdict": price_ok,
            "reason": f"PE {pe if pe is not None else '—'} / PB {market.get('pb') or '—'}；DCF 与历史分位需进一步核对。",
        },
        "pb": {
            "rival": rival or "行业主要竞争对手",
            "move": "价格、渠道、产品迭代是常见打法",
            "impact": "中等",
            "strength": (swot_s[0] if swot_s else "结合财报与访谈明确我方核心优势"),
            "weak": (swot_w[0] if swot_w else "结合财报与访谈明确我方软肋"),
            "next": "跟踪对手产品迭代、价格与渠道动作，按季度复盘",
            "conclusion": "结合护城河与财务弹性判断能否防守（初稿）",
            "invest": "若判断卡结论为「可关注」，评估当前仓位与安全边际",
        },
        "sources": {
            "ipo": news_line or "请补充招股书行业与风险因素摘录",
            "mda": f"营收同比 {rev_g if rev_g is not None else '—'}%，净利同比 {np_g if np_g is not None else '—'}%；请补充 MD&A 原文要点。",
            "audit": "请核对审计意见与监管问询",
            "research": "可叠加券商研报观点作交叉验证",
        },
        "meta": {
            "source": "rules",
            "dr": dr,
            "high_52": market.get("high_52"),
            "low_52": market.get("low_52"),
        },
    }


def _ai_draft(ctx: dict, provider_config: dict | None = None) -> dict:
    """LLM 草稿：把规则结果 + 行业上下文整理成分析初稿。

    facts / 提示词 / JSON 容错解析都在 `company_deep_llm`（叶子模块），本函数只管调用与重试。

    2026-10-02 实测（贵州茅台，真实网络）定位到「草稿阶段撞 60s 超时」的根因：
      · 现行参数（`max_tokens=2200`、未关思考）单次 **24.83s**，输出 1672 字符**被截断**→不可解析；
      · 重试用的仍是同一套参数 → 二次截断 → 累计 ≈50s，再叠加数据阶段即越过 60s 预算；
      · 关掉思考后（`reasoning_effort='none'`）单次 **19.24s**，输出 2255 字符**可解析**。
    `llm_client` 自己的注释也写明了这个机制：「思考会挤占 max_tokens 预算导致正文截断」。

    因此这里三件事同时做：**① 关思考**（省时且不吃预算）、
    **② 读 `finish_reason`**（`'length'` 即截断，不再盲目当"格式错误"）、
    **③ 重试递增预算**（截断时把 max_tokens 调大再试，而不是原样重发）。
    """
    from llm_client import chat
    from company_deep_llm import build_facts, build_prompt, parse_json_block, REPAIR_NUDGE

    name = ctx.get("name") or "该公司"
    facts = build_facts(ctx)
    messages = [
        {"role": "system", "content": "你是严谨的证券研究助理，只输出合法 JSON。"},
        {"role": "user", "content": build_prompt(name, facts)},
    ]

    # 关思考：实测更快且不再截断（见 docstring）。调用方若显式给了 reasoning_effort 则尊重之。
    cfg = dict(provider_config or {})
    cfg.setdefault("reasoning_effort", "none")

    budget = 2200
    last_err = None
    for attempt in (1, 2):
        resp = chat(messages, cfg, max_tokens=budget)
        content = (resp or {}).get("content") or ""
        data = parse_json_block(content)
        if data is not None:
            data["meta"] = {"source": "llm", "used_provider": (resp or {}).get("used_provider"),
                            "attempts": attempt, "facts_keys": sorted(facts.keys())}
            return data
        truncated = (resp or {}).get("finish_reason") == "length"
        last_err = ValueError(
            "LLM 输出被 max_tokens 截断" if truncated else "LLM 未返回可解析的 JSON 对象")
        logger.warning("AI draft 第 %d 次失败（截断=%s, 输出 %d 字符）",
                       attempt, truncated, len(content))
        if attempt == 1:
            # 截断 → 给足预算；否则只是格式问题 → 明确要纯 JSON
            budget = 3600 if truncated else budget
            messages = messages + [{"role": "user", "content": REPAIR_NUDGE}]
    raise last_err


def stage_draft(company: str, ctx: dict, use_ai: bool = True, llm_config: dict | None = None) -> dict:
    if use_ai:
        try:
            draft = _ai_draft(ctx, llm_config)
            ctx["draft"] = draft
            return {"source": "llm", "ok": True}
        except Exception as e:
            logger.warning("AI draft failed, fallback to rules: %s", e)
            draft = _rule_draft(ctx)
            ctx["draft"] = draft
            # AI 失败不再记成成功（项目原则「不把已有失败写成通过」）：
            # 返回 ok=False + 结构化 error_code，由 _emit 映射为 warn 状态。
            return {"source": "rules", "ok": False, "error_code": "AI_DRAFT_FALLBACK",
                    "error": str(e)}
    draft = _rule_draft(ctx)
    ctx["draft"] = draft
    return {"source": "rules", "ok": True, "error_code": None}


def _deep_defaults() -> dict:
    return {
        "company": {"name": "", "code": "", "industry": "", "market": "A股", "date": "", "analyst": "", "oneliner": ""},
        "sources": {"primary": {}, "secondary": {}, "ipo": "", "mda": "", "audit": "", "research": ""},
        "business": {
            "size": "", "growth": "", "share": "", "lifecycle": "", "cr3": "", "cr5": "",
            "policy": "", "score": "", "what": "", "ceiling": "", "kfs": "", "position": "",
        },
        "model": {
            "customer": "", "value": "", "channel": "", "revenue": "", "cost": "", "cash": "", "weakest": "",
            "revMix": "", "cust1": "", "cust5": "", "payment": "", "pricing": "",
            "formula": "", "gm": "", "nm": "", "bizType": "", "modelScore": "",
        },
        "moat": {
            "tech": 5, "brand": 5, "channel": 5, "cost": 5, "network": 5, "license": 5,
            "shareTrend": "", "marginPeers": "", "whyChoose": "", "whyBlocked": "", "dur": "",
        },
        "finance": {
            "rev": "", "np": "", "gp": "", "ocf": "", "fcf": "", "assets": "", "equity": "",
            "cash": "", "idebt": "", "arG": "", "invG": "", "revG": "", "roe": "", "gw": "",
            "payout": "", "npG": "", "gmD": "",
        },
        "gov": {
            "controller": "", "hold": "", "pledge": "", "structure": "", "stable": "",
            "related": "", "dividend": "", "promise": "", "incentive": "", "score": "",
        },
        "risks": [],
        "val": {
            "pe": "", "pePct": "", "pb": "", "pbPct": "", "ps": "", "peerPe": "", "peerPb": "",
            "dcfFcf": "", "dcfG": "2", "dcfWacc": "9", "dcfShares": "", "dcfNetcash": "0",
            "verdict": "", "reason": "",
        },
        "red": {"fin": {}, "biz": {}, "verdict": "", "worst": ""},
        "pb": {
            "strength": "", "weak": "", "next": "", "rival": "", "move": "", "impact": "",
            "steps": [], "conclusion": "", "invest": "",
        },
        "conclusions": {"a1": "", "a2": "", "a3": "", "a4": "", "a5": ""},
    }


def _merge(base: dict, extra: dict):
    if not isinstance(extra, dict):
        return base
    for k, v in extra.items():
        if k == "meta":
            continue
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        elif v is not None and v != "":
            base[k] = v
    return base
