# -*- coding: utf-8 -*-
"""现金流补充：经营现金流 / 自由现金流 / 现金消耗速度。

## 为什么需要（2026-10-09 用户要求）

用户明确要求：**「经营现金流必须补上——接口已获取就展示，算自由现金流和现金消耗速度」**。

旧实现的毛病是「要么全无、要么全有」：只要缺一个字段，整段现金流分析就消失，
于是报告里只剩利润表数字，看不出**利润是不是真金白银收回来了**（OCF/净利润），
也看不出**公司在烧钱还是在造血**（FCF 与 burn rate）。

本模块的规则：

1. **有 OCF 就展示** —— 不因缺 Capex 而隐藏（缺的那项单独降级为「未获取」）；
2. `FCF = OCF − Capex`（资本开支取不到时**如实标注为未扣 Capex 的近似值**，不假装精确）；
3. **现金消耗速度**：`FCF < 0` 时 report 每期净消耗与**现金跑道**（Runway = 现金 / 净消耗）；
4. **现金含量**：`OCF / 净利润`——长期显著低于 1 说明利润没变成现金（典型排雷信号）。

零项目依赖（仅标准库）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 字段别名（归一化后做**精确**匹配，避免 `ocf` 误抓 `ocf_ps`）
OCF_ALIASES: Tuple[str, ...] = (
    'ocf', '经营现金流', '经营现金流净额', '经营活动现金流净额',
    '经营活动产生的现金流量净额', 'operatingcashflow', 'cfof',
)
CAPEX_ALIASES: Tuple[str, ...] = (
    'capex', '资本开支', '资本性支出', '资本支出',
    '购建固定资产无形资产和其他长期资产支付的现金', '购建固定资产支付的现金',
    'capitalexpenditure',
)
CASH_ALIASES: Tuple[str, ...] = (
    'cash', '货币资金', '现金及现金等价物', '期末现金及现金等价物余额',
    'cashandequivalents', 'cashbalance',
)
NP_ALIASES: Tuple[str, ...] = (
    'netprofit', '净利润', '归母净利润', '归属于母公司股东的净利润', 'np',
)
CAPEX_MIN_NOTE = 'Capex 未获取，以下 FCF 为**未扣资本开支**的近似值（偏乐观）'


def _norm(key: Any) -> str:
    return str(key).strip().lower().replace('_', '').replace(' ', '').replace('-', '')


def _dig(obj: Any, aliases: Iterable[str], *, max_depth: int = 6) -> Optional[Any]:
    """在嵌套结构里按别名**精确**查找第一个非空值（只认归一化后完全相等）。"""
    target = {_norm(a) for a in aliases}
    queue: List[Tuple[Any, int]] = [(obj, 0)]
    while queue:
        cur, depth = queue.pop(0)
        if depth > max_depth:
            continue
        if isinstance(cur, dict):
            for k, v in cur.items():
                if _norm(k) in target and v not in (None, '', '—', '-'):
                    return v
                if isinstance(v, (dict, list)):
                    queue.append((v, depth + 1))
        elif isinstance(cur, list):
            for v in cur[:20]:
                if isinstance(v, (dict, list)):
                    queue.append((v, depth + 1))
    return None


def _f(v: Any) -> Optional[float]:
    try:
        return float(str(v).replace(',', '').replace('%', '').strip())
    except (TypeError, ValueError):
        return None


def analyze(snapshot: Any, *, capex: Any = None, cash: Any = None,
            net_profit: Any = None, period_unit: str = '年',
            unit: str = '亿元') -> Dict[str, Any]:
    """现金流体检。

    ``snapshot`` 可为任意嵌套结构（自动递归找 OCF / Capex / 货币资金 / 净利润）；
    也可用关键字显式传入。**只要有 OCF 就产出结果**，缺项单独进 ``gaps``。
    """
    snap = snapshot or {}
    ocf = _f(_dig(snap, OCF_ALIASES))
    cx = _f(capex) if capex is not None else _f(_dig(snap, CAPEX_ALIASES))
    csh = _f(cash) if cash is not None else _f(_dig(snap, CASH_ALIASES))
    np_ = _f(net_profit) if net_profit is not None else _f(_dig(snap, NP_ALIASES))

    gaps: List[str] = []
    if ocf is None:
        gaps.append('经营现金流（OCF）未获取')
        return {'ok': False, 'ocf': None, 'capex': cx, 'fcf': None, 'cash': csh,
                'net_profit': np_, 'ocf_ni': None, 'burn': None, 'runway': None,
                'verdict_cn': '经营现金流未获取，无法判断造血能力',
                'gaps': gaps, 'table_md': '', 'text': _gap_text(gaps), 'unit': unit}

    if cx is None:
        gaps.append(CAPEX_MIN_NOTE)
        fcf = ocf                       # 近似：未扣 Capex
    else:
        fcf = ocf - cx

    ratio = None
    if np_ not in (None, 0):
        ratio = round(ocf / np_, 3)

    burn = round(-fcf, 4) if fcf < 0 else None      # 净消耗（正数表示每期烧钱）
    runway = None
    if burn and burn > 0 and csh is not None:
        runway = round(csh / burn, 2)

    verdict = _verdict(ocf=ocf, fcf=fcf, ratio=ratio, runway=runway, unit=unit,
                       period_unit=period_unit)
    res = {
        'ok': True, 'ocf': round(ocf, 4), 'capex': None if cx is None else round(cx, 4),
        'fcf': round(fcf, 4), 'fcf_is_approx': cx is None, 'cash': csh,
        'net_profit': np_, 'ocf_ni': ratio, 'burn': burn, 'runway': runway,
        'runway_unit': period_unit, 'verdict_cn': verdict, 'gaps': gaps, 'unit': unit,
    }
    res['table_md'] = render_table(res)
    res['text'] = render_text(res)
    return res


def _verdict(*, ocf: float, fcf: float, ratio: Optional[float],
             runway: Optional[float], unit: str, period_unit: str = '年') -> str:
    """一句话判断（先答结论：造血还是烧钱）。"""
    if fcf < 0:
        base = f'⚠️ **在烧钱**：自由现金流为负（每期净消耗 {abs(round(fcf, 2))} {unit}）'
        if runway is not None:
            base += f'，按当前现金余额大约还能撑 **{runway} {period_unit}**'
        return base + '。'
    if ratio is not None and ratio < 0.5:
        return (f'⚠️ **利润含金量存疑**：经营现金流仅覆盖净利润的 {ratio:.2f} 倍'
                f'（长期 <0.5 常见于应收/存货占用），虽 FCF 为正（{round(fcf, 2)} {unit}）。')
    if ratio is not None and ratio >= 1:
        return (f'✓ **造血正常**：经营现金流 {round(ocf, 2)} {unit}，'
                f'覆盖净利润 {ratio:.2f} 倍，FCF {round(fcf, 2)} {unit}。')
    return f'FCF 为正（{round(fcf, 2)} {unit}），现金含量需结合净利润进一步判断。'


def render_table(res: Dict[str, Any]) -> str:
    """现金流指标表（缺项标「未获取」，不隐藏整表）。"""
    if not res.get('ok'):
        return ''
    u = res.get('unit', '亿元')
    rows = [
        ('经营现金流净额（OCF）', _v(res.get('ocf'), u)),
        ('资本开支（Capex）', _v(res.get('capex'), u) if res.get('capex') is not None
         else '未获取'),
        ('自由现金流（FCF）', _v(res.get('fcf'), u) + ('（近似）' if res.get('fcf_is_approx') else '')),
        ('货币资金', _v(res.get('cash'), u) if res.get('cash') is not None else '未获取'),
        ('现金含量（OCF/净利润）', f"{res['ocf_ni']:.2f}" if res.get('ocf_ni') is not None
         else '未获取'),
        ('每期现金净消耗', _v(res.get('burn'), u) if res.get('burn') else '无（净流入）'),
        ('现金跑道', f"{res['runway']} {res.get('runway_unit', '年')}"
         if res.get('runway') is not None else '—'),
    ]
    lines = ['**现金流体检**', '', '| 指标 | 数值 |', '| --- | --- |']
    for k, v in rows:
        lines.append(f'| {k} | {v} |')
    return '\n'.join(lines)


def render_text(res: Dict[str, Any]) -> str:
    """报告用整段（结论 + 表 + 缺口）。"""
    if not res.get('ok'):
        return _gap_text(res.get('gaps') or [])
    lines = ['', '#### 💵 现金流', '', res.get('verdict_cn') or '', '',
             res.get('table_md') or '']
    g = res.get('gaps') or []
    if g:
        lines += ['', '> ' + '；'.join(g)]
    return '\n'.join(lines)


def _gap_text(gaps: List[str]) -> str:
    if not gaps:
        return ''
    return ('现金流数据缺口：' + '；'.join(gaps) + '。'
            '建议改用 `financial_statements` 取现金流量表，或查年报「合并现金流量表」。')


def _v(v: Any, unit: str) -> str:
    if v is None:
        return '未获取'
    return f'{v:,.2f} {unit}' if isinstance(v, (int, float)) else str(v)
