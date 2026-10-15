# -*- coding: utf-8 -*-
"""financial_store.py — 版本化财务数据库（带 as-of 口径）+ 财报 PDF 分部数据提取

## 用户要求（2026-10-08 第 3 项）

> 接入财报 PDF 解析，自动提取分部收入、毛利率、经营利润。
> 建立财务数据库，带版本和 as-of 日期。
> 所有比率计算用 SQL/Python，不让 LLM 手算。

## 三条设计红线

1. **每条记录都带 `version` + `as_of`**：财务数据会被更正（追溯调整/口径变更），
   没有版本就无法解释"同一指标为什么两次不一样"；没有 as-of 就无法做时点回溯。
2. **比率一律由 SQL/Python 计算**：本模块提供 `derive_ratios()` 在库内用 SQL 派生，
   LLM 只允许**解读**算好的数字，绝不参与算术。
3. **PDF 提取必须标注页码与置信度**：财报版式千差万别，纯文本抽取**不能保证准确**，
   因此产出携带 `page` 与 `needs_review`，宁可标"待人工确认"也不假装精确。

存储用 DuckDB（项目已依赖），零新增第三方依赖（PDF 用已有的 PyMuPDF/pypdf）。
"""
import datetime
import logging
import os
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'data', 'financial_store.duckdb')
DB_PATH = os.environ.get('FIN_STORE_PATH', _DEFAULT_DB)

_TABLE = 'financials'
_COLS = ('version', 'as_of', 'metric', 'value', 'unit', 'currency',
         'source', 'source_tier', 'period', 'page', 'fetched_at', 'note')


def _conn(read_only: bool = False):
    import duckdb
    if not read_only:
        os.makedirs(os.path.dirname(DB_PATH) or '.', exist_ok=True)
    return duckdb.connect(DB_PATH, read_only=read_only)


def init_db() -> None:
    """建表（幂等）。"""
    con = _conn()
    try:
        con.execute(f"""
            CREATE TABLE IF NOT EXISTS {_TABLE} (
                version     VARCHAR,
                as_of       VARCHAR,     -- 数据截止日（时点口径，非写入时间）
                metric      VARCHAR NOT NULL,
                value       DOUBLE,
                unit        VARCHAR,
                currency    VARCHAR,
                source      VARCHAR,
                source_tier INTEGER,
                period      VARCHAR,     -- 报告期，如 2025-12-31
                page        VARCHAR,
                fetched_at  VARCHAR,
                note        VARCHAR
            )
        """)
    finally:
        con.close()


def now() -> str:
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def put(records: List[Dict[str, Any]], *, version: str = None, as_of: str = None,
        source: str = None, source_tier: int = None, period: str = None) -> int:
    """写入一批指标。`version`/`as_of` 可整批给，也可逐条覆盖。返回写入条数。"""
    if not records:
        return 0
    init_db()
    ver = version or _auto_version(as_of)
    rows = []
    for r in records:
        if not r or r.get('metric') in (None, '') or r.get('value') is None:
            continue
        rows.append((
            r.get('version') or ver,
            r.get('as_of') or as_of or '未标注',
            str(r['metric']),
            float(r['value']),
            r.get('unit') or '',
            r.get('currency') or 'CNY',
            r.get('source') or source or '未标注',
            r.get('source_tier') if r.get('source_tier') is not None else source_tier,
            r.get('period') or period or '未标注',
            str(r.get('page') or r.get('link') or '未标注'),
            r.get('fetched_at') or now(),
            r.get('note') or '',
        ))
    if not rows:
        return 0
    con = _conn()
    try:
        con.executemany(
            f"INSERT INTO {_TABLE} ({','.join(_COLS)}) VALUES ({','.join(['?'] * len(_COLS))})",
            rows)
    finally:
        con.close()
    return len(rows)


def _auto_version(as_of: str = None) -> str:
    stamp = (as_of or datetime.date.today().isoformat()).replace('-', '')
    return f'fin-{stamp}'


def query(metric: str = None, *, as_of: str = None, version: str = None,
          limit: int = 200) -> List[Dict[str, Any]]:
    """按指标 / as-of / version 查询（as-of 为**上界**：取不晚于该日的记录）。"""
    init_db()
    sql = f"SELECT {','.join(_COLS)} FROM {_TABLE} WHERE 1=1"
    params: List[Any] = []
    if metric:
        sql += " AND metric = ?"
        params.append(metric)
    if as_of:
        sql += " AND (as_of <= ? OR as_of = '未标注')"
        params.append(as_of)
    if version:
        sql += " AND version = ?"
        params.append(version)
    sql += " ORDER BY as_of DESC, fetched_at DESC LIMIT ?"
    params.append(int(limit))
    con = _conn()
    try:
        cur = con.execute(sql, params)
        return [dict(zip(_COLS, row)) for row in cur.fetchall()]
    finally:
        con.close()


def latest(metric: str, *, as_of: str = None) -> Optional[Dict[str, Any]]:
    """取某指标的**最新一版**（可按 as-of 截断，实现时点回溯）。"""
    rows = query(metric, as_of=as_of, limit=1)
    return rows[0] if rows else None


def history(metric: str, limit: int = 50) -> List[Dict[str, Any]]:
    """同指标的历史版本，用于回答"这个数字为什么变了"。"""
    return query(metric, limit=limit)


def metrics() -> List[str]:
    init_db()
    con = _conn()
    try:
        return [r[0] for r in con.execute(
            f"SELECT DISTINCT metric FROM {_TABLE} ORDER BY metric").fetchall()]
    finally:
        con.close()


# ==================== 比率：SQL 取值 + Python 计算，全程不经 LLM ====================

# key -> (中文名, 输入指标, 计算公式, 可审计的 SQL 表达式)
_RATIO_DEFS = {
    'net_margin': ('净利率', ('net_profit', 'revenue'),
                   lambda d: d['net_profit'] / d['revenue'] * 100,
                   'net_profit / revenue * 100', '%'),
    'gross_margin': ('毛利率', ('gross_profit', 'revenue'),
                     lambda d: d['gross_profit'] / d['revenue'] * 100,
                     'gross_profit / revenue * 100', '%'),
    'roe': ('ROE', ('net_profit', 'equity'),
            lambda d: d['net_profit'] / d['equity'] * 100,
            'net_profit / equity * 100', '%'),
    'debt_ratio': ('资产负债率', ('total_liab', 'total_assets'),
                   lambda d: d['total_liab'] / d['total_assets'] * 100,
                   'total_liab / total_assets * 100', '%'),
}


def _fetch_latest(con, metric: str, as_of: str = None, version: str = None):
    sql = f"SELECT value FROM {_TABLE} WHERE metric = ?"
    params: List[Any] = [metric]
    if as_of:
        sql += " AND (as_of <= ? OR as_of='未标注')"
        params.append(as_of)
    if version:
        sql += " AND version = ?"
        params.append(version)
    sql += " ORDER BY as_of DESC, fetched_at DESC LIMIT 1"
    row = con.execute(sql, params).fetchone()
    return row[0] if row else None


def derive_ratios(*, as_of: str = None, version: str = None) -> Dict[str, Any]:
    """用 **SQL 取值 + Python 计算** 派生比率，全程不经 LLM。

    返回 {'ratios': {...}, 'missing': [...]}。每项都带 `expr`（可审计的算式）
    与 `inputs`（用到的原始值），保证任何人能复算。
    """
    init_db()
    con = _conn()
    out: Dict[str, Any] = {'ratios': {}, 'missing': []}
    try:
        for key, (label, deps, fn, expr, unit) in _RATIO_DEFS.items():
            vals = {m: _fetch_latest(con, m, as_of, version) for m in deps}
            lack = [m for m, v in vals.items() if v is None]
            if lack:
                out['missing'].append(f"{label}（缺 {'、'.join(lack)}）")
                continue
            if any(v == 0 for v in vals.values()):
                out['missing'].append(f'{label}（分母为 0）')
                continue
            try:
                num = fn(vals)
            except (TypeError, ZeroDivisionError) as e:   # 单条失败不拖垮整体
                logger.debug('比率 %s 计算失败: %s', key, e)
                out['missing'].append(label)
                continue
            out['ratios'][key] = {
                'label': label, 'value': round(float(num), 4), 'unit': unit,
                'expr': expr, 'inputs': vals, 'as_of': as_of or '最新',
            }
    finally:
        con.close()
    return out


# ==================== 财报 PDF 分部数据提取 ====================

_SEG_KEYS = ('分部', '主营业务', '按产品', '按业务', '按地区', '营业收入构成',
             'Segment', 'Revenue by', 'Business Review')  # 港股财报多为中英双语
_NUM_RE = re.compile(r'(-?\d[\d,]*(?:\.\d+)?)\s*(%|亿元|百万元|万元|千元)?')


def _pdf_pages(path: str) -> List[str]:
    """逐页取文本（优先 PyMuPDF，退回 pypdf）。返回逐页文本列表。"""
    try:
        import fitz
        with fitz.open(str(path)) as doc:
            return [p.get_text() for p in doc]
    except ImportError:
        logger.debug('PyMuPDF 不可用，改用 pypdf')
    try:
        from pypdf import PdfReader
        return [p.extract_text() or '' for p in PdfReader(str(path)).pages]
    except ImportError:
        return []


def extract_pdf_segments(path: str, *, keywords: tuple = _SEG_KEYS,
                         max_pages: int = 12) -> Dict[str, Any]:
    """从财报 PDF 中定位「分部/主营构成」段落，抽取候选数值行。

    ⚠️ 财报版式差异极大，纯文本抽取**不能保证准确**。因此：
      - 每个候选行都带 `page`（页码，作溯源）；
      - 整体标 `needs_review=True`，必须人工核对后才可入库对外使用；
      - 抽不到就如实返回原因，绝不编造。
    """
    if not os.path.exists(str(path)):
        return {'ok': False, 'reason': f'文件不存在: {path}', 'rows': [], 'needs_review': True}
    pages = _pdf_pages(path)
    if not pages:
        return {'ok': False, 'reason': 'PDF 文本提取不可用（缺 PyMuPDF/pypdf）或文件无文本层',
                'rows': [], 'needs_review': True}

    rows: List[Dict[str, Any]] = []
    hit_pages: List[int] = []
    for i, text in enumerate(pages[:max_pages]):
        if not any(k in text for k in keywords):
            continue
        hit_pages.append(i + 1)
        for line in text.split('\n'):
            line = line.strip()
            if not line or len(line) > 80:
                continue
            nums = _NUM_RE.findall(line)
            if not nums:
                continue
            # 名称 = 去掉数字后的中文片段
            name = re.sub(r'[\d,.\s%（）()亿元百千万]+', ' ', line).strip()
            if not name or len(name) < 2:
                continue
            vals = []
            for n, unit in nums:
                try:
                    vals.append({'value': float(n.replace(',', '')), 'unit': unit or ''})
                except ValueError as e:
                    logger.debug('分部行数值解析跳过 %r：%s', n, e)
                    continue
            if vals:
                rows.append({'name': name[:40], 'values': vals, 'page': i + 1,
                             'raw': line[:120]})

    if not rows:
        return {'ok': False, 'reason': f'未在 PDF 中定位到分部数据（检索页 {max_pages} 页，'
                                       f'关键词 {keywords}）',
                'rows': [], 'pages_scanned': min(len(pages), max_pages), 'needs_review': True}

    return {
        'ok': True, 'rows': rows, 'hit_pages': hit_pages,
        'pages_scanned': min(len(pages), max_pages),
        'needs_review': True,
        'note': 'PDF 文本抽取仅供**定位与初筛**，同一行的数字与单位对应关系需人工核对；'
                '确认后再经 financial_store.put() 入库并标注页码。',
        'field_hint': '期望字段：分部名称 / 收入 / 同比 / 占比 / 毛利率 / 经营利润',
    }


def to_records(rows: List[Dict[str, Any]], *, segment: str, period: str,
               fields: tuple = ('revenue', 'yoy', 'share', 'gross_margin', 'operating_profit')
               ) -> List[Dict[str, Any]]:
    """把人工确认过的分部行转成入库记录（顺序对应 `fields`）。"""
    out: List[Dict[str, Any]] = []
    for r in rows or []:
        vals = [v.get('value') for v in (r.get('values') or [])]
        for i, f in enumerate(fields):
            if i < len(vals) and vals[i] is not None:
                out.append({'metric': f'{segment}.{f}', 'value': vals[i],
                            'page': r.get('page'), 'period': period,
                            'note': f"分部原始行：{r.get('raw', '')[:60]}"})
    return out