# -*- coding: utf-8 -*-
"""number_sanity.py — 数字体检：单位冲突 · 倍数反推 · 量级冲突 · 多源交叉 · 衍生数字。

## 面向的真实事故（2026-10-09 用户要求）

1. **「300 亿元 + 美元口径」自相矛盾**：同一数值在文中一处写「300 亿美元」、
   另一处写「300 亿元」——单位打架，读者无从判断到底哪个是错的。
   ⇒ `unit_conflict()`：同值出现两种货币单位 → 「单位/口径冲突」警告。
2. **PS 倍数不与市值/营收反推一致**：倍数是算出来的，就该能被算回去验证。
   ⇒ `check_ps_consistency()`：PS × 营收 与市值对照，偏差超容差即警告。
3. **34-43 倍 PS 被扔进「未核验」**：它明明是由**已核验**的 240~300 亿元估值与
   7 亿元营收算出来的，标记「未核验」是误导——它不是外部数据，是**本系统计算**。
   ⇒ `derive_range()`：由已核验输入推导区间，输出带「本系统计算（输入已核验）」标注；
   `fact_triplet.build_report` 会把这些值从「未核验」清单中剔除并单列一节。
4. **量级冲突**：同一指标上下文里两个数差 10 倍以上，几乎必是单位/口径错误
   （如「营收 7 亿」与「营收 70 亿」并存）⇒ `magnitude_conflict()`。
5. **多源交叉**：同一数字 3 个以上**独立来源**支持 → 置信度提升；只有 1 个 →
   标「单一来源」⇒ `multi_source_score()`。

零项目依赖（仅标准库）。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# ==================== ① 单位/口径冲突 ====================

_MONEY_UNIT = re.compile(
    r'(?<![\d.])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*'
    r'(万亿元|亿美元|亿元|万美元|万元|亿美元|亿美元|美元|元|亿|万)')

_CURRENCY_OF = {'美元': 'USD', '亿美元': 'USD', '万美元': 'USD',
                '亿元': 'CNY', '万亿元': 'CNY', '万元': 'CNY', '元': 'CNY',
                '亿': None, '万': None}


def unit_conflict(text: str) -> List[Dict[str, Any]]:
    """同一数值以两种货币单位出现 → 「单位/口径冲突」。

    例：「融资 300 亿美元」与「融资 300 亿元」并存 → 命中 ``300``。
    无量纲后缀（亿/万）不参与判定（无法确定币种，不猜）。
    """
    body = str(text or '')
    seen: Dict[str, Dict[str, List[int]]] = {}
    for m in _MONEY_UNIT.finditer(body):
        val, unit = m.group(1).replace(',', ''), m.group(2)
        ccy = _CURRENCY_OF.get(unit)
        if ccy is None:
            continue
        slot = seen.setdefault(val, {'USD': [], 'CNY': []})
        if len(slot[ccy]) < 3:
            slot[ccy].append(m.start())
    out: List[Dict[str, Any]] = []
    for val, slot in seen.items():
        if slot['USD'] and slot['CNY']:
            frag_us = body[slot['USD'][0]:slot['USD'][0] + len(val) + 5]
            frag_cny = body[slot['CNY'][0]:slot['CNY'][0] + len(val) + 5]
            out.append({
                'value': val, 'kind': 'unit_currency',
                'detail': f'数值 {val} 同时以「{frag_us.strip()}（美元口径）」'
                          f'与「{frag_cny.strip()}（人民币口径）」出现',
                'positions': {'USD': slot['USD'], 'CNY': slot['CNY']},
            })
    return out


# ==================== ② 倍数反推一致性 ====================


def check_multiple_consistency(multiple: float, denominator: Optional[float],
                               implied_value: Optional[float], *,
                               label: str = 'PS', tol: float = 0.15,
                               unit: str = '亿元') -> Dict[str, Any]:
    """倍数 × 分母 ≈ 隐含市值/估值（容差 15%）。

    例：PS 34.3~42.9 × 营收 7 亿 → 隐含估值应落回 240~300 亿；对不上说明
    「倍数、营收、市值」三者至少有一个口径是错的。
    """
    out = {'label': label, 'multiple': multiple, 'denominator': denominator,
           'implied_value': implied_value, 'ok': False, 'deviation_pct': None, 'reason': ''}
    for name, v in (('multiple', multiple), ('denominator', denominator),
                    ('implied_value', implied_value)):
        if v in (None, ''):
            out['reason'] = f'缺少 {name}，无法反推验证'
            return out
    try:
        implied = float(multiple) * float(denominator)
        target = float(implied_value)
    except (TypeError, ValueError) as e:
        out['reason'] = f'入参非数值: {e}'
        return out
    if target == 0:
        out['reason'] = '参照值为 0，无法验证'
        return out
    dev = (implied - target) / abs(target) * 100.0
    out['implied_computed'] = round(implied, 4)
    out['deviation_pct'] = round(dev, 2)
    if abs(dev) <= tol * 100:
        out['ok'] = True
        out['reason'] = (f'反推一致：{label} {multiple:g} × {denominator:g} {unit} '
                         f'= {implied:,.1f} {unit}，与参照 {target:,.1f} {unit} 偏差 {dev:+.1f}%')
    else:
        out['reason'] = (f'⚠️ 反推不一致：{label} {multiple:g} × {denominator:g} {unit} '
                         f'= {implied:,.1f} {unit}，但参照值为 {target:,.1f} {unit}'
                         f'（偏差 {dev:+.1f}% > ±{tol * 100:.0f}%）——'
                         f'倍数、分母、参照值三者至少有一个口径错误，请核实单位/口径。')
    return out


# ==================== ③ 量级冲突（同类数字差 >10 倍） ====================


def magnitude_conflict(rows: Iterable[Dict[str, Any]], *, ratio: float = 10.0) -> List[Dict[str, Any]]:
    """同一指标出现多个数值时，最大/最小之比超过 ``ratio`` → 提示核实单位/口径。

    ``rows`` 每项含 ``metric`` / ``value``（可数值化）/ ``source``。
    """
    grouped: Dict[str, List[Tuple[float, Dict[str, Any]]]] = {}
    for r in (rows or []):
        v = _to_f((r or {}).get('value'))
        m = str((r or {}).get('metric') or '').strip()
        if v is None or not m:
            continue
        grouped.setdefault(m, []).append((abs(v), r))
    out: List[Dict[str, Any]] = []
    for metric, items in grouped.items():
        if len(items) < 2:
            continue
        vals = [v for v, _ in items]
        lo_v, hi_v = min(vals), max(vals)
        if lo_v > 0 and hi_v / lo_v >= ratio:
            lo_row = next(r for v, r in items if v == lo_v)
            hi_row = next(r for v, r in items if v == hi_v)
            out.append({
                'metric': metric, 'kind': 'magnitude', 'ratio': round(hi_v / lo_v, 1),
                'low': {'value': lo_v, 'source': lo_row.get('source')},
                'high': {'value': hi_v, 'source': hi_row.get('source')},
                'warning': (f'「{metric}」出现 {lo_v:g} 与 {hi_v:g}（相差 {hi_v / lo_v:.0f} 倍），'
                            f'请核实单位/口径（如亿元 vs 万元、人民币 vs 美元）。'),
            })
    return out


# ==================== ④ 多源交叉 ====================


def multi_source_score(rows: Iterable[Dict[str, Any]], *,
                       strong: int = 3) -> Dict[str, Any]:
    """按数值统计**独立来源数**：≥strong → ``corroborated``；2 → ``dual``；1 → ``single``。

    「独立」按来源名去重计（同一工具重复返回只算 1 个）。
    """
    by_val: Dict[str, set] = {}
    meta: Dict[str, Dict[str, Any]] = {}
    for r in (rows or []):
        val = str((r or {}).get('value') or '').strip()
        if not val:
            continue
        src = str((r or {}).get('source') or (r or {}).get('tool') or '未标注')
        by_val.setdefault(val, set()).add(src)
        meta.setdefault(val, {'metric': (r or {}).get('metric') or '?'})
    graded = []
    counts = {'corroborated': 0, 'dual': 0, 'single': 0}
    for val, srcs in by_val.items():
        n = len(srcs)
        level = 'corroborated' if n >= strong else ('dual' if n == 2 else 'single')
        counts[level] += 1
        graded.append({'value': val, 'metric': meta[val]['metric'], 'sources': sorted(srcs),
                       'n_sources': n, 'level': level,
                       'level_cn': {'corroborated': '多源印证', 'dual': '双源',
                                    'single': '⚠️ 单一来源'}[level]})
    graded.sort(key=lambda x: x['n_sources'])
    return {'rows': graded, 'counts': counts,
            'weak': [g for g in graded if g['level'] == 'single'],
            'text': render_multi_source(graded, counts)}


def render_multi_source(graded: Sequence[Dict[str, Any]], counts: Dict[str, int]) -> str:
    lines = ['', '#### 🔍 多源交叉', '',
             f"多源印证 **{counts.get('corroborated', 0)}** · 双源 **{counts.get('dual', 0)}** · "
             f"单一来源 **{counts.get('single', 0)}**。", '']
    if counts.get('single'):
        lines.append('**单一来源数字（建议补充独立来源后再引用）：**')
        for g in graded:
            if g['level'] == 'single':
                lines.append(f"- `{g['metric']}` = `{g['value']}`（仅 `{g['sources'][0]}`）")
    return '\n'.join(lines)


# ==================== ⑤ 衍生数字（本系统计算） ====================


def derive_range(numerator: Tuple[float, float], denominator: Tuple[float, float], *,
                 label: str, unit: str = '亿元', out_unit: str = '倍',
                 inputs_verified: bool = True) -> Optional[Dict[str, Any]]:
    """由**已核验输入**推导比率区间，标注「本系统计算」而非「未核验」。

    分子分母均为 ``(low, high)`` 区间（分母须同号）。例：
    估值 240~300 亿元 ÷ 营收 7 亿元 → PS 34.3~42.9 倍。
    """
    n_lo, n_hi = sorted((float(numerator[0]), float(numerator[1])))
    d_lo, d_hi = sorted((float(denominator[0]), float(denominator[1])))
    if d_lo == 0 or (d_lo < 0 < d_hi) or n_lo < 0:
        logger.debug('衍生区间跳过：分母过零或分子为负 (%s)', label)
        return None
    cands = [n_lo / d_hi, n_hi / d_lo]          # 正分母下商的极值
    lo, hi = min(cands), max(cands)
    formula = (f'{n_lo:g}~{n_hi:g} {unit} ÷ {d_lo:g}~{d_hi:g} {unit}'
               if (n_lo != n_hi or d_lo != d_hi)
               else f'{n_lo:g} {unit} ÷ {d_lo:g} {unit}')
    return {
        'label': label, 'value_low': round(lo, 2), 'value_high': round(hi, 2),
        'value_text': f'{round(lo, 2):g}~{round(hi, 2):g} {out_unit}',
        'formula': formula, 'unit': unit, 'out_unit': out_unit,
        'computed_by': 'system',
        'note': ('本系统计算（输入已核验）' if inputs_verified
                 else '本系统计算（⚠️ 输入未全部核验）'),
    }


def _to_f(v: Any) -> Optional[float]:
    """宽松转数值：容忍中文单位后缀（亿元/万元/美元/倍…），失败返回 None。"""
    try:
        return float(re.sub(r'[,%倍美元元万亿点\s]', '', str(v or '')).strip())
    except (TypeError, ValueError):
        return None
