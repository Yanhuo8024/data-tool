#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""company_deep_rules.py — 企业深析的**规则单一实现**（排雷 / 健康分 / 护城河 / SWOT）

## 为什么需要这个模块

同一套风控规则原先在 4 个地方各写了一遍，阈值完全相同却各自维护：

| 位置 | 规则数 | 问题 |
|---|---|---|
| `company_deep_report.derive_metrics()` | 6 | 报告层，生成 Word/PPT 的预警 |
| `agent_tool_registry.tool_company_deep_redflags()` | 6 | 与上一行**逐条同阈值**，纯复制 |
| `static/js/company-deep.js`（实时预警区） | 7 | **多一条**「利润增长与毛利率背离」 |
| `company_deep_pipeline._rule_draft()` | — | 第三套叙述口径 |

后果：网页实时区比导出的 Word/PPT **多 1 条预警**；改一个阈值要同步改 4 处；
而且**网页的 20 项排雷清单（`state.red.fin/biz`）压根没被填充**（管线只写 `verdict`/`worst`），
导致「教科书级问题公司」在网页上显示为「轻度」。

本模块把规则收敛为**唯一实现**，供三处消费：
`company_deep_pipeline`（回写 state.red）／`company_deep_report`（导出预警）
／`agent_tool_registry.tool_company_deep_redflags`（AI 工具）。

## 依赖方向

本模块是**叶子**：只依赖标准库，不 import 任何项目模块 → 不会造成循环 import（R4）。
`generate_swot` 的旧实现留在 `company_analysis.py` 并**改为委托本模块**。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# 前端 FIN_RED 清单项 id ↔ 规则映射（未命中清单的规则 id 为 None，只出现在预警区）
REDFLAG_IDS = {
    'ocf': '现金流长期低于净利润',
    'sd': '存贷双高',
    'ar': '应收增速远超营收',
    'inv': '存货异常膨胀',
    'gw': '商誉占净资产过高',
    'capex': '资本开支长期大于折旧',
}

# 阈值集中在此，改这里即三处同步（报告 / 网页实时区口径 / AI 工具）
TH_OCF_RATIO_WARN = 1.0        # OCF/净利润 < 1 视为含金量不足
TH_OCF_RATIO_DANGER = 0.5      # < 0.5 视为现金流背离（最强排雷信号）
TH_CASH_ASSET = 0.15           # 存贷双高：货币资金/总资产
TH_IDEBT_ASSET = 0.10          # 存贷双高：有息负债/总资产
TH_AR_GAP = 15.0               # 应收增速超出营收增速多少 pp 触发
TH_INV_GAP = 20.0              # 存货增速超出营收增速多少 pp 触发
TH_GW_EQUITY = 0.30            # 商誉/净资产上限
TH_DEBT_RATIO = 70.0           # 资产负债率上限（%）
TH_NPG_WITH_GM_DROP = -3.0     # 利润增长但毛利率下滑超过该 pp → 背离

# 牌照/资质型行业（护城河 license 维度直接给高分）
LICENSE_INDUSTRIES = (
    '银行', '保险', '证券', '非银金融', '多元金融', '信托',
    '公用事业', '电力', '燃气', '水务', '港口', '机场', '高速公路',
    '电信', '通信服务', '航空', '铁路', '医药商业', '教育',
)
# 网络效应型行业（护城河 network 维度加权）
NETWORK_INDUSTRIES = (
    '互联网', '软件服务', '平台', '电子商务', '传媒', '游戏', '社交',
    '物流', '快递', '电信', '通信服务', '航空', '酒店', '餐饮',
)


def _n(v) -> float | None:
    """宽容转 float：None/''/'-'/非数字 → None（不做 0 兜底，避免假数据）。"""
    if v is None or v == '' or v == '-':
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x:                      # NaN
        return None
    return x


def norm_fin(fin: Any) -> dict:
    """把 `state["finance"]`（字符串值）或任意数值字典归一化，并补出派生比率。

    返回键：绝对值 + gm/nm/ocf_np/debt_ratio/cash_debt/tat/em/du_roe/roe。
    所有无法计算的项为 None（**不猜测、不填 0**）。
    """
    f = fin if isinstance(fin, dict) else {}
    rev = _n(f.get('rev'))
    np_ = _n(f.get('np')) or _n(f.get('net_profit'))
    gp = _n(f.get('gp'))
    ocf = _n(f.get('ocf'))
    assets = _n(f.get('assets'))
    equity = _n(f.get('equity'))
    cash = _n(f.get('cash'))
    idebt = _n(f.get('idebt'))
    gw = _n(f.get('gw'))
    roe = _n(f.get('roe'))
    out = {
        'rev': rev, 'np': np_, 'gp': gp, 'ocf': ocf,
        'assets': assets, 'equity': equity, 'cash': cash, 'idebt': idebt, 'gw': gw,
        'arG': _n(f.get('arG')), 'invG': _n(f.get('invG')),
        'revG': _n(f.get('revG')), 'npG': _n(f.get('npG')), 'gmD': _n(f.get('gmD')),
        'payout': _n(f.get('payout')), 'roe': roe,
    }
    # 派生比率：可算则算，否则保留调用方直接给出的比率（两种入参形态都支持）
    out['gm'] = (gp / rev * 100) if (rev and gp is not None) else _n(f.get('gross_margin') or f.get('gm'))
    out['nm'] = (np_ / rev * 100) if (rev and np_ is not None) else _n(f.get('net_margin') or f.get('nm'))
    out['ocf_np'] = (ocf / np_) if (ocf is not None and np_) else None
    out['debt_ratio'] = ((assets - equity) / assets * 100) if (assets and equity is not None) \
        else _n(f.get('debt_ratio') or f.get('dr'))
    out['cash_debt'] = (cash / idebt) if (cash is not None and idebt) else None
    out['tat'] = (rev / assets) if (rev and assets) else _n(f.get('tat') or f.get('asset_turnover'))
    out['em'] = (assets / equity) if (assets and equity) else None
    out['du_roe'] = (out['nm'] / 100 * out['tat'] * out['em'] * 100) \
        if (out['nm'] is not None and out['tat'] and out['em']) else roe
    out['rdays'] = _n(f.get('rdays') or f.get('receivable_days'))
    return out


def redflag_alerts(m: dict) -> list[dict]:
    """排雷扫描（**唯一实现**）。入参推荐 `norm_fin()` 的输出。

    返回 `[{id, label, note, level}]`：
      · `id`   命中前端 FIN_RED 清单项时给出（可自动勾选）；否则 None
      · `level` 'danger' | 'warn'
    只返回**已触发**的信号；未触发不占位（避免"未扫描"与"扫描无异常"混淆）。
    """
    alerts: list[dict] = []

    def add(label, note, level, rid=None):
        alerts.append({'id': rid, 'label': label, 'note': note, 'level': level})

    r = m.get('ocf_np')
    if r is not None:
        if r < TH_OCF_RATIO_DANGER:
            add('现金流背离', f'OCF/净利润 = {r:.2f}，利润含金量严重不足，警惕收入未转化为现金', 'danger', 'ocf')
        elif r < TH_OCF_RATIO_WARN:
            add('现金含量偏低', f'OCF/净利润 = {r:.2f}，利润含金量不足', 'warn', 'ocf')

    cash, idebt, assets = m.get('cash'), m.get('idebt'), m.get('assets')
    if cash is not None and idebt is not None and assets \
            and cash > assets * TH_CASH_ASSET and idebt > assets * TH_IDEBT_ASSET:
        add('疑似存贷双高', '账上大量货币资金与有息负债并存，需解释资金安排', 'danger', 'sd')

    ar_g, inv_g, rev_g = m.get('arG'), m.get('invG'), m.get('revG')
    if ar_g is not None and rev_g is not None and ar_g > rev_g + TH_AR_GAP:
        add('应收增速超营收', f'应收 +{ar_g:.1f}% vs 营收 {rev_g:+.1f}%，或存在放宽信用刺激收入', 'warn', 'ar')
    if inv_g is not None and rev_g is not None and inv_g > rev_g + TH_INV_GAP:
        add('存货异常膨胀', f'存货 +{inv_g:.1f}% vs 营收 {rev_g:+.1f}%，警惕滞销与减值', 'warn', 'inv')

    gw, equity = m.get('gw'), m.get('equity')
    if gw is not None and equity and gw / equity > TH_GW_EQUITY:
        add('商誉占净资产过高', f'商誉/净资产 = {gw / equity * 100:.1f}%，并购减值地雷', 'danger', 'gw')

    dr = m.get('debt_ratio')
    if dr is not None and dr > TH_DEBT_RATIO:
        add('资产负债率偏高', f'{dr:.1f}%，关注有息负债结构与偿债压力', 'warn')

    npg, gmd = m.get('npG'), m.get('gmD')
    if npg is not None and gmd is not None and npg > 0 and gmd < TH_NPG_WITH_GM_DROP:
        add('利润增长与毛利率背离', '增长可能靠放量或压费用，可持续性存疑', 'warn')

    return alerts


def finance_view(ctx: dict) -> dict:
    """从流水线 ctx 构造**规范化财务视图**（≈ `state["finance"]` 的等价物）。

    存在意义：排雷规则需要"利润 + 现金流 + 资产负债表"同时在手，
    而这三者分散在 `ctx["financial_analysis"]["ratios"]` 与 `ctx["statements"]` 两处。
    若各处自行拼装，就会出现"管线里少一个 np → 算不出 OCF/净利润 → 漏掉最强排雷信号"
    这类口径分叉（实测：网页缺「现金流背离」，同一份数据的导出文档却有）。
    """
    ratios = (ctx.get("financial_analysis") or {}).get("ratios") or {}
    p = ratios.get("profitability") or {}
    s = ratios.get("solvency") or {}
    g = ratios.get("growth") or {}
    o = ratios.get("operational") or {}
    rev, gm = p.get("revenue"), p.get("gross_margin")
    out = dict(ctx.get("statements") or {})
    out.update({
        "rev": rev, "np": p.get("net_profit"), "roe": p.get("roe"),
        "gross_margin": gm, "net_margin": p.get("net_margin"),
        "debt_ratio": s.get("debt_ratio"),
        "revG": g.get("revenue_growth"), "npG": g.get("profit_growth"),
        "receivable_days": o.get("receivable_days"),
    })
    if out.get("gp") is None and rev is not None and gm is not None:
        try:
            out["gp"] = round(float(rev) * float(gm) / 100.0, 2)
        except (TypeError, ValueError) as e:
            logger.debug("finance_view: 毛利额反推跳过（rev=%r gm=%r）: %s", rev, gm, e)
    return out


def health_score(m: dict) -> int:
    """财务健康分 0-100（规则加权）。与报告层口径一致，原实现在 `derive_metrics` 内。"""
    score = 50.0
    gm, nm, roe = m.get('gm'), m.get('nm'), m.get('roe')
    if gm is not None:
        score += min(15.0, (gm - 20) * 0.3)
    if nm is not None:
        score += min(15.0, (nm - 5) * 0.4)
    if roe is not None:
        score += min(15.0, (roe - 8) * 0.5)
    r = m.get('ocf_np')
    if r is not None:
        score += 12 if r >= 1 else (4 if r >= 0.7 else -15)
    dr = m.get('debt_ratio')
    if dr is not None:
        score += 10 if dr < 40 else (3 if dr < 60 else -10)
    return max(0, min(100, round(score)))


# ── 护城河六维：连续打分（0-10）────────────────────────────────
def _ramp(v, lo, hi) -> float:
    """把 v 从 [lo, hi] 线性映射到 [0, 10] 并夹取；v 为 None → 0。"""
    if v is None:
        return 0.0
    if hi == lo:
        return 0.0
    t = (float(v) - lo) / (hi - lo)
    return max(0.0, min(10.0, t * 10.0))


def _blend(*pairs) -> float:
    """加权平均：`_blend((0.6, 3.0), (0.4, 8.0))` → 5.0（权重自动归一化）。"""
    tw = sum(w for w, _ in pairs)
    if tw <= 0:
        return 0.0
    return sum(w * x for w, x in pairs) / tw


def moat_scores(m: dict, profile: dict = None, industry_stats: dict = None) -> dict:
    """护城河六维评分，**0-10 连续**（与前端滑杆 `min=0 max=10`、总分 `/10` 同量纲）。

    旧实现是硬编码分支，取值集恒为 {3,4,5,6}（只覆盖 0-10 的 30%-60%），
    导致"弱公司 4.0 / 强公司 5.0"（差 1 分），且工具判语「≥7 护城河较深」**永不可达**。
    本实现用真实财务信号线性映射，强/弱公司可拉开 7 分以上。

    输入不足的维度返回保守低分（而非中位 5）——避免"数据缺失"被误读为"护城河中等"。
    """
    profile = profile or {}
    industry = str(profile.get('industry') or '')
    st = industry_stats or {}
    gm, nm, roe = m.get('gm'), m.get('nm'), m.get('roe')
    dr, ato, rdays = m.get('debt_ratio'), m.get('tat'), m.get('rdays')
    mcap = _n(profile.get('market_cap_yi'))
    share = _n(st.get('share_rev'))

    tech = _blend((0.65, _ramp(gm, 8, 75)), (0.35, _ramp(roe, 3, 30)))
    brand = _blend((0.60, _ramp(gm, 12, 85)), (0.40, _ramp(nm, 2, 35)))
    # 渠道：周转越慢越弱（应收周转天数取负向）
    channel = _blend((0.60, _ramp(ato, 0.2, 1.6)), (0.40, _ramp((120 - rdays) if rdays is not None else None, 20, 110)))
    cost = _blend((0.60, _ramp(mcap, 30, 8000)), (0.40, _ramp((60 - dr) if dr is not None else None, 0, 45)))
    network = _blend((0.50, _ramp(share, 2, 35)), (0.50, _ramp(10 if industry in NETWORK_INDUSTRIES else 2, 0, 10)))
    if industry in LICENSE_INDUSTRIES:
        lic = _blend((0.7, 8.5), (0.3, _ramp(share, 2, 35)))
    else:
        lic = _blend((0.6, _ramp(gm, 20, 80)), (0.4, _ramp(share, 2, 35)))

    dims = {'tech': tech, 'brand': brand, 'channel': channel,
            'cost': cost, 'network': network, 'license': lic}
    out = {k: int(round(max(0.0, min(10.0, v)))) for k, v in dims.items()}
    avg = sum(out.values()) / 6.0
    out['avg'] = round(avg, 1)
    out['grade'] = ('护城河较深' if avg >= 7 else ('有一定壁垒' if avg >= 5
                   else ('壁垒偏薄' if avg >= 3 else '几乎无壁垒')))
    # 壁垒可持续性 1-5（与 moat.dur 的「X 级」口径一致）
    out['dur'] = str(max(1, min(5, int(round(avg / 2)))))
    return out


def strip_metrics(out: dict) -> dict:
    """从 `moat_scores()` 结果中取出可直接写入 `state["moat"]` 的六维 + dur。"""
    return {k: out.get(k) for k in ('tech', 'brand', 'channel', 'cost', 'network', 'license', 'dur')}


MOAT_KEYS = ('tech', 'brand', 'channel', 'cost', 'network', 'license')

# P0-4（2026-10-10）：六维中文名 + 打分公式（与 moat_scores 一一对应，单一实现）
MOAT_LABELS: dict = {
    'tech': ('技术/专利', '0.65×毛利率映射 + 0.35×ROE映射'),
    'brand': ('品牌心智', '0.60×毛利率映射 + 0.40×净利率映射'),
    'channel': ('渠道网络', '0.60×资产周转映射 + 0.40×应收天数（负向）'),
    'cost': ('成本/规模', '0.60×市值映射 + 0.40×负债率（负向）'),
    'network': ('网络效应', '0.50×市占率映射 + 0.50×行业网络效应'),
    'license': ('牌照/资质', '牌照行业 8.5 基础分，否则 0.60×毛利率+0.40×市占率'),
}


def moat_breakdown(moat: dict, default: float = 5.0) -> str:
    """护城河六维**完整展示**：每维分数 + 打分公式 + 均分计算过程。

    用户 P0（2026-10-10）：不许只给一个均分——6 个维度必须列全，
    均分 = Σ(六维)/6 要写出算式。键缺失按 `default` 计入并标注。
    """
    m = moat or {}
    vals: list = []
    lines: list = []
    for k in MOAT_KEYS:
        label, formula = MOAT_LABELS[k]
        try:
            v = float(m[k])
            note = ''
        except (TypeError, ValueError, KeyError):
            v = float(default)
            note = '（数据缺失，按默认 5 计入）'
        vals.append(v)
        lines.append(f"- {label}：{v:.0f}/10 —— {formula}{note}")
    avg = sum(vals) / len(vals) if vals else float(default)
    expr = '+'.join(f'{v:.0f}' for v in vals)
    lines.append(f"均分 = ({expr}) ÷ 6 = **{avg:.1f}/10**（{moat_grade(avg)}）")
    return '\n'.join(lines)


def moat_avg(moat: dict, default: float = 5.0) -> float:
    """护城河六维均分（**共用实现**）。

    🚨 不得写成 `float(m.get(k) or 5)`：**0 是合法取值**（无护城河），
    `0 or 5` 会把"几乎无壁垒"误判成"中等壁垒"。仅键缺失/非数字时回落 `default`。
    """
    vals = []
    for k in MOAT_KEYS:
        try:
            vals.append(float((moat or {}).get(k)))
        except (TypeError, ValueError):
            vals.append(float(default))
    return sum(vals) / len(vals) if vals else float(default)


def moat_grade(avg: float) -> str:
    """护城河判语。阈值与 `company_deep_moat_score` 工具的解读保持一致。"""
    return ('护城河较深' if avg >= 7 else ('有一定壁垒' if avg >= 5
            else ('壁垒偏薄' if avg >= 3 else '几乎无壁垒')))
