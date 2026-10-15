# -*- coding: utf-8 -*-
"""证据等级（A/B/C/D）与关键数字的工具口径优先级。

## 为什么需要（2026-10-09 用户要求）

核验表原来只说「这个数来自哪个工具」，但**来源的权威性**没有区分：
一条财报原文的数字和一条百科的数字，在表里长得一模一样，读者无从判断轻重。

用户给出的分级：

    A = 财报原文 / 数据接口      B = 权威金融终端
    C = 公开新闻                D = 百科及其他

并且规定：**C/D 级数字不得支撑核心结论**。若核心结论依赖 C/D 级来源，
必须自动标记「结论置信度低」，让人一眼看出结论的地基是软的。

另一件事是**取数口径的优先级**（同一个数字往往多个工具都能取到，该用哪个）：

| 数字 | 首选工具 |
|---|---|
| 分部收入 / 收入结构 | `segment_breakdown` |
| 财务比率（ROE / 毛利率 / 净利率 / 负债率） | `financial_ratios` |
| 资产负债表 / 利润表 / 现金流量表科目 | `financial_statements` |
| 可比估值（PE / PB / 行业倍数） | `stock_valuation` |
| 新闻、政策、行业动态 | `web_search` —— **仅作补充，不作首选** |

与 ``data_provenance`` 是**互补关系**：后者答「来源有多权威（1–5 级）」，
本模块答「该用哪个工具取（口径优先级）+ 这个数够不够格支撑结论（A–D 级 + 闸门）」。

零项目依赖（仅标准库）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# ==================== ① 证据等级 ====================

GRADES: Tuple[str, ...] = ('A', 'B', 'C', 'D')
UNGRADED = 'U'
GRADE_MEANING: Dict[str, str] = {
    'A': '财报原文 / 数据接口',
    'B': '权威金融终端',
    'C': '公开新闻',
    'D': '百科及其他',
    'U': '来源未标注',
}
GRADE_RANK: Dict[str, int] = {'A': 1, 'B': 2, 'C': 3, 'D': 4, 'U': 5}

# 只有 A/B 级可以支撑核心结论（用户口径：C/D 级不得支撑核心结论）
SUPPORTING_GRADES: Tuple[str, ...] = ('A', 'B')

# 工具名 → 等级。**先精确查这张表**，避免「公告解读」被「公告」误判为 A 这类问题。
TOOL_GRADE: Dict[str, str] = {
    # 直接取财报字段的接口（用户口径：接口 = A）
    'segment_breakdown': 'A',
    'financial_statements': 'A',
    'financial_ratios': 'A',
    'segment_data': 'A',
    'financial_store': 'A',
    # 终端行情 / 加工后的估值口径
    'stock_valuation': 'B',
    'company_analysis': 'B',
    'company_deep_analyze': 'B',
    'finance_data': 'B',
    'market_snapshot': 'B',
    'duckdb_engine': 'B',
    'db_query': 'B',
    # 新闻与检索
    'web_search': 'C',
    # 站内知识库 / 非权威来源
    'file_rag': 'D',
}

# 关键词兜底（按顺序匹配，第一个命中者胜）
# 🚨 D 的**降级强特征**单独前置：「媒体报道（转载）」不能因含「媒体」而判 C
#   —— 转载/自媒体/传闻是二手信息，用户口径明确归 D。
# P0（2026-10-10）：抖音/搜狗/今日头条/百度搜索 → D，不得支撑行业结论
_D_STRONG: Tuple[str, ...] = ('转载', '自媒体', '传闻', '网传', '百科', '维基', 'baike',
                              'wikipedia', '知乎', '公众号', '论坛', '博客',
                              '抖音', '搜狗', '今日头条', '百度搜索')
_GRADE_KEYWORDS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ('A', ('交易所', '港交所', '披露易', 'hkexnews', '巨潮', 'cninfo', 'sec.gov',
           '上交所', '深交所', '招股书', '年报', '中报', '季报', '半年报',
           '公司ir', '投资者关系', 'ir.', 'earnings', 'annual report', '财报原文')),
    # P0（2026-10-10）：行业权威统计机构 = A（中汽协/乘联会/海关总署）
    ('A', ('中汽协', '乘联会', '海关总署', 'caam', 'cpca', '国家统计局', '工信部')),
    ('B', ('akshare', '东财', '东方财富', '同花顺', '新浪财经', '雪球', 'wind', 'choice',
           'baostock', 'tushare', '理杏仁', '金融终端', '行情')),
    ('C', ('news', '新闻', '媒体', '快讯', 'web_search', '报道', '访谈', '资讯')),
    ('D', ('百科', '维基', 'baike', 'wikipedia', '知乎', '博客', '论坛', '公众号',
           '转载', '自媒体', '传闻', '抖音', '搜狗', '今日头条', '百度搜索')),
)


def grade_of(source: str, *hints: Optional[str]) -> Dict[str, Any]:
    """判定来源的证据等级。判不出返回 ``U``（**不臆测**）。

    返回 ``{'grade','meaning','rank','supports_core','matched'}``。
    """
    blob = ' '.join(str(x).strip().lower() for x in ((source,) + tuple(hints or ())) if x)
    grade: Optional[str] = None
    matched = ''
    if blob:
        for tool, g in TOOL_GRADE.items():          # ① 工具名精确优先
            if tool.lower() in blob:
                grade, matched = g, tool
                break
        if grade is None:                            # ② D 强特征（转载/自媒体等二手信息）
            if any(k in blob for k in _D_STRONG):
                grade, matched = 'D', '二手信息强特征'
        if grade is None:                            # ③ 常规关键词兜底
            for g, keys in _GRADE_KEYWORDS:
                hit = next((k for k in keys if k in blob), None)
                if hit:
                    grade, matched = g, hit
                    break
    grade = grade or UNGRADED
    return {
        'grade': grade,
        'meaning': GRADE_MEANING[grade],
        'rank': GRADE_RANK[grade],
        'supports_core': grade in SUPPORTING_GRADES,
        'matched': matched,
    }


# ==================== ② 工具口径优先级 ====================

FALLBACK_TOOL = 'web_search'
SUPPLEMENT_ONLY: Tuple[str, ...] = ('web_search', 'file_rag')

# (口径名, 该口径的关键词, 首选工具)
METRIC_TOOLS: Tuple[Tuple[str, Tuple[str, ...], str], ...] = (
    ('分部收入', ('分部', '收入结构', '收入占比', '业务拆分', '主营构成', '分业务', '按产品', '按地区'),
     'segment_breakdown'),
    # P0（2026-10-10 第四批）：'同比/环比' 从财务比率移除——它们是修饰词不是口径，
    # 「营收同比」会同时命中两个口径 → 歧义不猜 → 漏报。修饰词留在报表科目侧。
    ('财务比率', ('roe', 'roa', '毛利率', '净利率', '负债率', '资产负债率', '周转率',
              '比率'), 'financial_ratios'),
    # P0（2026-10-10）：上市公司核心财务数据（营收/净利润/研发投入）必须走接口，
    # web_search 不得用于上市公司核心财务数据（比亚迪 002594 / 长城 601633 / 吉利 HK0175…）
    ('报表科目', ('资产负债表', '利润表', '现金流量表', '总资产', '净资产', '权益',
              '存货', '应收账款', '商誉', '经营现金流', '资本开支', '有息负债',
              '营收', '营业收入', '净利润', '研发投入', '研发费用', '营业利润',
              '扣非', '毛利润', '毛利额'), 'financial_statements'),
    ('可比估值', ('pe', 'pb', 'ps', 'ev', '估值', '可比', '倍数', '市值'), 'stock_valuation'),
)


def preferred_tool(metric: str) -> Optional[str]:
    """给定指标名，返回**首选**取数工具。

    🚨 若描述里**同时命中多个口径**（说明传入的是段落而非单一指标名），返回 ``None``
    —— 歧义时**不猜**，否则会给出误导性的工具建议
    （实测踩过：核验表里 quote 含「毛利率」，导致「8 万辆销量」被建议改用 `financial_ratios`）。
    """
    blob = str(metric or '').strip().lower()
    if not blob:
        return None
    hits = [tool for _n, keys, tool in METRIC_TOOLS if any(k in blob for k in keys)]
    hits = list(dict.fromkeys(hits))
    return hits[0] if len(hits) == 1 else None


def resolve_tool(metric: str, used: Optional[str] = None) -> Dict[str, Any]:
    """校验「实际用的工具」是否是该口径的首选。

    返回 ``{'metric','preferred','used','ok','level','note'}``；
    ``level`` ∈ ``{'ok','fallback','mismatch','unknown'}``。
    """
    pref = preferred_tool(metric)
    u = str(used or '').strip()
    out = {'metric': str(metric or ''), 'preferred': pref, 'used': u,
           'ok': False, 'level': 'unknown', 'note': ''}
    if pref is None:
        out['note'] = ('该口径未登记首选工具（或描述同时命中多个口径），不参与校验'
                       '——歧义时不猜')
        return out
    if not u:
        out['note'] = f'未标注取数工具；该口径应优先用 `{pref}`'
        return out
    if u == pref:
        out['ok'] = True
        out['level'] = 'ok'
        out['note'] = f'口径正确：`{pref}`'
    elif u in SUPPLEMENT_ONLY:
        out['level'] = 'fallback'
        out['note'] = (f'⚠️ 用 `{u}` 取「{metric}」，但该口径应先查 `{pref}`'
                       f'（`{u}` 仅作补充）')
    else:
        out['level'] = 'mismatch'
        out['note'] = f'⚠️ 「{metric}」首选 `{pref}`，实际用了 `{u}`，口径可能不一致'
    return out


# ==================== ③ 证据审计 + 核心结论闸门 ====================


def audit(rows: Iterable[Dict[str, Any]], *,
          core_values: Optional[Iterable[Any]] = None,
          core: bool = True) -> Dict[str, Any]:
    """给每行数据打上证据等级，并判定**核心结论能否成立**。

    ``rows`` 每项至少含 ``metric`` / ``value`` / ``source``（可选 ``tool``）。
    ``core_values``：被核心结论引用的数字集合；为 None 时按 ``core`` 参数决定
    （``core=True`` 表示所有行都是核心证据）。
    """
    items = [r for r in (rows or []) if r]
    core_set = {str(v).strip() for v in (core_values or []) if str(v).strip()}

    audited: List[Dict[str, Any]] = []
    core_weak: List[Dict[str, Any]] = []
    mismatched: List[str] = []
    violations: List[Dict[str, Any]] = []          # P0-4：结构化违规清单
    for r in items:
        src = r.get('source') or r.get('tool') or ''
        g = grade_of(src, r.get('tool'), r.get('link'), r.get('note'))
        val = str(r.get('value')).strip() if r.get('value') is not None else ''
        is_core = (val in core_set) if core_set else bool(core)
        tool_check = resolve_tool(r.get('metric') or '', r.get('tool') or src)
        # P0（2026-10-10）：违规**全量收集**，不再限 is_core——
        # 「1 处未走首选工具」实际至少 3~5 处的漏报就是这么来的。
        if tool_check.get('level') in ('fallback', 'mismatch'):
            mismatched.append(tool_check['note'])
            violations.append({'metric': tool_check.get('metric') or '',
                               'value': str(r.get('value') or ''),
                               'preferred': tool_check.get('preferred') or '',
                               'used': tool_check.get('used') or '',
                               'level': tool_check.get('level') or ''})
        row = dict(r)
        row.update({
            'grade': g['grade'], 'grade_meaning': g['meaning'],
            'supports_core': g['supports_core'], 'is_core': is_core,
            'tool_check': tool_check,
        })
        audited.append(row)
        if is_core and not g['supports_core']:
            core_weak.append(row)

    core_rows = [r for r in audited if r['is_core']]
    issues: List[str] = []
    if not core_rows:
        confidence, verdict = 'unknown', '未识别到核心证据，无法判定'
    elif not core_weak:
        confidence, verdict = 'ok', '核心结论证据等级达标（全部 A/B 级）'
    elif len(core_weak) == len(core_rows):
        confidence = 'invalid'
        verdict = (f'❌ 核心结论**只依赖 C/D 级来源**（{len(core_weak)} 处），'
                   f'结论置信度低，不得作为方向性依据，请先补 A/B 级来源')
        issues.append('核心证据全部为 C/D 级')
    else:
        confidence = 'low'
        verdict = (f'⚠️ 核心结论中有 {len(core_weak)}/{len(core_rows)} 处依赖 C/D 级来源，'
                   f'结论置信度低，建议补权威来源')
        issues.append(f'{len(core_weak)} 处核心数字为 C/D 级')
    if mismatched:
        issues.append(f'{len(mismatched)} 处取数口径未走首选工具')

    return {
        'rows': audited, 'core_weak': core_weak,
        'core_count': len(core_rows), 'core_weak_count': len(core_weak),
        'confidence': confidence, 'verdict_cn': verdict,
        'tool_mismatch': mismatched, 'violations': violations, 'issues': issues,
        'table_md': render_table(audited),
    }


# ==================== 渲染 ====================


def render_table(rows: Sequence[Dict[str, Any]], *,
                 title: str = '数据来源与证据等级') -> str:
    """渲染带**证据等级列**的核验表。"""
    items = [r for r in (rows or []) if r]
    if not items:
        return ''
    lines = [f'**{title}**', '',
             '| 指标 | 数值 | 来源 | 证据等级 | 报告期 | 链接/页码 |',
             '| --- | --- | --- | --- | --- | --- |']
    for r in items:
        g = r.get('grade') or grade_or_ungraded(r)
        grade_cell = f'**{g}**（{GRADE_MEANING.get(g, "")}）' if g in GRADES else str(g)
        if r.get('is_core') and not r.get('supports_core', True):
            grade_cell += ' ⚠️'
        lines.append('| {m} | {v} | {s} | {g} | {p} | {l} |'.format(
            m=_cell(r.get('metric')), v=_cell(r.get('value')), s=_cell(r.get('source')),
            g=grade_cell, p=_cell(r.get('period') or '未标注'),
            l=_cell(r.get('link') or '未标注')))
    lines += ['', f'> 等级口径：**A** {GRADE_MEANING["A"]} · **B** {GRADE_MEANING["B"]} · '
                  f'**C** {GRADE_MEANING["C"]} · **D** {GRADE_MEANING["D"]}。'
                  f'其中 **C/D 级数字不得支撑核心结论**。']
    return '\n'.join(lines)


def grade_or_ungraded(row: Dict[str, Any]) -> str:
    """行内没有等级时按来源即时判定（渲染兜底）。"""
    return grade_of(row.get('source') or row.get('tool') or '')['grade']


def render_verdict(result: Dict[str, Any]) -> str:
    """渲染审计结论（置信度 + 问题清单 + 待补来源）。"""
    res = result or {}
    conf = res.get('confidence')
    icon = {'ok': '🟢', 'low': '🟡', 'invalid': '🔴', 'unknown': '⚪'}.get(conf, '⚪')
    lines = [f'{icon} **证据等级审计：{res.get("verdict_cn") or "—"}**']
    for it in (res.get('issues') or []):
        lines.append(f'- {it}')
    # P0-4（2026-10-09 用户要求）：「N 处未走首选工具」必须列出——
    # 具体哪一处、应走哪个工具、实际走了什么，不能只报个数。
    vi = res.get('violations') or []
    if vi:
        lines.append(f'**未走首选工具清单（{len(vi)} 处）：**')
        # P0（2026-10-10）：逐条列出 数据 → 应走工具 → 实际来源 → 降级原因
        level_note = {'fallback': '首选工具未用，来源降级',
                      'mismatch': '口径与首选工具不一致'}
        for v in vi[:12]:
            note = level_note.get(v.get('level') or '', '口径存疑')
            val = f'（值 {v.get("value")}）' if v.get('value') else ''
            lines.append(f"- 「{v.get('metric') or '?'}」{val}"
                         f"应走 `{v.get('preferred') or '未登记'}`，"
                         f"实际用了 `{v.get('used') or '未标注工具'}` —— {note}")
        if len(vi) > 12:
            lines.append(f'- …另有 {len(vi) - 12} 处口径违规')
    weak = res.get('core_weak') or []
    if weak:
        lines.append('')
        lines.append('**需补权威来源的核心数字：**')
        for r in weak[:8]:
            src = r.get('source') or r.get('tool') or '未标注'
            lines.append(f"- `{r.get('metric') or '?'}` = `{r.get('value')}`"
                         f"（现等级 {r.get('grade')} · 来源 `{src}`）"
                         f" → 建议改用 `{preferred_tool(r.get('metric') or '') or '公司公告 / 权威来源'}`")
    return '\n'.join(lines)


def _cell(v: Any) -> str:
    if v is None or v == '':
        return '—'
    return str(v).replace('|', '\\|')
