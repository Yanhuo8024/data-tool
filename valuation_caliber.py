# -*- coding: utf-8 -*-
"""估值口径统一：**口径说不清，结论就降级**。

三件事
------
1. **PE 口径必须注明**：是 TTM（滚动十二个月）还是最近一个**完整财年**？
   两者可以差很多（尤其利润季度波动大的公司）。口径不明时**不得**拿来做比较基准。
2. **市值来源必须交叉核验**：「现价 × 总股本」与独立来源的市值对不上（>5%），
   说明至少一边口径错了（币种 / 股本口径 / 复权方式）→ 估值结论自动降级为「**待补充**」。
3. **DCF 参数敏感性矩阵**：即使模型被标记异常，也给出
   折现率 **8% / 9% / 10%** × 永续增长 **2% / 2.5% / 3%** 的九宫格，
   让读者看到**模型弹性**（能看到"要什么假设才支撑得起现价"）。

零项目依赖（仅标准库）；重算函数由调用方注入以避免 import 环。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

# PE 口径
PE_TTM = 'TTM'
PE_FY = 'FY'
PE_UNKNOWN = 'unknown'
PE_CALIBER_CN = {PE_TTM: 'TTM（滚动十二个月）', PE_FY: '最近完整财年',
                 PE_UNKNOWN: '未标注'}

# 默认口径不明确时的结论
PENDING = '待补充'

# 敏感性轴默认值（百分数）
DEFAULT_RATES = (8.0, 9.0, 10.0)
DEFAULT_GROWTHS = (2.0, 2.5, 3.0)


# ==================== ① PE 口径 ====================


def classify_pe_caliber(*hints: Optional[str]) -> str:
    """从字段名 / 来源说明里判断 PE 口径。判断不出返回 ``unknown``（**不猜**）。"""
    blob = ' '.join(str(h).lower() for h in hints if h)
    if not blob:
        return PE_UNKNOWN
    if any(k in blob for k in ('ttm', 'trailing', 'rolling', '滚动', '最近十二', '近十二')):
        return PE_TTM
    if any(k in blob for k in ('fy', 'annual', 'year end', '年报', '全年', '最近一期年报',
                               '静态', 'static')):
        return PE_FY
    return PE_UNKNOWN


def label_pe(pe: Optional[float], *hints: Optional[str],
             period: Optional[str] = None) -> Dict[str, Any]:
    """给 PE 打上口径标签。口径不明时 ``comparable=False``（不可直接横向比较）。"""
    cal = classify_pe_caliber(*hints)
    return {
        'value': pe,
        'caliber': cal,
        'caliber_cn': PE_CALIBER_CN[cal],
        'period': period or '',
        'comparable': cal != PE_UNKNOWN,
        'note': ('可直接与其他公司比较' if cal != PE_UNKNOWN else
                 '⚠️ 口径未标注（TTM 还是最近完整财年？），**不得**直接横向比较，'
                 '请补口径后再比'),
    }


# ==================== ② 市值交叉核验 ====================


def verify_market_cap(*, price: Optional[float], shares: Optional[float],
                      reported_cap: Optional[float],
                      tol: float = 0.05,
                      price_ccy_note: str = '',
                      shares_source: str = '',
                      shares_period: str = '',
                      reported_source: str = '') -> Dict[str, Any]:
    """「现价 × 总股本」与独立来源市值对照。

    🚨 **总股本必须带来源**（2026-10-09 用户要求）：总股本本身可能来自
    「总股本 / 流通股本 / 港股 H 股股本 / 加权平均股本」等不同口径，
    不标来源就无法审计。**来源缺失即便数值对得上也不算通过**（``traceable=False``）。

    未通过 → ``ok=False``，调用方应把估值结论降级为「待补充」。
    """
    out: Dict[str, Any] = {'computed': None, 'reported': reported_cap,
                           'deviation_pct': None, 'ok': False, 'tol': tol,
                           'price_ccy_note': price_ccy_note, 'reason': '',
                           'traceable': bool(shares_source),
                           'shares_source': shares_source or '未标注',
                           'shares_period': shares_period or '未标注',
                           'reported_source': reported_source or '未标注'}
    for name, v in (('price', price), ('shares', shares), ('reported_cap', reported_cap)):
        if v in (None, ''):
            out['reason'] = f'缺少 {name}，无法交叉核验市值'
            return out
    try:
        computed = float(price) * float(shares)
        reported = float(reported_cap)
    except (TypeError, ValueError) as e:
        out['reason'] = f'市值核验入参非数值: {e}'
        return out
    if reported == 0:
        out['reason'] = '独立来源市值为 0，无法核验'
        return out
    dev = (computed - reported) / reported * 100.0
    out['computed'] = round(computed, 2)
    out['deviation_pct'] = round(dev, 2)
    within = abs(dev) <= tol * 100
    if within and shares_source:
        out['ok'] = True
        out['reason'] = (f'交叉核验通过（偏差 {dev:+.2f}%，容差 ±{tol * 100:.0f}%）；'
                         f'总股本来源：{shares_source}')
    elif within:
        out['reason'] = (f'⚠️ 数值交叉核验通过（偏差 {dev:+.2f}%），但**总股本来源未标注**'
                         f'（现价 × 总股本不可追溯）→ 估值结论降级为「{PENDING}」。'
                         f'请补总股本来源（如 `akshare stock_individual_info_em` 总股本字段）。')
    else:
        out['reason'] = (f'❌ 交叉核验不通过：现价×总股本 = {computed:,.0f}，'
                         f'独立来源 = {reported:,.0f}，偏差 {dev:+.2f}%（容差 ±{tol * 100:.0f}%）。'
                         f'至少一边口径有误（币种 / 股本口径 / 复权方式），'
                         f'估值结论降级为「{PENDING}」。')
    return out


# ==================== ③ 结论闸门 ====================


def gate_conclusion(base_conclusion: str, *,
                    pe: Optional[Dict[str, Any]] = None,
                    market_cap: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """口径不明确 / 市值未核验 → 结论降级为「待补充」。"""
    issues: List[str] = []
    if pe is not None and not pe.get('comparable'):
        issues.append('PE 口径未明确（未注明 TTM 或最近完整财年）')
    if market_cap is None:
        issues.append('市值来源未做交叉核验')
    elif not market_cap.get('ok'):
        if not market_cap.get('traceable'):
            issues.append('总股本来源未标注，现价×总股本不可追溯（须补股本来源）')
        else:
            issues.append(f"市值交叉核验未通过（{market_cap.get('reason')}）")
    if issues:
        return {'verdict': 'pending_supplement', 'verdict_cn': PENDING,
                'base_conclusion': base_conclusion, 'issues': issues,
                'text': f'估值结论降级为「{PENDING}」：' + '；'.join(issues) +
                        '。补齐上述口径后方可给出方向性结论。'}
    return {'verdict': 'ok', 'verdict_cn': base_conclusion, 'base_conclusion': base_conclusion,
            'issues': [], 'text': base_conclusion}


# ==================== ④ DCF 敏感性矩阵 ====================


def sensitivity_matrix(per_share_fn: Callable[..., Optional[float]], *,
                       base: float, shares: float, net_debt: float, g1: float,
                       years: int = 5,
                       rates: Sequence[float] = DEFAULT_RATES,
                       growths: Sequence[float] = DEFAULT_GROWTHS,
                       price: Optional[float] = None,
                       unit: str = '元') -> Dict[str, Any]:
    """折现率 × 永续增长率的 3×3 每股价值矩阵。

    ``per_share_fn(base, r, g2, g1, years, shares, net_debt)`` 返回每股价值（**报表币种**）。
    ``rates`` / ``growths`` 为百分数。**调用方应确保 base 与 g1 已做过币种统一与增速截断。**
    """
    cells: List[List[Optional[float]]] = []
    flat: List[float] = []
    for r in rates:
        row: List[Optional[float]] = []
        for g2 in growths:
            try:
                v = per_share_fn(base, r / 100.0, g2 / 100.0, g1, years, shares, net_debt)
                v = round(float(v), 2) if v is not None else None
            except Exception as e:            # 单格失败不影响整表
                logger.debug('敏感性单元格 (r=%s, g=%s) 计算失败: %s', r, g2, e)
                v = None
            row.append(v)
            if v is not None:
                flat.append(v)
        cells.append(row)
    lo, hi = (min(flat), max(flat)) if flat else (None, None)
    spread = None
    if lo is not None and lo != 0:
        spread = round((hi - lo) / abs(lo) * 100.0, 2)
    return {
        'rates': list(rates), 'growths': list(growths), 'cells': cells,
        'low': lo, 'high': hi, 'spread_pct': spread,
        'covers_price': (price is not None and lo is not None
                         and lo <= float(price) <= hi),
        'price': price, 'unit': unit,
        'implied': _implied(cells, rates, growths, price),
        'table_md': render_matrix(cells, rates, growths, price, unit),
    }


def _implied(cells: List[List[Optional[float]]], rates: Sequence[float],
             growths: Sequence[float], price: Optional[float]) -> Optional[Dict[str, Any]]:
    """只给一个语义：**要什么假设才支撑得起现价**（求现价落点最近的格子）。"""
    if price is None:
        return None
    best = None
    for i, row in enumerate(cells):
        for j, v in enumerate(row):
            if v is None:
                continue
            d = abs(v - float(price))
            if best is None or d < best[0]:
                best = (d, rates[i], growths[j], v)
    if not best:
        return None
    return {'rate_pct': best[1], 'growth_pct': best[2], 'value': best[3],
            'distance': round(best[0], 2)}


def render_matrix(cells: List[List[Optional[float]]], rates: Sequence[float],
                  growths: Sequence[float], price: Optional[float] = None,
                  unit: str = '元') -> str:
    """把矩阵渲染成 Markdown 表（行=折现率，列=永续增长率）。"""
    head = '| 折现率 \\ 永续增长 | ' + ' | '.join(f'{g:g}%' for g in growths) + ' |'
    sep = '|---|' + '---|' * len(growths)
    lines = [head, sep]
    for r, row in zip(rates, cells):
        cells_txt = []
        for v in row:
            if v is None:
                cells_txt.append('—')
            elif price is not None and abs(v - float(price)) < abs(float(price)) * 0.05:
                cells_txt.append(f'**{v}**')        # 接近现价的格子加粗
            else:
                cells_txt.append(f'{v}')
        lines.append(f'| {r:g}% | ' + ' | '.join(cells_txt) + ' |')
    return '\n'.join(lines)


def describe_matrix(matrix: Dict[str, Any]) -> str:
    """一句话解读（给报告用）。"""
    lo, hi, spread = matrix.get('low'), matrix.get('high'), matrix.get('spread_pct')
    if lo is None:
        return '敏感性矩阵未产出（重算函数不可用）。'
    seg = (f'参数敏感性：折现率 {matrix["rates"][0]:g}%~{matrix["rates"][-1]:g}% × '
           f'永续增长 {matrix["growths"][0]:g}%~{matrix["growths"][-1]:g}% 下，'
           f'每股价值区间 **{lo} ~ {hi} {matrix.get("unit", "元")}**'
           + (f'（跨度 {spread:.0f}%）' if spread is not None else '') + '。')
    if matrix.get('covers_price'):
        seg += ' 现价落在区间内，说明当前价可由一组合理假设解释。'
    elif matrix.get('price') is not None:
        seg += ' ⚠️ 现价**落在区间之外**，需重新审视假设或价格是否存在非基本面因素。'
    imp = matrix.get('implied')
    if imp:
        # 用 % 格式化：f-string 在此处需嵌套同类引号，仅 3.12+ 支持
        seg += (' 若要支撑现价，最接近的假设组合是折现率 %.1f%% × 永续增长 %.1f%%'
                '（对应每股 %s）。' % (imp['rate_pct'], imp['growth_pct'], imp['value']))
    return seg
