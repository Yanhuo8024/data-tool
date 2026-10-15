# -*- coding: utf-8 -*-
"""情景估值：悲观/中性/乐观三档 · SOTP 叠加 · 亏损企业估值模板。

## 面向的两个真实场景（2026-10-09 用户要求）

**场景 A：单一项目的量化情景（本例：尊界项目）**
新项目往往还没形成稳定利润，给一个「每股价值 45.51 元」的单点数是**假精确**。
正确做法是给**三档情景**：悲观 / 中性 / 乐观，每档都拆成可验算的乘法链：

    年销量（万辆） × 单车净利（万元/辆） = 年贡献利润（亿元）
    （量纲正好：万辆 × 万元 = 亿元）

再把各档结果**叠加到基本盘估值上做 SOTP**，得到每股价值的区间而不是单点。

**场景 B：亏损 / 未盈利企业怎么估值**
这类公司 DCF 会直接失效（FCF 为负，「永续」根本无从谈起）。本项目把处理流程固化成
**方法链**：

    DCF 失效 → SOTP（拆出能赚钱的分部）→ 期权式定价（未盈利业务视为看涨期权）
             → 情景分析（三档区间兜底）

每一环都注明**适用条件**与**为什么**，避免下次再从零想一遍。

零项目依赖（仅标准库）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

LEVELS: Sequence[str] = ('悲观', '中性', '乐观')
DEFAULT_PROB = {'悲观': 0.25, '中性': 0.5, '乐观': 0.25}

# ==================== ① 三档情景 ====================


def build_case(level: str, *, volume: float, unit_profit: float,
               probability: Optional[float] = None,
               note: str = '') -> Dict[str, Any]:
    """构建单档情景：年销量 × 单车净利 = 年贡献利润。

    ``volume`` 单位**万辆**，``unit_profit`` 单位**万元/辆**，乘积即**亿元**。
    """
    contribution = None
    if volume is not None and unit_profit is not None:
        contribution = round(float(volume) * float(unit_profit), 4)
    return {
        'level': level,
        'annual_volume': volume,
        'unit_profit': unit_profit,
        'contribution': contribution,
        'probability': probability if probability is not None else DEFAULT_PROB.get(level),
        'note': note,
        'formula': (f'{volume:g} 万辆 × {unit_profit:g} 万元/辆 = {contribution:g} 亿元'
                    if contribution is not None else '—'),
    }


def scenario_table(project_name: str, cases: Sequence[Dict[str, Any]], *,
                   multiple: Optional[float] = None,
                   multiple_name: str = 'PE',
                   discount_pct: Optional[float] = None,
                   years: int = 1,
                   unit: str = '亿元') -> Dict[str, Any]:
    """渲染三档情景表，并在给定倍数时把年贡献利润折算为**估值贡献**。

    ``multiple``：给该项目适用的倍数（如 PS / PE）。给 ``discount_pct`` 与 ``years``
    时额外算**折现后的现值**（用于项目尚需数年才达产的场景）。
    """
    rows: List[Dict[str, Any]] = []
    vals: List[float] = []
    for c in cases or []:
        row = dict(c)
        contrib = c.get('contribution')
        if contrib is None:
            row['note'] = (row.get('note') or '') + '（缺销量或单车净利，未计算）'
            rows.append(row)
            continue
        if multiple:
            row['valuation'] = round(float(contrib) * float(multiple), 4)
            row['valuation_formula'] = (f'{contrib:g} × {multiple:g}（{multiple_name}）'
                                        f' = {row["valuation"]:g} {unit}')
        if discount_pct is not None and years:
            pv = float(contrib) / ((1 + float(discount_pct) / 100.0) ** int(years))
            row['pv'] = round(pv, 4)
            row['pv_formula'] = (f'{contrib:g} ÷ (1+{discount_pct:g}%)^{years} = {pv:g} {unit}')
        key = row.get('valuation') if multiple else contrib
        if key is not None:
            vals.append(float(key))
        rows.append(row)

    lo, hi = (min(vals), max(vals)) if vals else (None, None)
    probs = [r.get('probability') for r in rows if r.get('probability') is not None
             and r.get('contribution') is not None]
    weights = [r.get('contribution') for r in rows if r.get('probability') is not None
               and r.get('contribution') is not None]
    expected = (round(sum(p * v for p, v in zip(probs, weights)) / sum(probs), 4)
                if probs and sum(probs) else None)
    return {
        'project': project_name, 'rows': rows, 'low': lo, 'high': hi,
        'expected_contribution': expected, 'multiple': multiple,
        'multiple_name': multiple_name, 'unit': unit,
        'table_md': render_scenario_table(project_name, rows, lo, hi, expected, unit),
    }


def render_scenario_table(project_name: str, rows: List[Dict[str, Any]],
                          lo: Optional[float], hi: Optional[float],
                          expected: Optional[float], unit: str = '亿元') -> str:
    has_val = any(r.get('valuation') is not None for r in rows)
    cols = ['情景', '年销量(万辆)', '单车净利(万元/辆)', f'年贡献利润({unit})']
    if has_val:
        cols.append(f'估值贡献({unit})')
    lines = [f'**{project_name} · 三档情景**', '',
             '| ' + ' | '.join(cols) + ' |',
             '|' + '---|' * len(cols)]
    for r in rows:
        cells = [str(r.get('level') or '—'),
                 _g(r.get('annual_volume')), _g(r.get('unit_profit')), _g(r.get('contribution'))]
        if has_val:
            cells.append(_g(r.get('valuation')))
        lines.append('| ' + ' | '.join(cells) + ' |')
    if lo is not None:
        rng = f'区间 **{lo:g} ~ {hi:g} {unit}**'
        if expected is not None:
            rng += f'，概率加权中枢 **{expected:g} {unit}**'
        lines += ['', f'> 三档{rng}。**单点数不呈现**——区间的宽度本身就是风险的信息量。']
    return '\n'.join(lines)


def _g(v: Any) -> str:
    if v is None:
        return '—'
    if isinstance(v, float):
        return f'{v:g}'
    return str(v)


# ==================== ② 叠加基本盘做 SOTP ====================


def sotp_add(base_value: float, project: Dict[str, Any], *,
             net_debt: float = 0.0, shares: float = 1.0,
             base_name: str = '基本盘', unit: str = '亿元') -> Dict[str, Any]:
    """把项目三档情景**叠加**到基本盘估值上，得到每股价值区间。

    ``base_value`` 为基本盘（如商用车业务）的**股权价值**；``net_debt`` 为净负债
    （净现金给负数）。返回每档的股权价值与每股价值。
    """
    rows: List[Dict[str, Any]] = []
    per_shares: List[float] = []
    for r in project.get('rows') or []:
        contrib = r.get('valuation')
        if contrib is None:
            contrib = r.get('contribution')
        if contrib is None:
            rows.append({**r, 'equity_value': None, 'per_share': None})
            continue
        equity = float(base_value) + float(contrib) - float(net_debt)
        per = round(equity / float(shares), 2) if shares else None
        if per is not None:
            per_shares.append(per)
        rows.append({**r, 'contribution_used': contrib,
                     'equity_value': round(equity, 4), 'per_share': per})
    lo, hi = (min(per_shares), max(per_shares)) if per_shares else (None, None)
    return {
        'base_name': base_name, 'base_value': base_value, 'net_debt': net_debt,
        'shares': shares, 'rows': rows, 'per_share_low': lo, 'per_share_high': hi,
        'unit': unit,
        'text': _sotp_text(project.get('project') or '新项目', base_name, base_value,
                           net_debt, shares, rows, lo, hi, unit),
    }


def _sotp_text(project_name: str, base_name: str, base_value: float, net_debt: float,
               shares: float, rows: List[Dict[str, Any]],
               lo: Optional[float], hi: Optional[float], unit: str) -> str:
    lines = ['', f'#### 🧩 分部加总（SOTP）：{base_name} + {project_name}', '',
             f'基本盘（{base_name}）股权价值 **{base_value:g} {unit}**'
             f' － 净负债 **{net_debt:g} {unit}** ＋ 项目贡献。', '',
             '| 情景 | 项目贡献 | 股权价值 | 每股价值 |', '|---|---|---|---|']
    for r in rows:
        lines.append('| {} | {} | {} | {} |'.format(
            r.get('level') or '—', _g(r.get('contribution_used')),
            _g(r.get('equity_value')), _g(r.get('per_share'))))
    if lo is not None:
        lines += ['', f'> 每股价值区间 **{lo:g} ~ {hi:g} 元**（{shares:g} 亿股口径）。']
    return '\n'.join(lines)


# ==================== ③ 亏损企业估值模板 ====================


PLAYBOOK: Sequence[Dict[str, Any]] = (
    {'step': 1, 'method': 'DCF', 'name': '自由现金流折现',
     'when': 'FCF 为正且可预期（3 年内不会长期为负）',
     'why': '现金流稳定时 DCF 是最贴近价值本质的方法',
     'fail_when': 'FCF 长期为负 / 剧烈波动 —— 「永续增长」假设无从谈起，结果会失控',
     'next': 'SOTP'},
    {'step': 2, 'method': 'SOTP', 'name': '分部加总',
     'when': '至少有一个分部能独立盈利（如传统主业），亏损集中在新业务',
     'why': '把能赚钱的部分单独估，避免被亏损业务拖累成「整体不可估」',
     'fail_when': '全部业务都在亏 —— 没有可锚定的盈利分部',
     'next': '期权式定价'},
    {'step': 3, 'method': 'OPTION', 'name': '期权式定价（简化）',
     'when': '新业务未盈利但具备**不对称上行**（投入有限、成功则空间大）',
     'why': '未盈利业务的价值主要是「可能性」，用概率加权情景近似看涨期权',
     'fail_when': '上行空间有限或失败概率极高 —— 期权价值趋近于 0',
     'next': '情景分析'},
    {'step': 4, 'method': 'SCENARIO', 'name': '情景分析（兜底）',
     'when': '无论前置方法是否可用，都应给出三档区间',
     'why': '亏损企业的单点估值几乎必然误导，区间 + 假设才是可沟通的结论',
     'fail_when': '—（兜底方法，始终可用）', 'next': None},
)


def loss_making_playbook(*, fcf_positive: Optional[bool] = None,
                         profitable_segments: Optional[int] = None,
                         option_like: Optional[bool] = None,
                         dcf_per_share: Optional[float] = None,
                         sotp_per_share: Optional[float] = None,
                         price: Optional[float] = None) -> Dict[str, Any]:
    """按输入条件推荐**亏损企业估值方法链**。

    返回 ``{'chain','recommended','reasons','discarded','text'}``；
    ``recommended`` 是最终应采用的方法名（未给条件时为 ``SCENARIO``）。
    """
    chain: List[str] = ['DCF']
    reasons: List[str] = []
    discarded: List[str] = []
    recommended = 'SCENARIO'

    if fcf_positive:
        reasons.append('FCF 为正 → DCF 可用，以 DCF 为主并做敏感性分析')
        recommended = 'DCF'
    else:
        if fcf_positive is None:
            reasons.append('未提供 FCF 符号 → 不假设 DCF 可用，直接进入下一环')
        else:
            reasons.append('⚠️ FCF 长期为负 → **DCF 失效**（永续增长假设无从谈起）')
        discarded.append('DCF')
        # SOTP 与期权式定价**不是互斥关系**：前者估「能赚钱的基本盘」，
        # 后者估「未盈利的新业务」，有基本盘 + 有新业务时两者都要用。
        chain.append('SOTP')
        if profitable_segments:
            reasons.append(f'有 {profitable_segments} 个可独立盈利分部 → SOTP 用于**基本盘**')
            recommended = 'SOTP'
        else:
            discarded.append('SOTP')
            reasons.append('无可独立盈利分部（或未提供）→ SOTP 不足以给整体定值')
        chain.append('期权式定价')
        if option_like:
            reasons.append('未盈利新业务呈**不对称上行**特征 → 期权式定价用于**该新业务**')
            if not profitable_segments:
                recommended = 'OPTION'
        else:
            discarded.append('期权式定价')
            reasons.append('上行空间有限或未提供 → 期权价值趋近于 0，不采用')
        chain.append('情景分析')

    chain = list(dict.fromkeys(chain))
    if '情景分析' not in chain:
        chain.append('情景分析')
    return {
        'chain': chain, 'recommended': recommended, 'reasons': reasons,
        'discarded': list(dict.fromkeys(discarded)),
        'dcf_per_share': dcf_per_share, 'sotp_per_share': sotp_per_share, 'price': price,
        'text': render_playbook(chain, recommended, reasons, discarded,
                                price=price, sotp_per_share=sotp_per_share),
    }


def render_playbook(chain: List[str], recommended: str, reasons: List[str],
                    discarded: List[str], *, price: Optional[float] = None,
                    sotp_per_share: Optional[float] = None) -> str:
    """渲染方法链（含每步的适用条件与失效信号）。"""
    lines = ['', '#### 🧭 亏损 / 未盈利企业估值方法链', '',
             '推荐路径：**' + ' → '.join(chain) + '**', '']
    for it in PLAYBOOK:
        mark = '✅' if it['method'] in [c.upper() for c in chain] else '⬜'
        lines.append(f"- {mark} **{it['step']}. {it['name']}**：适用 —— {it['when']}；"
                     f"说明 —— {it['why']}")
        if it['method'] in [d.upper() for d in discarded]:
            lines.append(f"  - ⚠️ **本次不适用**：{it['fail_when']}")
    lines += ['', '**判定依据：**']
    for r in reasons:
        lines.append(f'- {r}')
    lines += ['', '> ⚠️ 期权式定价是**简化实现**（缺少波动率输入，用概率加权情景近似），'
                  '用于表达「不对称上行」的量级，不等于完整 Black-Scholes，不假装精确。']
    if price is not None and sotp_per_share is not None:
        diff = (sotp_per_share - price) / price * 100.0 if price else None
        if diff is not None:
            lines += ['', f'> 参照：SOTP 每股 {sotp_per_share:g} 元 vs 现价 {price:g} 元'
                          f'（{diff:+.1f}%）。']
    return '\n'.join(lines)
