#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
company_deep_pipeline.py — 企业深度分析自动化流水线（编排层）

流水线阶段（漏斗）：
  1 resolve   识别公司 / 代码
  2 profile   公司概况（akshare）
  3 finance   财务指标 + 杜邦
  4 market    行情 / 估值 / 同业
  5 news      新闻与竞品
  6 risk      规则 SWOT + 风险排雷
  7 draft     AI 草稿（可选，失败自动降级规则文案）→ company_deep_draft
  8 assemble  组装「企业深析」完整 state            → company_deep_assemble

输出结构与前端 company-deep.js 的 state 完全对齐。
本模块只做"取数 + 编排"；文案在 company_deep_draft，组装在 company_deep_assemble。
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from typing import Callable

import company_deep_rules as _rules
import company_deep_narrative as _narr

logger = logging.getLogger(__name__)

# 单阶段墙钟预算（秒）：管线含 5+ 次外网调用，无预算时单点阻塞会拖死整个请求。
# 0 或负数关闭（回归测试/离线探针用 CD_STAGE_TIMEOUT=0）。
STAGE_TIMEOUT_S = float(os.environ.get('CD_STAGE_TIMEOUT', '60'))

# 阶段名（前端进度展示用）
STAGE_NAMES = [
    ("resolve", "识别公司"),
    ("profile", "公司概况"),
    ("finance", "财务与杜邦"),
    ("market", "行情与估值"),
    ("news", "新闻与竞品"),
    ("risk", "规则排雷"),
    ("draft", "智能草稿"),
    ("assemble", "组装分析卡"),
]

# ── 兼容 re-export（草稿/组装实现拆到子模块，外部引用点保持不变）──
from company_deep_draft import (  # noqa: E402,F401
    _f, _s, _round, _append_text, _rule_draft, _ai_draft, stage_draft,
    _deep_defaults, _merge, _as_dict,
)
from company_deep_assemble import stage_assemble  # noqa: E402,F401


def stage_resolve(company: str, ctx: dict) -> dict:
    from company_analysis import resolve_ticker, code_to_name, STOCK_ALIASES

    code, market = resolve_ticker(company)
    if not code:
        # 宽松再试：纯数字
        raw = (company or "").strip()
        if raw.isdigit() and len(raw) == 6:
            code, market = raw, "a"
        else:
            raise ValueError(f"无法识别公司：{company}。请提供名称（贵州茅台/腾讯）或代码（600519/HK0700）")
    name = code_to_name(code)
    if name == code:
        name = company
    ctx.update({
        "code": code,
        "market_kind": market,       # 市场标签 str（a/hk/us）：profile/finance 读这个
        "name": name,
        "aliases": [k for k, v in STOCK_ALIASES.items() if v == code],
        # 🚨 这里**故意不写** `ctx["market"]`：该键专供「行情 dict」，只由 stage_market 写入。
        #    历史上一度在此写入市场标签字符串、随后被 stage_market 覆写为 dict（同一键两种类型）；
        #    一旦 stage_market 失败或超时，它就滞留为字符串 → 下游 `"a".get(...)` → AttributeError
        #    把 assemble 阶段打成致命错误（整条管线 ok=False）。键名分离后该类型陷阱不再可能复现。
    })
    return {"code": code, "market": market, "name": name}


def stage_profile(company: str, ctx: dict) -> dict:
    from company_analysis import get_company_profile

    profile = get_company_profile(ctx["code"], ctx.get("market_kind") or "a")
    if profile.get("name") and profile["name"] != ctx["code"]:
        ctx["name"] = profile["name"]
    ctx["profile"] = profile
    return profile


def stage_finance(company: str, ctx: dict) -> dict:
    from company_analysis import get_financial_analysis

    fa = get_financial_analysis(ctx["code"], ctx.get("market_kind") or "a")
    ctx["financial_analysis"] = fa
    ctx["statements"] = (fa or {}).get("statements") or {}
    ratios = (fa or {}).get("ratios") or {}
    prof = ratios.get("profitability") or {}
    solv = ratios.get("solvency") or {}
    grow = ratios.get("growth") or {}
    dup = ratios.get("dupont") or {}
    return {
        "ok": bool(fa.get("ok")),
        "revenue": prof.get("revenue"),
        "net_profit": prof.get("net_profit"),
        "gross_margin": prof.get("gross_margin"),
        "net_margin": prof.get("net_margin"),
        "roe": prof.get("roe"),
        "eps": prof.get("eps"),
        "debt_ratio": solv.get("debt_ratio"),
        "current_ratio": solv.get("current_ratio"),
        "revenue_growth": grow.get("revenue_growth"),
        "profit_growth": grow.get("profit_growth"),
        "dupont": dup,
    }


def _valuation_percentiles(code: str):
    """百度股市通 PE(TTM)/PB 全历史分位（%），不可达返回 (None, None)"""
    try:
        import akshare as ak
        res = {}
        for key, ind in (("pe", "市盈率(TTM)"), ("pb", "市净率")):
            try:
                df = ak.stock_zh_valuation_baidu(symbol=code, indicator=ind, period="全部")
                if df is None or df.empty or "value" not in df:
                    continue
                vals = df["value"].astype(float).dropna()
                if vals.empty:
                    continue
                res[key] = round(float((vals <= float(vals.iloc[-1])).mean() * 100), 1)
            except Exception as _e:
                logger.debug("valuation percentiles swallowed: %s", _e)
                continue
        return res.get("pe"), res.get("pb")
    except Exception:
        return None, None


def stage_market(company: str, ctx: dict) -> dict:
    from company_analysis import get_market_data, get_dcf_valuation

    market = get_market_data(ctx["code"])
    val = get_dcf_valuation(ctx["code"])
    ctx["market"] = market
    ctx["valuation_raw"] = val
    # 供 assemble 回填估值表单：DCF 组件 / PS / 历史分位
    dcf = val.get("dcf") or {}
    ctx["dcf_fcf_yi"] = dcf.get("base_fcf_yi")
    ctx["dcf_netcash_yi"] = -float(dcf["net_debt_yi"]) if dcf.get("net_debt_yi") is not None else None
    try:
        shares = dcf.get("shares")
        ctx["dcf_shares_yi"] = round(float(shares) / 1e8, 2) if shares else None
    except Exception:
        ctx["dcf_shares_yi"] = None
    mc, rev = market.get("market_cap_yi"), market.get("revenue_yi")
    try:
        ctx["ps"] = round(float(mc) / float(rev), 2) if (mc and rev) else None
    except Exception:
        ctx["ps"] = None
    ctx["pe_pct"], ctx["pb_pct"] = _valuation_percentiles(ctx["code"])
    # 行业聚合：优先东财业绩报表（营收口径：规模/增速/市占率/CR3/CR5），失败回退巨潮（利润口径）
    em_stats = {}
    try:
        from company_analysis import get_industry_stats_em
        em_stats = get_industry_stats_em(ctx["code"]) or {}
    except Exception as e:
        logger.warning("em industry stats failed: %s", e)
    ctx["industry_stats_em"] = em_stats
    ctx["industry_agg"] = {}
    if not em_stats:
        try:
            from company_analysis import get_industry_aggregates
            ctx["industry_agg"] = get_industry_aggregates(
                (ctx.get("profile") or {}).get("industry") or "",
                ((ctx.get("financial_analysis") or {}).get("ratios") or {})
                .get("profitability", {}).get("net_profit"))
        except Exception as e:
            logger.warning("industry aggregates failed: %s", e)
    return {
        "market": market,
        "valuation": val,
    }


def stage_news(company: str, ctx: dict) -> dict:
    from company_analysis import get_news, get_competitors

    news = get_news(ctx["code"], limit=8) or []
    competitors = get_competitors(ctx["code"]) or []
    # 竞品回填：PEERS_BY_CODE 只有硬编码的 8 个代码，之外竞品为 0。
    # 行业成分其实已由东财业绩报表拿到 → 用它兜底（同行业营收前 3，带 PE/PB）。
    if not competitors:
        try:
            from company_deep_sources import industry_peers
            competitors = industry_peers(ctx["code"], name=ctx.get("name") or "", top_k=3)
        except Exception as e:
            logger.warning("industry peers fallback failed: %s", e)
    ctx["news"] = news
    ctx["competitors"] = competitors
    return {"news_count": len(news), "competitors": competitors}


def stage_risk(company: str, ctx: dict) -> dict:
    from company_analysis import generate_risks

    # `_as_dict` 护栏：`ctx["market"]` 只应由 stage_market 写成 dict，
    # 但该阶段失败/超时时键可能缺失或残留为市场标签字符串（历史包袱）。
    market = _as_dict(ctx.get("market"))
    ratios = (ctx.get("financial_analysis") or {}).get("ratios") or {}
    fin = {
        "rev_growth": market.get("rev_growth") or (ratios.get("growth") or {}).get("revenue_growth"),
        "profit_growth": (ratios.get("growth") or {}).get("profit_growth"),
    }
    # SWOT 走 company_deep_narrative 版本：旧实现在 company_analysis 里
    # `profile`/`dcf` 两个入参**从未被使用**（"行业位置"与"DCF 估值"两个维度实为空缺），
    # 此处补齐；规则层与报告层共用同一实现。
    profile = ctx.get("profile") or {}
    val = ctx.get("valuation_raw") or {}
    swot = _narr.generate_swot(market, fin, profile, val.get("dcf") or {})
    risks = generate_risks(market, fin)
    # 排雷清单（单一实现）：用规范化财务视图（ratios + 三表），缺 np 会漏「现金流背离」
    ctx["swot"] = swot
    ctx["risks_rule"] = risks
    ctx["redflag_alerts"] = _rules.redflag_alerts(_rules.norm_fin(_rules.finance_view(ctx)))
    return {"swot": swot, "risks": risks, "redflags": len(ctx["redflag_alerts"])}


# ── 主入口 ────────────────────────────────────────────
def run_pipeline(
    company: str,
    use_ai: bool = True,
    llm_config: dict | None = None,
    on_stage: Callable[[str, str, str], None] | None = None,
) -> dict:
    """
    跑完整条企业深析流水线。
    on_stage(stage_id, status, message) 可选回调：status in pending/ok/warn/error

    ⚠️ 这里**不再**写 `from company_deep_draft import stage_draft` —— 本模块顶部已 re-export
    同名符号，函数内再 import 会**遮蔽模块属性**，导致 `monkeypatch.setattr(pipeline, 'stage_draft')`
    打不中（用例会静默跑到真实实现上）。模块级符号即唯一入口。
    """

    ctx: dict = {}
    stages = []
    t0 = datetime.now()

    def _emit(sid, status, msg, error_code=None):
        item = {"id": sid, "status": status, "message": msg}
        if error_code:
            item["error_code"] = error_code        # 结构化失败码（前端可判读，不靠解析中文）
        stages.append(item)
        if on_stage:
            try:
                on_stage(sid, status, msg)
            except Exception as e:
                logger.debug("on_stage 回调异常: %s", e)

    def _run_bounded(fn):
        """给单个阶段套墙钟预算，超时立刻返回 `(None, 错误说明)`。

        🚨 两条都被实测证伪过的错误做法：
          ① `with ThreadPoolExecutor(...) as ex:` —— `__exit__` 会 `shutdown(wait=True)`，
             阻塞到工作线程真正结束 → 「超时」只丢结果不省时间；
          ② 「常驻 ThreadPoolExecutor + fut.result(timeout)」—— 超时后**孤儿线程仍占着池槽位**，
             后续阶段排队等它，整体耗时照样等于被超时那个阶段的时长（实测 0.2s 预算、
             1.0s 阶段，管线仍耗时 1.00s）。

        正确做法：**每个阶段一个独立 daemon 线程**，`join(超时)` 到点即弃。
        daemon 保证孤儿线程永不阻塞进程退出，且不占用任何共享槽位。
        """
        if STAGE_TIMEOUT_S <= 0:
            return fn(), None
        box = {}

        def _worker():
            try:
                box['v'] = fn()
            except (Exception, SystemExit) as e:      # 与项目"旁路增强"同口径；禁捕 BaseException
                box['err'] = e
        th = threading.Thread(target=_worker, daemon=True, name='cd-stage')
        th.start()
        th.join(STAGE_TIMEOUT_S)
        if th.is_alive():
            return None, f"阶段超过 {int(STAGE_TIMEOUT_S)}s 未返回"
        if 'err' in box:
            raise box['err']
        return box.get('v'), None

    runners = [
        ("resolve", lambda: stage_resolve(company, ctx)),
        ("profile", lambda: stage_profile(company, ctx)),
        ("finance", lambda: stage_finance(company, ctx)),
        ("market", lambda: stage_market(company, ctx)),
        ("news", lambda: stage_news(company, ctx)),
        ("risk", lambda: stage_risk(company, ctx)),
        ("draft", lambda: stage_draft(company, ctx, use_ai=use_ai, llm_config=llm_config)),
        ("assemble", lambda: stage_assemble(company, ctx)),
    ]

    assembled = None
    hard_error = None
    for sid, fn in runners:
        try:
            result, _to_err = _run_bounded(fn)
            # 不变量：每跑完一个阶段，`ctx["market"]` 要么不存在、要么是 dict。
            # 摆在 `_run_bounded` 之后、分支之前 → 即使**下一个** market 阶段超时/抛错，
            # 它此时也已被归零为 dict，下游 `.get(...)` 永远不会撞上字符串。
            if not isinstance(ctx.get("market"), dict):
                ctx["market"] = {}
            if _to_err:
                if sid in ("resolve", "assemble"):
                    _emit(sid, "warn", _to_err, "STAGE_TIMEOUT")
                    hard_error = TimeoutError(_to_err)
                    break
                # 草稿超时**不得留空**：立刻退回规则草稿（否则用户白等 60s 拿到空字段）
                if sid == "draft":
                    try:
                        from company_deep_draft import _rule_draft
                        ctx["draft"] = _rule_draft(ctx)
                        _emit(sid, "warn", "AI 草稿超预算，已改用规则草稿（字段非空）",
                              "AI_DRAFT_TIMEOUT")
                    except Exception as e:
                        logger.exception("规则草稿兜底失败")
                        _emit(sid, "warn", f"AI 草稿超预算且规则兜底失败：{str(e)[:120]}",
                              "AI_DRAFT_TIMEOUT")
                else:
                    _emit(sid, "warn", _to_err, "STAGE_TIMEOUT")
                continue
            if sid == "assemble":
                assembled = result
                _emit(sid, "ok", f"分析卡组装完成（草稿来源：{(result.get('meta') or {}).get('draft_source')}）")
            else:
                msg = "完成"
                status = "ok"
                code = None
                if sid == "draft" and isinstance(result, dict):
                    msg = f"草稿来源：{result.get('source')}" + (f"（{result.get('error', '')[:40]}）" if result.get("error") else "")
                    # AI 失败不得记成成功：置 warn 并给出结构化错误码
                    if result.get("ok") is False:
                        status, code = "warn", result.get("error_code") or "AI_DRAFT_FALLBACK"
                elif sid == "finance" and isinstance(result, dict) and not result.get("ok"):
                    msg, status = "财报接口无数据，仅保留行情字段", "warn"
                elif sid == "news" and isinstance(result, dict):
                    msg = f"新闻 {result.get('news_count', 0)} 条，竞品 {len(result.get('competitors') or [])} 个"
                elif sid == "risk" and isinstance(result, dict):
                    n = int(result.get("redflags") or 0)
                    msg = f"排雷命中 {n} 条" if n else "排雷未触发主要信号"
                _emit(sid, status, msg, code)
        except Exception as e:
            logger.exception("pipeline stage %s failed", sid)
            _emit(sid, "error", str(e)[:200], type(e).__name__)
            if sid in ("resolve", "assemble"):
                hard_error = e
                break

    if hard_error or not assembled:
        return {
            "ok": False,
            "error": str(hard_error) if hard_error else "流水线未完成",
            "stages": stages,
            "elapsed_sec": round((datetime.now() - t0).total_seconds(), 2),
        }

    # 降级登记：让导出文档 / 前端能看出"哪些阶段没拿到数据"，而不是把空缺当正常
    degraded = [{"id": s["id"], "status": s["status"], "error_code": s.get("error_code"),
                 "message": s.get("message")} for s in stages if s.get("status") in ("warn", "error")]
    assembled["meta"]["degraded_stages"] = degraded
    assembled["meta"]["ok_stages"] = len([s for s in stages if s.get("status") == "ok"])
    assembled["meta"]["total_stages"] = len(stages)

    return {
        "ok": True,
        "stages": stages,
        "state": assembled["state"],
        "meta": assembled["meta"],
        "report": {
            "name": ctx.get("name"),
            "code": ctx.get("code"),
            "profile": ctx.get("profile"),
            "financial": {
                "ratios": (ctx.get("financial_analysis") or {}).get("ratios"),
            },
            "market": ctx.get("market"),
            "valuation": ctx.get("valuation_raw"),
            "news": ctx.get("news"),
            "competitors": ctx.get("competitors"),
            "swot": ctx.get("swot"),
            "risks": ctx.get("risks_rule"),
        },
        "elapsed_sec": round((datetime.now() - t0).total_seconds(), 2),
    }
