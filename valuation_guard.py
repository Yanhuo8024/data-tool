# -*- coding: utf-8 -*-
"""valuation_guard.py — DCF 估值的硬校验层（确定性规则，可复现、可审计）

## 为什么需要这一层（2026-10-08 实战）

线上 DCF 对小米集团-W（HK1810）给出「内在价值 313.59 元 vs 现价 23.94 元，
偏差 +1209.9%」，并据此输出「低估」。这是三处缺陷叠加：

1. **币种混算**：港股现价取自 `stock_hk_daily`（**港元**），而净利/股东权益取自
   东财港股财报（**人民币**）→ `spread = (人民币每股 − 港元股价) / 港元股价`。
2. **单年高增速外推**：`g1` 直接取「净利润同比」（小米 +106.77%，系低基数修复），
   作为未来 5 年**恒定**增速 → 终值指数爆炸。
3. **没有任何合理性闸门**：算出来多少就报多少，1000% 偏差照样给「低估」结论。

## 本模块的闸门（自上而下，任一触发即拒绝方向性结论）

| 闸门 | 条件 | 裁定 |
|---|---|---|
| `frozen` | 现价×总股本 与 独立市值来源偏差 > 5% | 冻结估值，不出结论 |
| `model_anomaly` | 每股内在价值 / 现价 > 2 或 < 0.5 | 「模型异常」 |
| `assumption_extreme` | 偏差 > 100% | 只出「假设极端，需人工复核」 |
| `cross_conflict` | DCF 隐含倍数与市场倍数背离 > 3 倍 | 降级为人工复核 |

**验收标准**：再出现 1000% 偏差时，本模块拦截，不生成「低估」结论。

零项目依赖（仅标准库），汇率随输出一并标注口径与日期。
"""
import logging
import os

logger = logging.getLogger(__name__)

# ==================== 汇率口径 ====================
# 说明：这里只做**量级对齐**，不做精确换汇。所有输出都会带上口径与日期，
# 并在风控层按「汇率假设」提示人工复核。
# 推导：USD/CNY 7.1180 ÷ USD/HKD 7.8000 = 0.9126。
# 可用环境变量覆盖：FX_HKD_CNY / FX_USD_CNY。
FX_AS_OF = '2026-10-08'
FX_SOURCE = '内置参考汇率（USD 交叉推算，仅供量级对齐；精确估值应改用交易日报价）'
FX_TO_CNY = {
    'CNY': 1.0,
    'HKD': float(os.environ.get('FX_HKD_CNY', '0.9126')),
    'USD': float(os.environ.get('FX_USD_CNY', '7.1180')),
}

# 市场 → 股价计价币种 / 财报币种
# ⚠️ 港股中资股（小米、腾讯等）财报以**人民币**列示，但股价以**港元**计价 ——
#    这是本案偏差的来源。少数港股以港元/美元记账，故该假设会写入输出供人工确认。
MARKET_CCY = {'hk': ('HKD', 'CNY'), 'a': ('CNY', 'CNY')}

# ==================== 阈值（可用环境变量按需调优）====================
MARKET_CAP_TOL = float(os.environ.get('VG_MARKET_CAP_TOL', '0.05'))   # 市值反推偏差容忍 5%
RATIO_HIGH = float(os.environ.get('VG_RATIO_HIGH', '2.0'))            # 内在价值/现价 上界
RATIO_LOW = float(os.environ.get('VG_RATIO_LOW', '0.5'))              # 内在价值/现价 下界
SPREAD_EXTREME = float(os.environ.get('VG_SPREAD_EXTREME', '100.0'))  # |偏差| 超此值只出「假设极端」
GROWTH_CAP = float(os.environ.get('VG_GROWTH_CAP', '25.0'))           # 单年增速作 5 年恒定增速的上限(%)
CROSS_CONFLICT_X = float(os.environ.get('VG_CROSS_CONFLICT_X', '3.0'))  # 隐含/市场倍数背离阈值


def to_cny(value, ccy):
    """把金额换算成人民币。ccy 未知时按 1:1 处理并交由上层标注。"""
    if value is None:
        return None
    rate = FX_TO_CNY.get(str(ccy or 'CNY').upper())
    if rate is None:
        return value
    return value * rate


def market_currencies(market):
    """返回 (股价币种, 财报币种)。"""
    return MARKET_CCY.get(str(market or 'a').lower(), ('CNY', 'CNY'))


def normalize_growth(g1_raw, years=5, cap=GROWTH_CAP):
    """把「单年同比」规整为可用于多年外推的增速。

    单年高增速常来自低基数修复（小米净利同比 +106.77%），直接外推 5 年会指数爆炸。
    这里按上限截断；被截断即产生 flag，供风控与报告显式披露。

    返回 (g1_used, flags)
    """
    flags = []
    try:
        g = float(g1_raw if g1_raw is not None else 0.0)
    except (TypeError, ValueError):
        return 0.0, ['growth_unparsable']
    if g < 0:
        return 0.0, ['growth_negative_clamped']
    if years >= 3 and g > cap:
        flags.append('single_year_growth_capped')
        return cap, flags
    return g, flags


def evaluate(*, sn, per_share, g1_raw, g1_used, r, g2, years, base,
             reported_market_cap=None, base_mode='net_profit', shares_source=''):
    """DCF 出口的唯一校验函数。

    入参：
      sn                  —— 快照（需含 price / shares / market / net_debt / eps / bps）
      per_share           —— 模型算出的每股内在价值（**财报币种**，即人民币）
      g1_raw / g1_used    —— 原始单年增速 / 规整后增速（%）
      r / g2              —— 折现率 / 永续增长率（小数）
      reported_market_cap —— 独立市值（元）；None 时由调用方尽力获取
      base_mode           —— 估值基数口径（net_profit / ocf）

    返回 dict，含可用作结论的 verdict / verdict_cn，以及全部校验痕迹。
    """
    market = sn.get('market') or 'a'
    price_ccy, fin_ccy = market_currencies(market)
    price = sn.get('price')
    shares = sn.get('shares')
    flags = []
    notes = []

    # ---- ⓪ 基数口径披露：净利润 ≠ 自由现金流 ----
    # 净利润未扣资本开支与营运资本占用，直接当 FCF 会系统性高估（重资产公司尤甚）。
    # 这不是 bug 而是模型简化的边界，必须显式说明，否则用户会把粗算当精确定价。
    if base_mode != 'ocf':
        flags.append('base_is_net_profit')
        notes.append('估值基数用的是**净利润**而非自由现金流（未扣资本开支/营运资本），'
                     '属粗算口径，会系统性偏高；结论仅供量级参考。')

    # ---- ① 币种统一：把现价换算到财报币种后再比 ----
    rate = FX_TO_CNY.get(price_ccy, 1.0) / FX_TO_CNY.get(fin_ccy, 1.0) \
        if fin_ccy != price_ccy else 1.0
    price_fin = price * rate if price is not None else None
    currency = {
        'price_ccy': price_ccy, 'fin_ccy': fin_ccy,
        'fx_rate': round(rate, 6), 'fx_as_of': FX_AS_OF, 'fx_source': FX_SOURCE,
        'price_original': price,
        'price_in_fin_ccy': round(price_fin, 4) if price_fin is not None else None,
        'converted': price_ccy != fin_ccy,
    }
    if price_ccy != fin_ccy:
        flags.append('currency_converted')

    if not price_fin or per_share is None:
        return _result(None, 'nodata', '数据不足，无法给出估值结论', flags,
                       currency, {}, None, notes=notes)

    # ---- ⓪′ 增速截断披露 ----
    if g1_raw is not None and g1_used is not None and float(g1_raw) > float(g1_used) + 1e-9:
        flags.append('single_year_growth_capped')
        notes.append(f'高增速原值 {float(g1_raw):.2f}%（单年同比）已按 {float(g1_used):.2f}% 截断，'
                     f'避免单年高增速外推 5 年导致终值指数膨胀。')

    # ---- ② 市值反推：现价×总股本 vs 独立市值 ----
    cap = _market_cap_guard(price, shares, price_ccy, reported_market_cap, shares_source)
    if cap.get('checked') and not cap.get('traceable'):
        flags.append('market_cap_untraceable')   # 数值对得上但来源不可追溯 → 显式披露

    # ---- ③ 异常阈值（标记，可与结论闸门叠加）----
    # 注意这两条**语义不同**，不要合并：
    #   「模型异常」是**诊断标记**（比值越界，说明模型输出不可信）；
    #   「假设极端」是**输出闸门**（偏差量级已超出可解释范围，禁止给方向）。
    # 比值 > 2 与偏差 > +100% 在数值上重合，故断言 1000% 场景应同时命中两者。
    spread = (per_share - price_fin) / price_fin * 100.0
    ratio = per_share / price_fin
    if ratio > RATIO_HIGH or ratio < RATIO_LOW:
        flags.append('model_anomaly')
        notes.append(f'每股内在价值为现价的 {ratio:.2f} 倍，'
                     f'越出合理区间 {RATIO_LOW}~{RATIO_HIGH}，该模型输出不可作为结论依据。')

    # ---- ④ 结论闸门（优先级从高到低）----
    if cap.get('frozen'):
        flags.append('frozen_market_cap')
        verdict, vcn = 'frozen', (
            f"估值冻结：现价×总股本反推市值与独立来源偏差 "
            f"{cap.get('deviation_pct'):.1f}%（> {MARKET_CAP_TOL * 100:.0f}%），"
            f"数据口径存疑，不出结论")
    elif abs(spread) > SPREAD_EXTREME:
        verdict = 'assumption_extreme'
        vcn = (f"假设极端，需人工复核：偏差 {spread:+.1f}%，已超出 ±{SPREAD_EXTREME:.0f}% "
               f"可解释范围，不输出低估/高估判断")
        if 'model_anomaly' in flags:
            vcn += "（同时标记：模型异常，比值越界）"
    elif 'model_anomaly' in flags:
        verdict, vcn = 'model_anomaly', (
            f"模型异常：每股内在价值为现价的 {ratio:.2f} 倍"
            f"（合理区间 {RATIO_LOW}~{RATIO_HIGH}），结果不可用")
    elif spread > 20:
        verdict, vcn = 'undervalued', '低估（内在价值高于现价 20% 以上）'
    elif spread < -20:
        verdict, vcn = 'overvalued', '高估（内在价值低于现价 20% 以上）'
    else:
        verdict, vcn = 'fair', '估值合理（内在价值与现价差距在 ±20% 内）'

    # ---- ⑤ 交叉验证（禁止单点 DCF）----
    cross = _cross_validate(sn, per_share, price_fin)
    if cross.get('conflict') and verdict in ('undervalued', 'overvalued'):
        flags.append('cross_conflict')
        verdict, vcn = 'needs_review', (
            f"需人工复核：DCF 结论与相对估值（{'/'.join(cross.get('available') or [])}）"
            f"背离 {cross.get('multiple_x')} 倍，单点 DCF 不足以支撑方向性结论")

    # ---- ⑥ 敏感性三档区间 ----
    sens = sensitivity_triple(base, r, g2, g1_used, years,
                              shares, sn.get('net_debt') or 0, price_fin)

    return _result(spread, verdict, vcn, flags, currency, cross, cap, sens, notes=notes,
                   extra={'per_share_original_ccy': per_share,
                          'price_fin_ccy': round(price_fin, 4),
                          'g1_raw': g1_raw, 'g1_used': g1_used})


def _result(spread, verdict, vcn, flags, currency, cross, cap,
            sens=None, extra=None, notes=None):
    out = {
        'spread_pct': round(spread, 1) if spread is not None else None,
        'verdict': verdict, 'verdict_cn': vcn, 'flags': flags,
        'notes': notes or [],
        'currency': currency, 'cross_check': cross or {}, 'market_cap': cap or {},
        'sensitivity_range': sens or {},
    }
    out.update(extra or {})
    return out


def _market_cap_guard(price, shares, price_ccy, reported=None, shares_source=''):
    """现价 × 总股本 反推市值，与独立来源比对；偏差超阈值 → 冻结。

    港股总股本由「股东权益 / 每股净资产」推算，口径脆弱 —— 这正是需要反推核验的原因。
    取不到独立市值时**不静默通过**，而是标记 checked=False 交人工。

    🚨 **总股本必须可追溯**（2026-10-09 用户要求）：``shares_source`` 记录总股本取自哪个
    工具/字段（如 `akshare stock_individual_info_em · 总股本`）。未标注时 ``traceable=False``
    —— 数值可能对得上，但**审计时无法复现**，故在结论里显式披露。

    注：判定口径与 ``valuation_caliber.verify_market_cap`` 保持一致（同一套阈值语义）。
    """
    if not price or not shares:
        return {'checked': False, 'reason': '缺现价或总股本，无法反推', 'frozen': False,
                'shares_source': shares_source or '未标注',
                'traceable': bool(shares_source)}
    computed = price * shares                              # 计价币种
    computed_cny = to_cny(computed, price_ccy)
    res = {'checked': bool(reported), 'frozen': False,
           'computed_yi_cny': round(computed_cny / 1e8, 2),
           'reported_yi_cny': round(to_cny(reported, 'CNY') / 1e8, 2) if reported else None,
           'tolerance_pct': MARKET_CAP_TOL * 100,
           'shares_source': shares_source or '未标注',
           'traceable': bool(shares_source)}
    if not reported:
        res['reason'] = '未取到独立市值来源，未能核验（不视为通过）'
        return res
    dev = abs(computed_cny - reported) / reported * 100.0
    res['deviation_pct'] = round(dev, 2)
    res['frozen'] = dev > MARKET_CAP_TOL * 100
    if not res['frozen'] and not shares_source:
        res['reason'] = ('数值核验通过，但**总股本来源未标注** → 核验结论不可追溯，'
                         '请补股本来源（如 `akshare stock_individual_info_em` 总股本字段）')
    return res


def _cross_validate(sn, per_share, price_fin):
    """PE / PB / PS 交叉验证：DCF 隐含倍数与市场倍数背离过大即判冲突。

    目标是拦住「单点 DCF 拍出一个数就当结论」——DCF 是绝对估值，
    必须与相对估值口径互证。
    """
    eps = sn.get('eps')
    bps = sn.get('bps')
    m = {}
    xs = []
    if eps and eps > 0:
        m['pe_market'] = round(price_fin / eps, 2)
        m['pe_dcf'] = round(per_share / eps, 2)
        if m['pe_market']:
            xs.append(m['pe_dcf'] / m['pe_market'])
    if bps and bps > 0:
        m['pb_market'] = round(price_fin / bps, 2)
        m['pb_dcf'] = round(per_share / bps, 2)
        if m['pb_market']:
            xs.append(m['pb_dcf'] / m['pb_market'])
    m['available'] = [k.split('_')[0].upper() for k in m if k.startswith('pe_') or k.startswith('pb_')]
    m['available'] = sorted(set(m['available']))
    m['ebitda'] = 'unavailable'      # 快照未取 EBITDA，显式标注不可算而非省略
    if not xs:
        m['conflict'] = False
        m['note'] = '缺少 EPS/BPS，无法交叉验证'
        return m
    ratio = sum(xs) / len(xs)
    m['multiple_x'] = round(ratio, 2)
    m['conflict'] = ratio > CROSS_CONFLICT_X or ratio < 1 / CROSS_CONFLICT_X
    return m


def sensitivity_triple(base, r, g2, g1, years, shares, net_debt, price_fin=None):
    """乐观 / 中性 / 悲观三档 → 输出区间而非单点。

    乐观：折现率 −2pp、永续 +0.5pp、高增速 +3pp
    悲观：折现率 +2pp、永续 −0.5pp、高增速 −3pp
    """
    if not base or not shares:
        return {}
    cases = {
        'optimistic': (r - 0.02, g2 + 0.005, g1 + 3),
        'neutral': (r, g2, g1),
        'pessimistic': (r + 0.02, g2 - 0.005, g1 - 3),
    }
    out = {}
    for name, (rr, gg2, g1v) in cases.items():
        if rr <= gg2 or rr <= 0:
            out[name] = None
            continue
        out[name] = round(_per_share(base, rr, gg2, g1v, years, shares, net_debt), 2)
    vals = [v for v in out.values() if v is not None]
    if vals:
        out['low'] = min(vals)
        out['high'] = max(vals)
        out['span_pct'] = round((max(vals) - min(vals)) / (min(vals) or 1) * 100, 1)
        if price_fin:
            out['covers_price'] = bool(min(vals) <= price_fin <= max(vals))
    return out


def _per_share(base, r, g2, g1_pct, years, shares, net_debt):
    """两阶段 DCF 每股价（与 valuation.dcf_valuation 同口径）。"""
    g1 = g1_pct / 100.0
    pv = 0.0
    for t in range(1, int(years) + 1):
        pv += base * (1 + g1) ** t / (1 + r) ** t
    tv = base * (1 + g1) ** years * (1 + g2) / (r - g2)
    ev = pv + tv / (1 + r) ** years
    return (ev - (net_debt or 0)) / shares


def fetch_market_cap(symbol, market=None):
    """尽力获取独立市值（人民币元）。取不到返回 None —— 由上层标记「未核验」。

    独立来源的意义：与「现价×总股本」互为校验，任一口径出错都能被发现。
    """
    try:
        import akshare as ak
    except ImportError:
        return None
    mk = (market or '').lower()
    try:
        if mk == 'a':
            info = ak.stock_individual_info_em(symbol=symbol)
            if info is not None and not info.empty and 'item' in info.columns:
                hit = info[info['item'].astype(str).str.contains('总市值')]
                if not hit.empty:
                    return float(hit.iloc[0].get('value') or 0) or None
        else:
            df = ak.stock_hk_valuation_baidu(symbol=symbol, indicator='总市值', period='近一年')
            if df is not None and not df.empty:
                return float(df.iloc[-1].get('value') or 0) * 1e8 or None
    except Exception as e:                       # 外部接口失败不应影响估值主流程
        logger.debug('获取独立市值失败 %s: %s', symbol, e)
    return None