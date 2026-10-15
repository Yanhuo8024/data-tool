# -*- coding: utf-8 -*-
"""多语言与多市场财报口径：A 股 / 港股 / 美股的差异统一。

三处最容易出错的地方（也是本模块存在的理由）
------------------------------------------
1. **计价币种 ≠ 财报币种**：港股用港元交易，但多数公司以**人民币**记账
   （小米、腾讯）；美股以美元交易且以美元记账。直接用「股价 − 每股收益」会得出错误偏差。
2. **财年制不同**：A 股/多数港股为自然年（12 月结）；美股由公司自定
   （苹果 9 月结、微软 6 月结）→ 报告期无法直接横向比较，必须显式标注财年截止日。
3. **报表字段名不同**：同一含义在三地分别叫「营业总收入 / 营业额 / Total Revenue」，
   「归属于母公司股东的净利润 / 股东应占溢利 / Net Income」→ 取数必须走**别名映射**。

本模块给出：市场规格表、字段别名映射、币种与财年统一、以及可选的行情/财报取数
（取数延迟 import akshare，失败如实上报，不伪造）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ==================== 市场规格 ====================

# 汇率（→ 人民币）与口径说明。**汇率为参考值，使用时必须标注日期**（见 FX_AS_OF）。
FX_AS_OF = '2026-10-08'
FX_TO_CNY = {'CNY': 1.0, 'HKD': 0.9126, 'USD': 7.12}

MARKETS: Dict[str, Dict[str, Any]] = {
    'a': {
        'name': 'A 股', 'price_ccy': 'CNY', 'fin_ccy': 'CNY',
        'standard': '中国会计准则（CAS）', 'fiscal': '自然年（12 月结）',
        'unit': '元', 'trading': 'T+1', 'limit': '±10%（ST ±5%）',
        'report_names': ('年报', '半年报', '季报'), 'code_rule': '6 位数字（如 600519）',
    },
    'hk': {
        'name': '港股', 'price_ccy': 'HKD', 'fin_ccy': 'CNY',
        'standard': 'IFRS / 香港财务报告准则', 'fiscal': '多为 12 月结，少数 3 月结',
        'unit': '元', 'trading': 'T+0', 'limit': '无涨跌幅限制',
        'report_names': ('年报', '中期报告', '业绩公告'), 'code_rule': 'HK + 4~5 位（如 HK01810）',
        'caliber_note': '以港元计价、但多数公司以人民币记账，比较前必须统一币种。',
    },
    'us': {
        'name': '美股', 'price_ccy': 'USD', 'fin_ccy': 'USD',
        'standard': 'US GAAP', 'fiscal': '公司自定财年（如苹果 9 月结、微软 6 月结）',
        'unit': '美元', 'trading': 'T+0', 'limit': '无涨跌幅限制（有熔断）',
        'report_names': ('10-K', '10-Q', '8-K', '业绩电话会'), 'code_rule': '字母代码（如 AAPL）',
        'caliber_note': '财年常与自然年不一致，必须标注财年截止日（FY end）。',
    },
}

# ==================== 字段别名 ====================

# 内部字段 → 三地常见报表名（用于从任意口径文本/表头映射到内部字段）
FIELD_ALIASES: Dict[str, tuple] = {
    'revenue': ('营业总收入', '营业收入', '营业额', '收入合计', 'Revenue', 'Total Revenue',
                'Net Sales', 'Operating Revenue', 'Revenues'),
    'net_profit': ('归属于母公司股东的净利润', '归母净利润', '净利润', '股东应占溢利',
                   'Profit for the year', 'Net Income', 'Net Profit', 'Profit attributable to'),
    'adjusted_profit': ('经调整净利润', 'Non-GAAP net income', 'Adjusted net profit',
                        '经调整纯利'),
    'gross_profit': ('毛利', 'Gross Profit'),
    'operating_profit': ('营业利润', '经营利润', 'Operating Income', 'Operating Profit'),
    'equity': ('所有者权益合计', '股东权益', '归属于母公司股东权益', '权益总额',
               "Total shareholders' equity", 'Total Equity', 'Total equity attributable'),
    'total_assets': ('资产总计', '总资产', 'Total Assets'),
    'total_liab': ('负债合计', '总负债', 'Total Liabilities'),
    'eps': ('基本每股收益', '每股收益', 'Basic EPS', 'Earnings per share', 'Basic earnings per share'),
    'bps': ('每股净资产', 'Net asset value per share', 'Book value per share'),
    'ocf': ('经营活动产生的现金流量净额', '经营活动现金流净额',
            'Net cash from operating activities', 'Cash flow from operations'),
    'capex': ('购建固定资产、无形资产和其他长期资产支付的现金',
              'Capital expenditure', 'Purchase of property'),
}

# 反向索引（一次构建，避免每次遍历）
_ALIAS_INDEX: Dict[str, str] = {}
for _std, _names in FIELD_ALIASES.items():
    for _n in _names:
        _ALIAS_INDEX[_n.lower()] = _std


# ==================== 基础接口 ====================


def market_spec(market: str, *, fin_ccy: Optional[str] = None) -> Dict[str, Any]:
    """取市场规格。``fin_ccy`` 可覆盖默认财报币种（港股需按公司确认）。"""
    key = normalize_market(market)
    spec = dict(MARKETS[key])
    spec['market'] = key
    if fin_ccy and fin_ccy in FX_TO_CNY:
        spec['fin_ccy'] = fin_ccy
        spec['fin_ccy_overridden'] = True
    spec['fx_to_cny'] = {c: r for c, r in FX_TO_CNY.items()}
    spec['fx_as_of'] = FX_AS_OF
    spec['price_fin_same_ccy'] = spec['price_ccy'] == spec['fin_ccy']
    return spec


def normalize_market(market: str) -> str:
    m = str(market or '').strip().lower()
    if m in ('a', 'cn', 'sh', 'sz', 'ashare', 'a股'):
        return 'a'
    if m in ('hk', 'hkg', 'hshare', '港股', '香港'):
        return 'hk'
    if m in ('us', 'usa', 'nasdaq', 'nyse', '美股', '美国'):
        return 'us'
    return 'a'


def normalize_symbol(code: str, market: str) -> str:
    """统一代码格式：A 股 6 位、港股 HK+5 位（去前导零补齐）、美股大写字母。"""
    m = normalize_market(market)
    c = str(code or '').strip().upper().replace(' ', '')
    c = c.replace('SH', '').replace('SZ', '').replace('BJ', '') if m == 'a' else c
    if m == 'a':
        digits = ''.join(ch for ch in c if ch.isdigit())
        return digits.zfill(6)[-6:] if digits else c
    if m == 'hk':
        digits = ''.join(ch for ch in c if ch.isdigit())
        return ('HK' + digits.zfill(5)) if digits else c
    return c


def infer_market(code: str) -> str:
    """从代码形态推断市场（前端未指定时用）。"""
    c = str(code or '').strip()
    if not c:
        return 'a'
    if c.upper().startswith('HK'):
        return 'hk'
    digits = ''.join(ch for ch in c if ch.isdigit())
    if digits and len(digits) in (5, 6) and c.isdigit():
        return 'a' if len(digits) == 6 else 'hk'
    if c.isalpha():
        return 'us'
    return 'a'


# ==================== 字段映射 ====================


def map_field(raw_name: str) -> Optional[str]:
    """把任意口径的报表字段名映射到内部标准字段名；未识别返回 None。"""
    if not raw_name:
        return None
    key = str(raw_name).strip().lower()
    if key in _ALIAS_INDEX:
        return _ALIAS_INDEX[key]
    # 退化匹配：包含关系（去空格后）
    flat = key.replace(' ', '')
    for alias, std in _ALIAS_INDEX.items():
        if alias.replace(' ', '') in flat or flat in alias.replace(' ', ''):
            return std
    return None


def financial_field_map(market: str) -> Dict[str, List[str]]:
    """该市场的字段映射表（内部字段 → 该市场常见叫法），供取数/提示词使用。"""
    m = normalize_market(market)
    prefer = {'a': ('营业总收入', '归属于母公司股东的净利润'),
              'hk': ('营业额', '股东应占溢利'),
              'us': ('Total Revenue', 'Net Income')}[m]
    out: Dict[str, List[str]] = {}
    for std, names in FIELD_ALIASES.items():
        ordered = list(names)
        for p in prefer:
            if p in ordered:
                ordered.remove(p)
                ordered.insert(0, p)
        out[std] = ordered
    return out


# ==================== 口径统一 ====================


def to_internal(snapshot: Dict[str, Any], market: str, *,
                fin_ccy: Optional[str] = None,
                fx_override: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """把任意市场的快照统一到**内部口径**：财报金额→人民币元，价格→人民币元。

    返回 ``{'spec','fx_used','snapshot','conversions','warnings'}``；
    ``conversions`` 逐项列出「原值 × 汇率 = 新值」，可直接进审计凭证的计算留痕。
    """
    spec = market_spec(market, fin_ccy=fin_ccy)
    fx = dict(FX_TO_CNY)
    if fx_override:
        fx.update(fx_override)
    price_ccy, fin_ccy2 = spec['price_ccy'], spec['fin_ccy']
    p_rate = fx.get(price_ccy, 1.0)
    f_rate = fx.get(fin_ccy2, 1.0)

    out: Dict[str, Any] = dict(snapshot or {})
    conv: List[Dict[str, Any]] = []
    warn: List[str] = []

    # 价格 → 人民币
    if out.get('price') not in (None, ''):
        try:
            raw = float(out['price'])
            new = round(raw * p_rate, 6)
            out['price'] = new
            out['price_original'] = raw
            conv.append({'name': f'{price_ccy}→CNY 股价', 'expr': f'{raw} × {p_rate}',
                         'inputs': {price_ccy: raw, 'rate': p_rate}, 'output': new, 'unit': '元'})
        except (TypeError, ValueError) as e:
            warn.append(f'price 无法解析：{e}')
    if p_rate != 1.0 or f_rate != 1.0:
        out['fx_as_of'] = FX_AS_OF

    # 财报金额 → 人民币（字段名可能非内部口径，先做别名映射）
    for k in list(out.keys()):
        std = map_field(k) or (k if k in FIELD_ALIASES else None)
        if not std or k in ('price', 'price_original'):
            continue
        v = out.get(k)
        if v in (None, '') or isinstance(v, (dict, list)):
            continue
        try:
            raw = float(v)
        except (TypeError, ValueError) as e:
            logger.debug('字段 %s 非数值，跳过换算: %s', k, e)
            continue
        new = round(raw * f_rate, 6)
        out.setdefault(std, new)
        if std != k:
            out[std] = new
        if f_rate != 1.0:
            conv.append({'name': f'{fin_ccy2}→CNY {std}', 'expr': f'{raw} × {f_rate}',
                         'inputs': {fin_ccy2: raw, 'rate': f_rate}, 'output': new, 'unit': '元'})

    if not spec['price_fin_same_ccy']:
        warn.append(f"{spec['name']}计价币种（{price_ccy}）与记账币种（{fin_ccy2}）不同，"
                    f"已按 {FX_AS_OF} 汇率统一；请在报告中标注汇率口径与日期。")
    out['market'] = spec['market']
    out['financial_currency'] = 'CNY'
    return {'spec': spec, 'fx_used': {c: fx.get(c, 1.0) for c in ('CNY', 'HKD', 'USD')},
            'snapshot': out, 'conversions': conv, 'warnings': warn}


def period_align(period: str, market: str) -> Dict[str, Any]:
    """报告期口径说明：美股必须显式给出财年截止日。"""
    m = normalize_market(market)
    p = str(period or '').strip()
    spec = market_spec(m)
    note = f"{spec['name']}：{spec['fiscal']}"
    aligned = p
    if m == 'us':
        note += '；「FY2024」指公司财年，不等于自然年，横向比较前须换算到同一时间窗。'
    return {'period': p, 'aligned': aligned, 'note': note}


def caliber_diff() -> List[Dict[str, Any]]:
    """三地口径对比表（给报告/教程直接用）。"""
    rows = []
    for k in ('a', 'hk', 'us'):
        s = market_spec(k)
        rows.append({'市场': s['name'], '计价币种': s['price_ccy'], '记账币种': s['fin_ccy'],
                     '会计准则': s['standard'], '财年制': s['fiscal'],
                     '单位': s['unit'], '交易制度': f"{s['trading']}，{s['limit']}"})
    return rows


def describe_market(market: str) -> str:
    """一段口径说明（可拼进报告或提示词）。"""
    s = market_spec(market)
    seg = [f"{s['name']}：以{s['price_ccy']}计价、以{s['fin_ccy']}记账，"
           f"适用{s['standard']}，{s['fiscal']}，单位为{s['unit']}。"]
    if s.get('caliber_note'):
        seg.append(s['caliber_note'])
    seg.append(f"汇率口径（{FX_AS_OF}）：1 HKD = {FX_TO_CNY['HKD']} CNY、1 USD = {FX_TO_CNY['USD']} CNY。")
    return ''.join(seg)


# ==================== 取数（延迟 import，失败如实上报） ====================


def _ak():
    try:
        import akshare as ak           # 延迟 import：无网/未装时不影响口径映射
        return ak
    except Exception as e:              # pragma: no cover - 环境相关
        logger.debug('akshare 不可用: %s', e)
        return None


def fetch_quote(symbol: str, market: str) -> Dict[str, Any]:
    """取最新行情。返回 ``{'symbol','market','price','change_pct','ccy','source'}``。

    失败返回 ``{'ok': False, 'error': …}`` —— **绝不编造数字**。
    """
    m = normalize_market(market)
    sym = normalize_symbol(symbol, m)
    ak = _ak()
    if ak is None:
        return {'ok': False, 'error': 'akshare 不可用', 'symbol': sym, 'market': m}
    try:
        if m == 'a':
            df = ak.stock_zh_a_spot_em()
            row = df[df['代码'] == sym]
            if row.empty:
                return {'ok': False, 'error': f'未找到 {sym} 的行情', 'symbol': sym, 'market': m}
            r = row.iloc[0]
            return {'ok': True, 'symbol': sym, 'market': m, 'price': float(r['最新价']),
                    'change_pct': float(r['涨跌幅']), 'ccy': 'CNY', 'source': '东方财富(A股现货)'}
        if m == 'hk':
            df = ak.stock_hk_spot_em()
            key = sym.replace('HK', '').zfill(5)
            row = df[df['代码'] == key]
            if row.empty:
                return {'ok': False, 'error': f'未找到 {sym} 的行情', 'symbol': sym, 'market': m}
            r = row.iloc[0]
            return {'ok': True, 'symbol': sym, 'market': m, 'price': float(r['最新价']),
                    'change_pct': float(r['涨跌幅']), 'ccy': 'HKD', 'source': '东方财富(港股现货)'}
        df = ak.stock_us_spot_em()
        code = sym.upper()
        row = df[df['代码'].astype(str).str.upper().str.endswith(code)]
        if row.empty:
            return {'ok': False, 'error': f'未找到 {code} 的行情', 'symbol': code, 'market': m}
        r = row.iloc[0]
        return {'ok': True, 'symbol': code, 'market': m, 'price': float(r['最新价']),
                'change_pct': float(r['涨跌幅']), 'ccy': 'USD', 'source': '东方财富(美股现货)'}
    except Exception as e:                # 网络/字段变更等，如实上报
        logger.warning('取行情失败 %s/%s: %s', sym, m, e)
        return {'ok': False, 'error': f'{type(e).__name__}: {e}', 'symbol': sym, 'market': m}


def fetch_financials(symbol: str, market: str) -> Dict[str, Any]:
    """取财报口径字段（尽力而为）。美股走 ``stock_financial_us_report_em``。"""
    m = normalize_market(market)
    sym = normalize_symbol(symbol, m)
    ak = _ak()
    if ak is None:
        return {'ok': False, 'error': 'akshare 不可用', 'symbol': sym, 'market': m}
    try:
        if m == 'us':
            df = ak.stock_financial_us_report_em(stock=sym, symbol='综合损益表', indicator='年报')
        elif m == 'a':
            df = ak.stock_financial_abstract(symbol=sym)
        else:
            return {'ok': False, 'symbol': sym, 'market': m,
                    'error': '港股无公开统一财报接口，请查港交所披露易年报「财务报表」附注'}
        return {'ok': True, 'symbol': sym, 'market': m, 'rows': len(df),
                'columns': [str(c) for c in list(df.columns)[:12]],
                'field_map': financial_field_map(m),
                'period': period_align(str(df.iloc[0, 0]) if len(df) else '', m)}
    except Exception as e:
        logger.warning('取财报失败 %s/%s: %s', sym, m, e)
        return {'ok': False, 'error': f'{type(e).__name__}: {e}', 'symbol': sym, 'market': m}
