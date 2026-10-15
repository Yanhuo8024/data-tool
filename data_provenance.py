# -*- coding: utf-8 -*-
"""data_provenance.py — 数据来源分级与溯源戳（每个数字都要能回答"你哪来的"）

## 为什么需要（2026-10-08 用户反馈）

报告里数据来源只写「financial_ratios / web_search / company_analysis」——
这是**内部函数名**，不是数据来源，用户既无法核对也无法判断可信度。

按要求改为：**数据源优先级 + 每个数字带来源名称、报告期、链接/页码、抓取时间**。

    交易所公告 > 公司 IR 财报 > 权威金融终端 > 公开新闻 > 百科

本模块零项目依赖（仅标准库），`stamp()` 产出统一的溯源戳，`render()` 渲染成可读清单。
"""
import datetime
import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 数据源优先级：数字越小越权威。命中即取最高优先级。
SOURCE_TIERS: Tuple[Tuple[int, str, Tuple[str, ...]], ...] = (
    (1, '交易所公告', ('交易所', '公告', '港交所', '披露易', 'hkexnews', '巨潮',
                   '上交所', '深交所', 'cninfo', 'sec.gov', '招股书')),
    (2, '公司IR财报', ('公司ir', '投资者关系', 'ir.', '年报', '中报', '季报',
                   '财报原文', '业绩公告', 'earnings', 'annual report')),
    (3, '权威金融终端', ('akshare', '东财', '东方财富', '同花顺', '新浪财经', '雪球',
                    'wind', 'choice', 'baostock', 'tushare', '理杏仁', 'financial_ratios',
                    'company_analysis', 'finance_data')),
    (4, '公开新闻', ('news', '新闻', '媒体', '快讯', 'web_search', '报道', '访谈')),
    (5, '百科及其他', ('百科', '维基', 'baike', 'wikipedia', '知乎', '博客', '论坛')),
)
TIER_LABEL = {t[0]: t[1] for t in SOURCE_TIERS}
_UNKNOWN_TIER = 6


def classify(source: str) -> Tuple[int, str]:
    """把来源名称映射为 (优先级, 层级名称)。无法识别归入最低优先级。"""
    s = str(source or '').strip().lower()
    if not s:
        return _UNKNOWN_TIER, '来源未标注'
    for tier, label, keys in SOURCE_TIERS:
        if any(k in s for k in keys):
            return tier, label
    return _UNKNOWN_TIER, '未识别来源'


def now_iso() -> str:
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def stamp(source: str, *, value: Any = None, metric: str = '',
          period: str = None, link: str = None, page: str = None,
          fetched_at: str = None, note: str = None) -> Dict[str, Any]:
    """为一个数字生成溯源戳。

    字段对齐用户要求：来源名称 / 报告期 / 页码或链接 / 抓取时间
    （外加 metric、value、tier 便于程序化校验与排序）。
    """
    tier, label = classify(source)
    return {
        'metric': metric,
        'value': value,
        'source': str(source or '未标注'),
        'source_tier': tier,
        'source_label': label,
        'period': period or '未标注',          # 报告期（如 2025-12-31）
        'link': link or page or '未标注',      # 链接或页码
        'fetched_at': fetched_at or now_iso(),
        'note': note,
    }


def worst_tier(stamps: Iterable[Dict[str, Any]]) -> int:
    """最差（数值最大）的优先级 —— 用于判定整体可信度。"""
    ts = [s.get('source_tier', _UNKNOWN_TIER) for s in (stamps or []) if s]
    return max(ts) if ts else _UNKNOWN_TIER


def is_authoritative(stamps: Iterable[Dict[str, Any]], max_tier: int = 3) -> bool:
    """是否**全部**数字都来自前 max_tier 级来源。"""
    ts = [s.get('source_tier', _UNKNOWN_TIER) for s in (stamps or []) if s]
    return bool(ts) and all(t <= max_tier for t in ts)


def coverage(stamps: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """溯源完整度：有多少数字带了来源/报告期/链接。"""
    items = [s for s in (stamps or []) if s]
    n = len(items)
    if not n:
        return {'total': 0, 'complete': 0, 'ratio': 0.0, 'missing': []}
    missing = []
    for s in items:
        lack = [k for k in ('source', 'period', 'link') if str(s.get(k) or '未标注') == '未标注']
        if lack:
            missing.append({'metric': s.get('metric') or '?', 'lack': lack})
    return {'total': n, 'complete': n - len(missing),
            'ratio': round((n - len(missing)) / n, 3), 'missing': missing}


def render(trade_stamps: List[Dict[str, Any]], title: str = '数据来源与口径') -> str:
    """渲染为 Markdown 清单。无溯源信息时返回空串（不产出空标题）。"""
    items = [s for s in (trade_stamps or []) if s]
    if not items:
        return ''
    items = sorted(items, key=lambda s: (s.get('source_tier', _UNKNOWN_TIER), s.get('metric') or ''))
    lines = [f'\n#### 📎 {title}', '', '| 指标 | 数值 | 来源 | 层级 | 报告期 | 链接/页码 | 抓取时间 |',
             '| --- | --- | --- | --- | --- | --- | --- |']
    for s in items:
        lines.append('| {m} | {v} | {s} | {t} | {p} | {l} | {f} |'.format(
            m=s.get('metric') or '—', v=_fmt(s.get('value')),
            s=s.get('source'), t=s.get('source_label'),
            p=s.get('period'), l=s.get('link'), f=s.get('fetched_at')))
    cov = coverage(items)
    if cov['missing']:
        lines.append('')
        lines.append(f'> ⚠️ 溯源完整度 {cov["complete"]}/{cov["total"]}，'
                     f'以下指标缺来源/报告期/链接：'
                     + '；'.join(f"{m['metric']}（缺 {'、'.join(m['lack'])}）"
                                 for m in cov['missing'][:5]))
    return '\n'.join(lines)


def _fmt(v: Any) -> str:
    if v is None:
        return '—'
    if isinstance(v, float):
        return f'{v:,.2f}'
    return str(v)
