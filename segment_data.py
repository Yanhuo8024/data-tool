# -*- coding: utf-8 -*-
"""segment_data.py — 分部（主营构成）数据：收入 / 同比 / 占比 / 毛利率 / 经营利润

## 为什么需要（2026-10-08 用户反馈）

用户问「收入占比」，系统回「暂未包含」——这是**答非所问**。
正确做法是**直接查财报分部附注**。本模块把这件事做成可调用能力：

- **A 股**：东财主营构成接口可直取（按产品/按地区/按行业），含收入、占比、毛利率，
  并由历史报告期自行推算同比。
- **港股**：公开接口**不提供**分部数据（实测 akshare 无对应函数）。
  此时**不允许**用「暂未包含」搪塞 —— 必须给出「缺什么、为什么缺、去哪里取、
  期望哪些字段」的**可执行缺口说明**，让上层要么去查港交所披露易年报附注，
  要么明确告知用户需要人工补充。

本模块零项目依赖（akshare 为惰性导入），每个字段都带溯源戳（见 data_provenance）。
"""
import logging
from typing import Any, Dict, List, Optional

from data_provenance import stamp

logger = logging.getLogger(__name__)

# 用户点名的字段口径
SEGMENT_FIELDS = ('收入', '同比', '占比', '毛利率', '经营利润')

# 港股分部数据的真实出处（接口取不到时交给用户/上层按图索骥）
HK_SEGMENT_SOURCE = ('港交所披露易 · 年报/中报「管理层讨论与分析」中的「分部资料」附注')
HK_SEGMENT_LINK = 'https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=zh'
HK_SEGMENT_HINT = ('分部收入通常列在年报附注「分部信息」(Segment Information)，'
                   '按业务线披露收入、分部业绩（经营利润）与分部资产；'
                   '小米集团-W 惯例分为：智能手机 / IoT 与生活消费产品 / 互联网服务 / '
                   '智能电动汽车及创新业务。')

_CATEGORY_DEFAULT = '按产品分类'


def _num(v, default=0.0):
    try:
        if v is None or v == '':
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def fetch_segments(code: str, market: str = None, category: str = _CATEGORY_DEFAULT,
                   limit: int = 8) -> Dict[str, Any]:
    """取一只股票的分部（主营构成）数据。

    返回统一结构：
      {ok, market, code, category, period, segments[], provenance[], gap|None}
      - segments 每项：{'name','revenue','yoy','share','gross_margin','profit'}
      - gap 非空表示**取不到**，含 reason / source / link / hint / fields（可执行缺口）
    """
    mk = (market or ('hk' if _looks_hk(code) else 'a')).lower()
    if mk == 'hk':
        return _hk_gap(code, category)
    return _fetch_a(code, category, limit)


def _looks_hk(code: str) -> bool:
    s = str(code or '').strip().upper().replace('HK', '')
    return s.isdigit() and len(s) == 5


def _hk_gap(code: str, category: str) -> Dict[str, Any]:
    """港股：公开接口无分部数据 → 输出可执行缺口，而非「暂未包含」。"""
    return {
        'ok': False, 'market': 'hk', 'code': code, 'category': category,
        'period': None, 'segments': [], 'provenance': [],
        'gap': {
            'reason': '公开数据接口不提供港股分部（主营构成）数据',
            'source': HK_SEGMENT_SOURCE,
            'link': HK_SEGMENT_LINK,
            'hint': HK_SEGMENT_HINT,
            'fields': list(SEGMENT_FIELDS),
            'next_actions': [
                f'查 {HK_SEGMENT_SOURCE}',
                '按上表字段（收入/同比/占比/毛利率/经营利润）整理后回填，并注明报告期与页码',
                '若需自动取数，可接入付费终端（Wind/Choice）的分部数据接口',
            ],
        },
    }


def _fetch_a(code: str, category: str, limit: int) -> Dict[str, Any]:
    try:
        import akshare as ak
    except ImportError:
        return {'ok': False, 'market': 'a', 'code': code, 'category': category,
                'period': None, 'segments': [], 'provenance': [],
                'gap': {'reason': 'akshare 未安装，无法取分部数据'}}
    sym = _a_symbol(code)
    try:
        df = ak.stock_zygc_em(symbol=sym)
    except Exception as e:
        logger.debug('取分部数据失败 %s: %s', sym, e)
        return {'ok': False, 'market': 'a', 'code': code, 'category': category,
                'period': None, 'segments': [], 'provenance': [],
                'gap': {'reason': f'接口调用失败：{type(e).__name__}'}}
    if df is None or df.empty:
        return {'ok': False, 'market': 'a', 'code': code, 'category': category,
                'period': None, 'segments': [], 'provenance': [],
                'gap': {'reason': '接口返回空数据'}}

    sub = df[df['分类类型'].astype(str) == category]
    if sub.empty:                       # 退回可用分类，避免直接空手而归
        category = str(df['分类类型'].iloc[0])
        sub = df[df['分类类型'].astype(str) == category]
    sub = sub.copy()
    sub['报告日期'] = sub['报告日期'].astype(str).str[:10]
    period = sub['报告日期'].max()
    cur = sub[sub['报告日期'] == period]
    prev = sub[sub['报告日期'] == _shift_year(period)]

    prev_map = {str(r['主营构成']): _num(r.get('主营收入')) for _, r in prev.iterrows()}
    segments: List[Dict[str, Any]] = []
    for _, r in cur.iterrows():
        name = str(r.get('主营构成') or '').strip()
        rev = _num(r.get('主营收入'))
        base = prev_map.get(name)
        segments.append({
            'name': name,
            'revenue': round(rev / 1e8, 2),                      # 亿元
            'yoy': (round((rev - base) / base * 100, 2)
                    if base and base > 0 else None),             # 去年同期不可比则为 None
            'share': round(_num(r.get('收入比例')) * 100, 2),      # %
            'gross_margin': round(_num(r.get('毛利率')) * 100, 2),
            'profit': round(_num(r.get('主营利润')) / 1e8, 2),     # 毛利（亿元）
        })
    segments.sort(key=lambda x: x['revenue'], reverse=True)
    for s in segments:
        if s['gross_margin'] == 0 and s['profit'] == 0:
            s['gross_margin'] = None
    segments = segments[:limit]
    prov = [stamp('东方财富 主营构成', metric=f'分部数据（{category}）', period=period,
                  link=f'https://emweb.securities.eastmoney.com/PC_HSF10/BusinessAnalysis/Index?type=web&code={sym}',
                  note='经营利润未在接口披露，此处利润列为毛利')]
    return {'ok': True, 'market': 'a', 'code': code, 'category': category,
            'period': period, 'segments': segments, 'provenance': prov, 'gap': None,
            'note': '接口未披露分部「经营利润」，表中利润列为分部毛利；'
                    '经营利润需查年报分部附注。'}


def _a_symbol(code: str) -> str:
    s = str(code or '').strip().upper()
    s = s.replace('SH', '').replace('SZ', '').replace('.SH', '').replace('.SZ', '')
    return ('SH' if s.startswith('6') else 'SZ') + s


def _shift_year(period: str) -> str:
    """2026-06-30 → 2025-06-30（取去年同期，保证同比可比）。"""
    try:
        y, rest = period.split('-', 1)
        return f'{int(y) - 1}-{rest}'
    except (ValueError, AttributeError):
        return ''


def render_segments(data: Dict[str, Any]) -> str:
    """渲染为 Markdown。取不到时输出**可执行缺口**，不输出「暂未包含」。"""
    if not data:
        return ''
    if not data.get('ok'):
        gap = data.get('gap') or {}
        lines = [f"\n#### 🧩 分部数据（{str(data.get('category') or '按产品')}）：未能自动获取",
                 f"- **原因**：{gap.get('reason', '未知')}"]
        if gap.get('source'):
            lines.append(f"- **应查来源**：{gap['source']}")
        if gap.get('link'):
            lines.append(f"- **入口**：{gap['link']}")
        if gap.get('hint'):
            lines.append(f"- **定位提示**：{gap['hint']}")
        if gap.get('fields'):
            lines.append(f"- **期望字段**：{' / '.join(gap['fields'])}")
        for act in gap.get('next_actions') or []:
            lines.append(f"  - {act}")
        return '\n'.join(lines)

    lines = [f"\n#### 🧩 分部数据（{data.get('category')}）",
             f"报告期：{data.get('period')}　单位：亿元", '',
             '| 分部 | 收入 | 同比 | 占比 | 毛利率 | 毛利 |',
             '| --- | --- | --- | --- | --- | --- |']
    for s in data.get('segments') or []:
        lines.append('| {n} | {r} | {y} | {sh} | {gm} | {p} |'.format(
            n=s['name'], r=_pct(s['revenue'], raw=True),
            y=_pct(s['yoy']), sh=_pct(s['share']), gm=_pct(s['gross_margin']),
            p=_pct(s['profit'], raw=True)))
    if data.get('note'):
        lines += ['', f"> 口径说明：{data['note']}"]
    return '\n'.join(lines)


def _pct(v, raw: bool = False) -> str:
    if v is None:
        return '未披露'
    return f'{v}' if raw else f'{v}%'
