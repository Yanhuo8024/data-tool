#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
company_deep_metrics.py — 企业深析报告公共层

颜色常量 / 字体 / 格式化工具（_s/_n/_fmt）/ 指标推导（derive_metrics）
与五问结论（build_verdict_lines）。docx 与 pptx 两个导出层共用。
"""
from typing import Any

FONT = "Microsoft YaHei"
ACCENT = (0x1F, 0x3A, 0x5F)      # deep navy
ACCENT2 = (0x22, 0xD3, 0xEE)     # cyan
OK = (0x2D, 0xD4, 0xA8)
WARN = (0xF5, 0xC4, 0x51)
DANGER = (0xF8, 0x71, 0x71)
INK = (0x1F, 0x1F, 0x1F)
MUTED = (0x59, 0x59, 0x59)
CREAM = (0xF7, 0xF5, 0xF0)



# ── 公共 ──────────────────────────────────────────────
# ── 公共 ──────────────────────────────────────────────
def _s(v: Any, default: str = "—") -> str:
    if v is None or v == "" or (isinstance(v, float) and v != v):
        return default
    return str(v)


def _n(v: Any):
    if v is None or v == "":
        return None
    try:
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None


def _fmt(n, digits: int = 1, suffix: str = ""):
    if n is None:
        return "—"
    return f"{n:.{digits}f}{suffix}"


def derive_metrics(state: dict) -> dict:
    """从分析状态推导报告用指标（与前端口径一致）。

    归一化 / 健康分 / 排雷预警全部委托 `company_deep_rules`（**单一实现**）：
    原先此处的 6 条排雷规则与 `static/js/company-deep.js` 的 7 条、
    `agent_tool_registry.tool_company_deep_redflags` 的 6 条各写一遍，
    阈值相同却各自维护 → 导出的 Word/PPT 比网页少 1 条预警
    （网页有「利润增长与毛利率背离」，此层没有）。现已收敛。
    """
    from company_deep_rules import norm_fin, health_score, redflag_alerts
    m = norm_fin(state.get("finance") or {})
    moat_avg = _moat_avg(state.get("moat") or {})

    # 保持 [(标题, 说明)] 二元组格式：build_verdict_lines / docx / pptx 三处按此解包
    alerts = [(a["label"], a["note"]) for a in redflag_alerts(m)]

    return {
        "gm": m["gm"], "nm": m["nm"], "ocf_np": m["ocf_np"], "debt_ratio": m["debt_ratio"],
        "cash_debt": m["cash_debt"], "tat": m["tat"], "em": m["em"], "du_roe": m["du_roe"],
        "health": health_score(m), "moat_avg": moat_avg, "alerts": alerts,
    }


def _moat_avg(moat: dict) -> float:
    """护城河均分。**不得用 `or 5` 兜底**：0 是合法取值（无护城河），
    `0 or 5` 会把它误判成"中等"。仅在键缺失/非数字时才回落 5。"""
    vals = []
    for k in ("tech", "brand", "channel", "cost", "network", "license"):
        try:
            vals.append(float(moat.get(k)))
        except (TypeError, ValueError):
            vals.append(5.0)
    return sum(vals) / 6.0 if vals else 5.0


def moat_detail(state: dict) -> str:
    """P0-4（2026-10-10）：六维紧凑明细「技术7·品牌6·渠道5·成本5·网络4·牌照6」，均分算式见 rules.moat_breakdown。"""
    from company_deep_rules import MOAT_LABELS
    m = state.get("moat") or {}
    parts = []
    for k in ("tech", "brand", "channel", "cost", "network", "license"):
        try:
            v = float(m[k])
        except (TypeError, ValueError, KeyError):
            v = 5.0
        parts.append(f"{MOAT_LABELS[k][0].split('/')[0]}{v:.0f}")
    return "·".join(parts)


def build_verdict_lines(state: dict, met: dict) -> list[tuple[str, str, str]]:
    """返回 [(问题, 回答, 证据)]，与一页纸判断卡一致。"""
    c = state.get("company") or {}
    b = state.get("business") or {}
    model = state.get("model") or {}
    m = state.get("moat") or {}
    g = state.get("gov") or {}
    v = state.get("val") or {}
    risks = state.get("risks") or []
    concl = state.get("conclusions") or {}

    a1 = concl.get("a1") or (
        f"{'好赛道' if _n(b.get('score')) and _n(b.get('score')) >= 4 else '赛道一般/未评估'}。"
        f"{_s(c.get('name'), '该公司')}处于{_s(b.get('lifecycle'), '未标注')}阶段的{_s(c.get('industry'), '所属行业')}行业；"
        f"行业规模约 {_s(b.get('size'))} 亿元、增速约 {_s(b.get('growth'))}%；{_s(b.get('what'), '业务本质待补充')}"
    )
    a2 = concl.get("a2") or (
        f"六维护城河均分 {_fmt(met['moat_avg'])}/10（{moat_detail(state)}）；市占率 {_s(b.get('share'))}%；"
        f"{_s(m.get('whyChoose'), '壁垒证据待补充')}"
    )
    a3 = concl.get("a3") or (
        f"财务健康分 {met['health']}/100；"
        + (f"净利率 {_fmt(met['nm'])}%；" if met["nm"] is not None else "")
        + (f"ROE {_fmt(_n(f := (state.get('finance') or {}).get('roe')))}%；" if _n((state.get("finance") or {}).get("roe")) is not None else "")
        + (f"OCF/净利润 {_fmt(met['ocf_np'], 2)}；" if met["ocf_np"] is not None else "")
        + ("现金流已覆盖净利润" if met["ocf_np"] and met["ocf_np"] >= 1 else "需警惕应收/存货占用")
    )
    a4 = concl.get("a4") or (
        f"实控人：{_s(g.get('controller'))}；质押率 {_s(g.get('pledge'))}%；"
        + (f"已登记风险 {len(risks)} 条" if risks else "风险因素尚未系统登记")
    )
    a5 = concl.get("a5") or (
        f"{('明确判断：' + v.get('verdict')) if v.get('verdict') else '估值结论待补充'}；"
        f"PE {_s(v.get('pe'))}；历史分位 {_s(v.get('pePct'))}%"
    )

    return [
        ("这门生意好不好？", a1,
         f"行业评分 {_s(b.get('score'))}/5 · 生命周期 {_s(b.get('lifecycle'))} · 政策 {_s(b.get('policy'))}"),
        ("这家公司强不强？", a2,
         f"护城河 {_fmt(met['moat_avg'])}/10 · 毛利率 vs 同行：{_s(m.get('marginPeers'))}"),
        ("账靠不靠谱？", a3,
         f"健康分 {met['health']} · 毛利率 {_fmt(met['gm'])}% · 现金含量 {_fmt(met['ocf_np'], 2)}"),
        ("谁在管、有什么风险？", a4,
         f"治理评分 {_s(g.get('score'))}/5 · 风险 {len(risks)} 条"),
        ("当前价格值不值？", a5,
         f"PE 分位 {_s(v.get('pePct'))}% · 结论 {_s(v.get('verdict'))}"),
    ]


# ── Word ──────────────────────────────────────────────
