# -*- coding: utf-8 -*-
"""净利润变动归因：**净利下滑必须给原因**，取不到就明说去哪找。

为什么需要它
------------
只报告"净利同比 −35%"是不合格的。看报告的人一定会问"**为什么**"。
常见的四个驱动因素，并且都是**能从财报里查证**的：

| 维度 | 典型情形 |
|---|---|
| 财务费用 | 有息负债上升、利率上行 → 利息支出吃掉利润 |
| 汇兑损益 | 出口/海外收入占比高，本币升值 → 汇兑损失 |
| 资产减值 | 存货跌价、应收账款坏账、商誉减值 |
| 海外子公司 | 海外业务单独拖累或贡献（需分地区收入/利润） |

设计原则
--------
1. **绝不编数字**：取不到就如实说"已尝试 A/B 工具，未获取"，并给出**可执行的下一步**
   （查哪份报告的哪一节）。**页码只在能真实定位时给**，否则标"页码待核对"——
   编一个"第 12 页"比不说更糟。
2. **可注入取数**：默认从传入的快照/三表里扫关键词；调用方可注入更强的 fetcher。
3. **零项目依赖**（仅标准库）。

供 ``company_deep_*`` 与 AI 助手在"净利异常波动"时调用。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# ==================== 归因维度 ====================

# 每个维度：怎么在家里找、找不到去哪找
DIMENSIONS: Dict[str, Dict[str, Any]] = {
    'finance_expense': {
        'name': '财务费用',
        'keywords': ('财务费用', '利息支出', '利息费用', '利息净支出', 'Finance cost',
                     'Finance expenses', 'Interest expense'),
        'why': '有息负债规模与融资成本变化会直接吃掉利润',
        'where': '利润表「财务费用」及附注「财务费用明细」',
        'section': '财务报表附注·财务费用',
    },
    'fx_gain_loss': {
        'name': '汇兑损益',
        'keywords': ('汇兑', '汇兑损益', '汇兑损失', '汇兑收益', 'Exchange gain',
                     'Exchange loss', 'foreign exchange'),
        'why': '出口或海外收入占比高的公司，汇率波动会形成汇兑损益',
        'where': '利润表「财务费用」明细中的汇兑损益，或附注「外币折算」',
        'section': '财务报表附注·汇兑损益 / 外币折算',
    },
    'impairment': {
        'name': '资产减值',
        'keywords': ('资产减值', '减值损失', '信用减值', '存货跌价', '坏账', '商誉减值',
                     'Impairment', 'Provision for impairment', 'write-down'),
        'why': '存货跌价、应收坏账与商誉减值都是一次性但影响巨大的利润项',
        'where': '利润表「资产减值损失」「信用减值损失」及附注',
        'section': '财务报表附注·资产减值准备明细',
    },
    'overseas': {
        'name': '海外子公司/境外业务',
        'keywords': ('境外', '海外', '国外', '分地区', 'Overseas', 'International',
                     'Foreign operations', 'Geographic'),
        'why': '境外业务可能单独拖累（关税、运费、当地需求）或成为主要增量',
        'where': '年报「分地区收入/利润」分部资料，或「主要子公司」清单',
        'section': '分部资料·分地区 / 主要子公司',
    },
}

# 触发归因的阈值（净利同比绝对值，%）
ATTRIBUTION_THRESHOLD = 15.0

# MD&A 要点抽取的关键词
MDNA_KEYWORDS = ('净利', '净利润', '归属于', '下滑', '下降', '减少', '亏损', '同比',
                 '毛利', '费用', '减值', '汇兑', '原材料', '价格', '销量', '产能',
                 '关税', '海外', '境外', '投产', '扩产')


# ==================== 诊断 ====================


def diagnose(snapshot: Dict[str, Any],
              threshold: float = ATTRIBUTION_THRESHOLD) -> Dict[str, Any]:
    """判断是否需要归因。``snapshot`` 需含净利同比（%），**支持嵌套结构**。

    净利同比在真实上下文里常嵌在 ``financial_analysis.ratios.growth.profit_growth``
    这类深层路径下，所以这里做递归查找（而非只读顶层键）——
    否则会因"读不到同比"而**每次都触发归因**。

    返回 ``{'needed','direction','change_pct','reason'}``。
    """
    snap = snapshot or {}
    g = _f(snap.get('profit_growth'))
    if g is None:
        hit = _find_by_keywords(snap, ('profit_growth', '净利润同比', '净利同比',
                                       '归母净利润同比', 'profit growth'))
        if hit:
            g = _f(hit[1])
    if g is None:
        return {'needed': True, 'direction': 'unknown', 'change_pct': None,
                'reason': '缺少净利同比数据，无法判断变动方向，仍需说明数据来源'}
    if abs(g) < threshold:
        return {'needed': False, 'direction': 'stable', 'change_pct': round(g, 2),
                'reason': f'净利同比 {g:+.2f}%，波动在 ±{threshold:.0f}% 以内，暂不需要专项归因'}
    return {'needed': True, 'direction': 'down' if g < 0 else 'up',
            'change_pct': round(g, 2),
            'reason': f'净利同比 {g:+.2f}%，超出 ±{threshold:.0f}%，必须给出原因'}


# ==================== 取数 ====================


def collect(snapshot: Dict[str, Any], *, market: str = 'a',
            fetcher: Optional[Callable[..., Dict[str, Any]]] = None) -> Dict[str, Any]:
    """逐个维度尝试取数。

    ``fetcher(key, snapshot, market)`` 返回
    ``{'ok':bool,'value':Any,'unit':str,'source':str,'page':int|None,'error':str}``。
    未注入时使用内置的**快照关键词扫描**（从 statements / 任意嵌套字段里找）。
    """
    hits: List[Dict[str, Any]] = []
    gaps: List[Dict[str, Any]] = []
    for key, spec in DIMENSIONS.items():
        tried = [fetcher.__name__ if fetcher else 'snapshot_scan']
        res: Dict[str, Any] = {}
        try:
            res = (fetcher(key, snapshot, market) if fetcher
                   else _scan_snapshot(key, snapshot, market)) or {}
        except Exception as e:              # 取数异常如实记录，不中断其余维度
            logger.debug('归因维度 %s 取数失败: %s', key, e)
            res = {'ok': False, 'error': f'{type(e).__name__}: {e}'}
        if res.get('ok') and res.get('value') not in (None, ''):
            hits.append({
                'key': key, 'name': spec['name'], 'value': res.get('value'),
                'unit': res.get('unit') or '', 'source': res.get('source') or '工具返回',
                'page': res.get('page'), 'why': spec['why'],
                'text': _hit_text(spec, res),
            })
        else:
            gaps.append({
                'key': key, 'name': spec['name'],
                'attempted': res.get('attempted') or tried,
                'error': res.get('error') or '未在现有数据中找到该科目',
                'where': spec['where'], 'section': spec['section'],
                'hint': _gap_hint(spec, res),
            })
    return {'hits': hits, 'gaps': gaps,
            'ok': bool(hits), 'coverage': round(len(hits) / len(DIMENSIONS), 2)}


def _scan_snapshot(key: str, snapshot: Dict[str, Any], market: str) -> Dict[str, Any]:
    """内置兜底：在快照（含三表）里按关键词找科目，命中即返回原值。"""
    spec = DIMENSIONS[key]
    found = _find_by_keywords(snapshot, spec['keywords'])
    if found is None:
        return {'ok': False, 'attempted': ['snapshot_scan'],
                'error': f"快照与三表中未找到含「{'/'.join(spec['keywords'][:3])}」的科目"}
    label, value = found
    return {'ok': True, 'value': value, 'source': f'财报数据（{label}）',
            'attempted': ['snapshot_scan']}


def _find_by_keywords(obj: Any, keywords: tuple, _depth: int = 0) -> Optional[tuple]:
    """递归查 dict/list，返回 (命中的键名, 值)。深度上限防环。"""
    if _depth > 6:
        return None
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (int, float, str)) and v not in (None, ''):
                if any(kw.lower() in str(k).lower() for kw in keywords):
                    return str(k), v
            if isinstance(v, (dict, list)):
                hit = _find_by_keywords(v, keywords, _depth + 1)
                if hit:
                    return hit
    elif isinstance(obj, list):
        for it in obj[:50]:
            hit = _find_by_keywords(it, keywords, _depth + 1)
            if hit:
                return hit
    return None


# ==================== 渲染 ====================


def render(result: Dict[str, Any], diag: Optional[Dict[str, Any]] = None) -> str:
    """把归因结果渲染成可读文本（含缺口与**可执行**的下一步）。"""
    lines: List[str] = []
    if diag:
        lines.append(f"**净利变动诊断**：{diag.get('reason')}")
    hits = result.get('hits') or []
    gaps = result.get('gaps') or []
    if hits:
        lines += ['', '**已查证的影响项**', '']
        for h in hits:
            page = f"，第 {h['page']} 页" if h.get('page') else ''
            lines.append(f"- **{h['name']}**：{h['value']}{h.get('unit') or ''}　"
                         f"（来源：{h['source']}{page}）—— {h['why']}")
    if gaps:
        lines += ['', '**未获取的影响项（已尝试，未取到）**', '']
        for g in gaps:
            tried = '、'.join(g.get('attempted') or []) or '默认取数'
            lines.append(f"- **{g['name']}**：已尝试 `{tried}`，未获取（{g['error']}）。"
                         f"建议查：{g['where']}。{g['hint']}")
    if not hits and not gaps:
        lines.append('（未执行归因）')
    elif not hits:
        lines += ['', '> ⚠️ 四个常见驱动因素**均未从现有数据中取到**。']
        lines.append('> 这不代表没有影响，而是**当前工具没有拉到对应科目**。'
                     '在补齐之前，**不得对净利变动给出因果结论**，只能列为「待补充」。')
    return '\n'.join(lines)


def _hit_text(spec: Dict[str, Any], res: Dict[str, Any]) -> str:
    return f"{spec['name']} {res.get('value')}{res.get('unit') or ''}"


def _gap_hint(spec: Dict[str, Any], res: Dict[str, Any]) -> str:
    """缺口提示：有真实页码就给页码，否则明确标"页码待核对"。"""
    if res.get('page'):
        return f"（定位到第 {res['page']} 页）"
    return f"（建议查「{spec['section']}」；页码需按当期报告目录核对，系统不臆测页码）"


# ==================== MD&A 要点（P2 支撑） ====================


def extract_mdna_points(text: str, *, limit: int = 8) -> List[Dict[str, Any]]:
    """从财报 MD&A / 公告正文里抽取与净利变动相关的**原句**（不做改写）。

    返回 ``[{'quote','keyword','page'}]``；``page`` 仅在文本带页标记时给出。
    """
    body = str(text or '')
    if not body:
        return []
    out: List[Dict[str, Any]] = []
    for raw in re.split(r'[。；\n]', body):
        sent = raw.strip()
        if len(sent) < 8:
            continue
        hit = [k for k in MDNA_KEYWORDS if k in sent]
        if len(hit) < 2:                     # 同时命中两个词才算"在讲变动原因"
            continue
        page = None
        m = re.search(r'第\s*(\d+)\s*页', sent)
        if m:
            page = int(m.group(1))
        out.append({'quote': sent[:200], 'keyword': hit[0], 'page': page})
        if len(out) >= limit:
            break
    return out


def render_mdna(points: List[Dict[str, Any]]) -> str:
    if not points:
        return ('**管理层讨论（MD&A）**：未从已获取文本中提取到与净利变动相关的原句。'
                '建议查年报「管理层讨论与分析 → 主要经营情况分析」，'
                '或调用信源工具获取 MD&A 原文。（页码需按当期报告目录核对）')
    lines = ['**管理层讨论（MD&A）原句摘录**（原文引用，未改写）', '']
    for p in points:
        page = f"（第 {p['page']} 页）" if p.get('page') else ''
        lines.append(f"- 「{p['quote']}」{page}")
    return '\n'.join(lines)


def analyze(snapshot: Dict[str, Any], *, market: str = 'a',
            fetcher: Optional[Callable[..., Dict[str, Any]]] = None,
            mdna_text: str = '') -> Dict[str, Any]:
    """一站式：诊断 → 取数 → 渲染。返回含 ``text`` 的结果字典。"""
    diag = diagnose(snapshot)
    out: Dict[str, Any] = {'diagnosis': diag}
    if not diag['needed']:
        out.update({'hits': [], 'gaps': [], 'text': render({}, diag),
                    'verdict': 'not_needed'})
        return out
    res = collect(snapshot, market=market, fetcher=fetcher)
    mdna = extract_mdna_points(mdna_text) if mdna_text else []
    text = render(res, diag)
    if mdna:
        text += '\n\n' + render_mdna(mdna)
    out.update(res)
    out['mdna_points'] = mdna
    out['text'] = text
    # 结论口径：无一命中 → 只能待补充；有命中 → 可给方向性描述
    out['verdict'] = 'attributed' if res['hits'] else 'pending_supplement'
    if out['verdict'] == 'pending_supplement':
        text += ('\n\n> 结论口径：**归因待补充**——四个驱动因素均未取到，'
                 '不得写成"净利下滑是因为 X"。')
        out['text'] = text
    return out


def _f(v: Any) -> Optional[float]:
    try:
        return None if v in (None, '') else float(v)
    except (TypeError, ValueError):
        return None
