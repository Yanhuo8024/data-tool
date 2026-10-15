# -*- coding: utf-8 -*-
"""sotp_valuation.py — 分业务估值（Sum of the Parts）+ 汽车业务专项分析

## 为什么不能用单一 DCF 估一家多业务公司（2026-10-08 用户要求）

小米这类「手机 + IoT + 互联网服务 + 汽车」的混合体，各业务**商业模式与盈利阶段完全不同**：
用同一个折现率与同一个增长率去套，等于把成熟现金牛和烧钱孵化期混成一锅。
正因为此，上一轮才会出现「净利同比 +106.77% 被外推 5 年」这种模型失控。

按业务线匹配估值方法（用户指定，本模块即按此实现）：

| 业务 | 方法 | 理由 |
|---|---|---|
| 智能手机 | PE 或 EV/EBITDA | 成熟制造业，利润可比 |
| IoT | PE 或 PS | 增长期，利润波动大，收入更稳 |
| 互联网服务 | DCF | 高毛利、现金流稳定，适合折现 |
| 汽车 | PS 或 EV/Sales | **尚未稳定盈利**，用利润类倍数会失真 |

合计 = 分部加总 − 净负债。

汽车业务必须**单独深析**（毛利率趋势 / 单车毛利 / 经营利润率 / 研发分摊 / 现金消耗 / 盈亏平衡点）。

所有结论经 `report_evidence.claim()` 产出，携带依据、置信度与区间。
"""
import logging
from typing import Any, Dict, List, Optional

from report_evidence import assumption, claim, confidence

logger = logging.getLogger(__name__)

# 业务线关键词 → 推荐方法（按优先级排序，取第一个输入齐备的）
SEGMENT_METHOD_MAP = (
    (('智能手机', '手机', 'smartphone'), ('PE', 'EV/EBITDA')),
    (('iot', '生态链', '生活消费', '智能家居'), ('PE', 'PS')),
    (('互联网服务', '广告', '游戏', '增值服务', 'internet service'), ('DCF',)),
    (('汽车', '电动车', '智能电动', 'ev', 'automotive'), ('PS', 'EV/Sales')),
)
DEFAULT_METHODS = ('PE', 'PS')

# 各方法依赖的输入字段
METHOD_INPUTS = {
    'PE': ('net_profit', 'target_pe'),
    'PB': ('book_value', 'target_pb'),
    'PS': ('revenue', 'target_ps'),
    'EV/EBITDA': ('ebitda', 'target_ev_ebitda'),
    'EV/Sales': ('revenue', 'target_ev_sales'),
    'DCF': ('fcf', 'wacc', 'g1', 'g2'),
}
# EV 口径方法：结果先算成企业价值，需减去该分部净负债
EV_BASED = ('EV/EBITDA', 'EV/Sales', 'DCF')


def classify_segment(name: str) -> List[str]:
    """按业务线名称推断适用估值方法（返回优先级列表）。"""
    low = str(name or '').strip().lower()
    for keys, methods in SEGMENT_METHOD_MAP:
        if any(k in low for k in keys):
            return list(methods)
    return list(DEFAULT_METHODS)


def missing_inputs(method: str, seg: Dict[str, Any]) -> List[str]:
    """某方法还缺哪些输入（用于置信度与缺口披露）。"""
    return [k for k in METHOD_INPUTS.get(method, ()) if seg.get(k) in (None, '')]


def _pick_method(methods: List[str], seg: Dict[str, Any]) -> Optional[str]:
    """优先取输入齐备的方法；全不齐则取缺口最小的那个（仍会标缺口）。"""
    for m in methods:
        if not missing_inputs(m, seg):
            return m
    if not methods:
        return None
    return min(methods, key=lambda m: len(missing_inputs(m, seg)))


def value_by_method(method: str, seg: Dict[str, Any]) -> Dict[str, Any]:
    """单分部按指定方法估值。返回 {'value','basis','formula','missing'}（单位与输入一致，通常亿元）。"""
    miss = missing_inputs(method, seg)

    def _f(k, d=None):
        v = seg.get(k)
        try:
            return float(v)
        except (TypeError, ValueError):
            return d

    if method == 'PE' and not miss:
        v, basis = _f('net_profit') * _f('target_pe'), 'equity'
        formula = f"净利 {_f('net_profit')} × PE {_f('target_pe')}"
    elif method == 'PB' and not miss:
        v, basis = _f('book_value') * _f('target_pb'), 'equity'
        formula = f"净资产 {_f('book_value')} × PB {_f('target_pb')}"
    elif method == 'PS' and not miss:
        v, basis = _f('revenue') * _f('target_ps'), 'equity'
        formula = f"收入 {_f('revenue')} × PS {_f('target_ps')}"
    elif method == 'EV/EBITDA' and not miss:
        v, basis = _f('ebitda') * _f('target_ev_ebitda'), 'ev'
        formula = f"EBITDA {_f('ebitda')} × {_f('target_ev_ebitda')}"
    elif method == 'EV/Sales' and not miss:
        v, basis = _f('revenue') * _f('target_ev_sales'), 'ev'
        formula = f"收入 {_f('revenue')} × EV/Sales {_f('target_ev_sales')}"
    elif method == 'DCF' and not miss:
        v, basis = _dcf_ev(_f('fcf'), _f('wacc') / 100.0, _f('g1') / 100.0,
                           _f('g2') / 100.0, int(seg.get('years') or 5)), 'ev'
        formula = f"DCF(基数 {_f('fcf')}, WACC {_f('wacc')}%, g1 {_f('g1')}%, g2 {_f('g2')}%)"
    else:
        return {'value': None, 'basis': None, 'formula': None, 'missing': miss}
    if v is None:
        return {'value': None, 'basis': basis, 'formula': formula, 'missing': miss}
    if basis == 'ev':                      # 扣分部净负债，换成权益价值
        v -= _f('net_debt', 0.0) or 0.0
    return {'value': round(v, 2), 'basis': 'equity', 'formula': formula, 'missing': []}


def _dcf_ev(fcf: float, wacc: float, g1: float, g2: float, years: int = 5) -> Optional[float]:
    """两阶段 DCF 企业价值（与 valuation.dcf_valuation 同口径）。"""
    if not fcf or wacc <= g2 or wacc <= 0:
        return None
    pv = 0.0
    for t in range(1, years + 1):
        pv += fcf * (1 + g1) ** t / (1 + wacc) ** t
    tv = fcf * (1 + g1) ** years * (1 + g2) / (wacc - g2)
    return pv + tv / (1 + wacc) ** years


def value_sotp(segments: List[Dict[str, Any]], *, net_debt: float = 0.0,
               shares: float = None, market_cap: float = None,
               unit: str = '亿元') -> Dict[str, Any]:
    """分部加总估值：Σ 分部权益价值 − 净负债 → 每股价值。

    `segments` 每项：{'name', 'revenue', 'net_profit', 'ebitda', 'target_*', ...}
    方法缺输入时**不静默跳过**，而是记入 `gaps` 并压低置信度。
    """
    rows, gaps, assumptions = [], [], []
    for seg in (segments or []):
        name = str(seg.get('name') or '未命名分部')
        methods = seg.get('methods') or classify_segment(name)
        chosen = _pick_method(methods, seg)
        r = value_by_method(chosen, seg) if chosen else {
            'value': None, 'basis': None, 'formula': None, 'missing': ['(无可用方法)']}
        row = {'name': name, 'methods': methods, 'method': chosen,
               'value': r['value'], 'formula': r['formula'],
               'missing': r['missing'], 'revenue': seg.get('revenue'),
               'revenue_share': seg.get('revenue_share'),
               'gross_margin': seg.get('gross_margin')}
        rows.append(row)
        if r['missing']:
            gaps.append(f"{name}（{chosen}）缺 {'/'.join(r['missing'])}")
        for k in METHOD_INPUTS.get(chosen or '', ()):
            if k.startswith('target_') and seg.get(k) is not None:
                assumptions.append(assumption(
                    f'{name}·{k}', seg.get(k), unit='倍', adjustable=True,
                    basis=f'按 {chosen} 口径选取的可比倍数'))

    valued = [r for r in rows if r['value'] is not None]
    total = round(sum(r['value'] for r in valued), 2)
    equity = round(total - float(net_debt or 0.0), 2)
    per_share = round(equity / shares, 2) if shares else None

    covered = round(sum((r.get('revenue') or 0) for r in valued), 2)
    total_rev = round(sum((r.get('revenue') or 0) for r in rows), 2)

    disp = None
    if per_share and market_cap and shares:
        price = market_cap / shares
        disp = abs(per_share - price) / price * 100 if price else None

    conf = confidence(inputs_total=len(rows) or 1, inputs_missing=len(gaps),
                      dispersion_pct=disp, method_count=len({r['method'] for r in valued}))
    concl = claim(
        f"分部加总权益价值 {equity} {unit}"
        + (f"，每股 {per_share} 元" if per_share else ''),
        evidence=[f"{r['name']}：{r['method']} → {r['value']} {unit}（{r['formula']}）"
                  for r in valued][:6]
        + ([f"扣除净负债 {net_debt} {unit}"] if net_debt else []),
        conf=conf, method='分部加总（SOTP）',
        value_range=(None if not per_share else (round(per_share * 0.8, 2), round(per_share * 1.2, 2))),
        unit='元/股' if per_share else unit,
        caveats=[f'以下分部缺输入，未纳入加总：{"；".join(gaps)}'] if gaps else None)

    return {
        'rows': rows, 'gaps': gaps, 'assumptions': assumptions,
        'total_segment_value': total, 'net_debt': net_debt, 'equity_value': equity,
        'per_share': per_share, 'unit': unit,
        'revenue_coverage_pct': round(covered / total_rev * 100, 1) if total_rev else None,
        'claim': concl,
        'confidence': conf,
        'note': '分部价值口径已统一为权益价值（EV 类方法已扣该分部净负债），'
                '加总后统一扣除集团净负债。',
    }


# ==================== 汽车业务专项 ====================
CAR_KEYS = ('汽车', '电动车', '智能电动', 'automotive', 'ev')


def is_car_segment(name: str) -> bool:
    low = str(name or '').lower()
    return any(k in low for k in CAR_KEYS)


def car_deep_dive(car: Dict[str, Any]) -> Dict[str, Any]:
    """汽车业务六项专项分析（用户逐条点名）。

    输入（能填多少填多少，缺的进 missing 而不是编造）：
      gross_margin_trend : [(期, 毛利率%)]，用于判断趋势
      asp                : 单车均价（万元）
      revenue            : 分部收入（亿元）
      gross_margin       : 当期毛利率（%）
      operating_profit   : 分部经营利润（亿元）
      rd_total           : 集团研发费用（亿元）
      rd_share_pct       : 汽车占研发比例（%）
      ocf, capex         : 经营现金流、资本开支（亿元）
      fixed_cost         : 年固定成本（亿元，用于盈亏平衡）
      deliveries         : 交付量（万辆）
    """
    m: Dict[str, Any] = {}
    missing: List[str] = []
    evidence: List[str] = []
    assumptions: List[Dict[str, Any]] = []

    def num(k, d=None):
        try:
            return float(car.get(k))
        except (TypeError, ValueError):
            return d

    # ① 毛利率趋势
    trend = car.get('gross_margin_trend') or []
    gm = num('gross_margin')
    if trend and len(trend) >= 2:
        vals = [float(v) for _, v in trend]
        delta = vals[-1] - vals[0]
        direction = '上行' if delta > 1 else ('下行' if delta < -1 else '基本持平')
        m['gross_margin_trend'] = {
            'points': trend, 'direction': direction, 'change_pp': round(delta, 2)}
        evidence.append(f"毛利率 {trend[0][0]} {vals[0]}% → {trend[-1][0]} {vals[-1]}%（{direction}）")
        if delta < 0:
            m['gross_margin_warning'] = '毛利率下行，需核查价格战/成本上升'
    elif gm is not None:
        m['gross_margin_trend'] = {'points': [], 'direction': '样本不足', 'change_pp': None}
        evidence.append(f'当期毛利率 {gm}%（缺少多期数据，趋势待补）')
    else:
        missing.append('gross_margin_trend/gross_margin')

    # ② 单车毛利
    asp = num('asp')
    if asp is not None and gm is not None:
        per_unit = asp * gm / 100.0
        m['per_unit_gross_profit'] = round(per_unit, 2)
        evidence.append(f'单车毛利 = 均价 {asp} 万 × 毛利率 {gm}% = {round(per_unit, 2)} 万元')
    else:
        missing.append('asp')

    # ③ 经营利润率
    rev, op = num('revenue'), num('operating_profit')
    if rev and op is not None:
        m['operating_margin_pct'] = round(op / rev * 100, 2)
        evidence.append(f'经营利润率 = 经营利润 {op} / 收入 {rev} = {m["operating_margin_pct"]}%')
        if m['operating_margin_pct'] < 0:
            m['operating_warning'] = '分部经营亏损'
    else:
        missing.append('operating_profit')

    # ④ 研发分摊
    rd, rd_share = num('rd_total'), num('rd_share_pct')
    if rd is not None and rd_share is not None:
        alloc = rd * rd_share / 100.0
        m['rd_allocation'] = round(alloc, 2)
        assumptions.append(assumption('汽车占研发比例', rd_share, unit='%', adjustable=True,
                                      basis='分摊口径由披露/估算确定，属可调假设'))
        evidence.append(f'研发分摊 = 研发 {rd} 亿 × {rd_share}% = {round(alloc, 2)} 亿元')
    else:
        missing.append('rd_total/rd_share_pct')

    # ⑤ 现金消耗
    ocf, capex = num('ocf'), num('capex')
    if ocf is not None or capex is not None:
        burn = (ocf or 0.0) - (capex or 0.0)
        m['cash_burn'] = round(burn, 2)
        m['cash_state'] = '净消耗' if burn < 0 else '自我造血'
        evidence.append(f'现金{"消耗" if burn < 0 else "净流入"} = 经营现金流 {ocf} − 资本开支 {capex} '
                        f'= {round(burn, 2)} 亿元（{m["cash_state"]}）')
    else:
        missing.append('ocf/capex')

    # ⑥ 盈亏平衡点
    fixed = num('fixed_cost')
    per_unit = m.get('per_unit_gross_profit')
    if fixed and per_unit:
        m['breakeven_units'] = round(fixed / per_unit, 2)          # 万辆
        m['breakeven_revenue'] = round(fixed / (gm / 100.0), 2) if gm else None
        evidence.append(f'盈亏平衡 = 固定成本 {fixed} 亿 / 单车毛利 {per_unit} 万 '
                        f'= {m["breakeven_units"]} 万辆')
        deliv = num('deliveries')
        if deliv:
            m['breakeven_gap_units'] = round(m['breakeven_units'] - deliv, 2)
            evidence.append(f'当前交付 {deliv} 万辆，距平衡点还差 {m["breakeven_gap_units"]} 万辆'
                            if m['breakeven_gap_units'] > 0 else
                            f'当前交付 {deliv} 万辆，已越过平衡点 {abs(m["breakeven_gap_units"])} 万辆')
    else:
        missing.append('fixed_cost')

    conf = confidence(inputs_total=6, inputs_missing=len(set(missing)),
                      source_tier=car.get('source_tier'))
    _state = '已实现盈利' if (m.get('operating_margin_pct') or -1) >= 0 else '尚未稳定盈利'
    _gap_txt = '缺口未纳入：' + '、'.join(sorted(set(missing)))
    c = claim(
        '汽车业务：' + _state + '，建议用 PS / EV-Sales 估值（利润类倍数会失真）',
        evidence=evidence, conf=conf, method='汽车业务专项',
        value_range=car.get('value_range'),
        caveats=[_gap_txt] if missing else None)

    return {'metrics': m, 'missing': sorted(set(missing)), 'assumptions': assumptions,
            'claim': c, 'confidence': conf}