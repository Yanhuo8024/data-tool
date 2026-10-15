# -*- coding: utf-8 -*-
"""合规审计：为每一次金融输出生成**可追溯凭证**。

满足「所有输出可追溯」的四件事
----------------------------
1. **来源链**：每个数字都能回指到「来源名称 / 层级 / 报告期 / 抓取时间 / 链接或页码」。
2. **计算留痕**：公式名 + 算式 + 输入 + 输出，可复算（LLM 不经手算术）。
3. **合规检查**：免责声明、币种一致、时效、极端估值、风险提示（复用 ``risk_review`` 的结论）。
4. **防篡改**：每条记录带 ``prev_hash``，形成**哈希链**；``verify_chain()`` 可检出任何改动。

零项目依赖（仅标准库），可与 ``data_provenance`` / ``risk_review`` 对接但**不修改**它们。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# 数据来源优先级（与 data_provenance 同口径）
TIER_NAMES = {1: '交易所公告', 2: '公司IR/财报', 3: '权威金融终端',
              4: '公开新闻', 5: '百科/其他'}

# 合规判定三档
VERDICT_CN = {'pass': '🟢 可发布', 'needs_review': '🟡 需人工复核',
              'block': '🔴 禁止发布'}

# 凭证必填项（缺失即视为不完整，不允许对外发布）
REQUIRED_FIELDS = ('kind', 'subject', 'sources', 'computations', 'as_of')

_DEFAULT_DIR = os.environ.get('AUDIT_TRAIL_DIR') or os.path.join('data', 'audit')


# ==================== 构建 ====================


def build_record(*, kind: str, subject: str,
                 sources: Iterable[Dict[str, Any]],
                 computations: Iterable[Dict[str, Any]],
                 compliance: Optional[Dict[str, Any]] = None,
                 model: Optional[str] = None,
                 prompt_version: Optional[str] = None,
                 as_of: Optional[str] = None,
                 actor: str = 'system',
                 payload: Optional[Dict[str, Any]] = None,
                 note: str = '') -> Dict[str, Any]:
    """构造一条审计凭证（尚未计算哈希；由 :class:`AuditLog` 追加时补齐）。

    ``sources`` 每条：``{'name','tier','period','retrieved_at','url'/'page','metric'}``
    ``computations`` 每条：``{'name','expr','inputs','output','unit'}``
    ``compliance``：``{"verdict": 'pass|needs_review|block', 'findings': [...]}``
    """
    src = [_norm_source(s) for s in (sources or [])]
    comp = [_norm_computation(c) for c in (computations or [])]
    comp_verdict = (compliance or {}).get('verdict') or 'needs_review'
    return {
        'record_id': uuid.uuid4().hex[:16],
        'created_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'kind': kind,
        'subject': subject,
        'as_of': as_of or _oldest_period(src) or '未标注',
        'actor': actor,
        'model': model,
        'prompt_version': prompt_version,
        'sources': src,
        'computations': comp,
        'compliance': {
            'verdict': comp_verdict,
            'verdict_cn': VERDICT_CN.get(comp_verdict, comp_verdict),
            'findings': [dict(f) for f in ((compliance or {}).get('findings') or [])],
        },
        'payload': payload or {},
        'note': note,
        'traceable': None,          # 由 _finalize 计算
        'warnings': [],             # 由 _finalize 计算
    }


def _norm_source(s: Dict[str, Any]) -> Dict[str, Any]:
    tier = s.get('tier')
    try:
        tier = int(tier) if tier is not None else None
    except (TypeError, ValueError):
        tier = None
    return {
        'metric': str(s.get('metric') or s.get('field') or ''),
        'name': str(s.get('name') or s.get('source') or '未标注来源'),
        'tier': tier,
        'tier_cn': TIER_NAMES.get(tier, '未分级'),
        'period': str(s.get('period') or s.get('as_of') or ''),
        'retrieved_at': str(s.get('retrieved_at') or s.get('fetched_at') or ''),
        'locator': str(s.get('url') or s.get('page') or s.get('locator') or ''),
    }


def _norm_computation(c: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'name': str(c.get('name') or ''),
        'expr': str(c.get('expr') or ''),
        'inputs': dict(c.get('inputs') or {}),
        'output': c.get('output'),
        'unit': str(c.get('unit') or ''),
    }


def _oldest_period(sources: List[Dict[str, Any]]) -> str:
    """以**最早**的报告期作为 as-of：保守口径（数据不得晚于声明）。"""
    periods = [s['period'] for s in sources if s.get('period')]
    return min(periods) if periods else ''


def _finalize(record: Dict[str, Any]) -> Dict[str, Any]:
    """计算可追溯性与完整性告警（不含哈希链字段）。"""
    missing = [f for f in REQUIRED_FIELDS
               if not record.get(f) or (f in ('sources', 'computations')
                                        and not record.get(f))]
    warn: List[str] = []
    if missing:
        warn.append('凭证不完整，缺少：' + '、'.join(missing))
    no_loc = [s['metric'] or s['name'] for s in record['sources'] if not s['locator']]
    if no_loc:
        warn.append('来源缺定位（URL/页码）：' + '、'.join(no_loc))
    if any(s['tier'] is None for s in record['sources']):
        warn.append('存在未分级来源')
    elif any(s['tier'] >= 4 for s in record['sources']):
        warn.append('含低层级来源（新闻/百科），对外发布前建议升级来源')
    if record['computations'] and any(not c['expr'] for c in record['computations']):
        warn.append('存在无算式记录的计算项')
    if not record['computations'] and record['kind'] in ('valuation', 'ratio'):
        warn.append('估值/比率类输出应附计算式')
    record['warnings'] = warn
    record['traceable'] = not missing and not any(
        w.startswith('来源缺定位') for w in warn)
    return record


# ==================== 哈希链 ====================


def _canonical(record: Dict[str, Any], *, drop=('hash', 'prev_hash')) -> str:
    body = {k: v for k, v in record.items() if k not in drop}
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def compute_hash(record: Dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(record).encode('utf-8')).hexdigest()


class AuditLog:
    """凭证的追加式存储（JSONL）+ 哈希链校验。"""

    def __init__(self, path: Optional[str] = None, *, enabled: bool = True):
        self.path = path or os.path.join(_DEFAULT_DIR, 'audit_trail.jsonl')
        self.enabled = enabled
        self._dir = os.path.dirname(os.path.abspath(self.path))
        self._cache: List[Dict[str, Any]] = []

    # ---- 写入 ----

    def append(self, record: Dict[str, Any]) -> Dict[str, Any]:
        rec = _finalize(dict(record))
        rec.pop('hash', None)
        rec['prev_hash'] = self._tail_hash()
        rec['hash'] = compute_hash(rec)
        self._cache.append(rec)
        if self.enabled:
            try:
                os.makedirs(self._dir, exist_ok=True)
                with open(self.path, 'a', encoding='utf-8', newline='\n') as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
            except OSError as e:               # 落盘失败不得中断业务，但要可见
                logger.warning('审计凭证落盘失败（内存保留）: %s', e)
        return rec

    def _tail_hash(self) -> str:
        if self._cache:
            return self._cache[-1]['hash']
        rows = self.read_all()
        return rows[-1]['hash'] if rows else ''

    # ---- 读取 ----

    def read_all(self) -> List[Dict[str, Any]]:
        if not os.path.exists(self.path):
            return []
        out: List[Dict[str, Any]] = []
        try:
            with open(self.path, encoding='utf-8', newline='') as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError as e:
                        logger.warning('审计行解析失败，已跳过: %s', e)
        except OSError as e:
            logger.warning('审计凭证读取失败: %s', e)
        return out

    # ---- 校验 ----

    def verify(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """校验单条记录：哈希是否被改动 + 内容完整性。"""
        rec = dict(record)
        got = rec.pop('hash', None)
        expect = compute_hash(rec)
        ref = _finalize(dict(rec))
        return {
            'ok': bool(got) and got == expect,
            'hash_ok': bool(got) and got == expect,
            'traceable': ref['traceable'],
            'warnings': ref['warnings'],
            'record_id': rec.get('record_id'),
        }

    def verify_chain(self, records: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """校验整条哈希链：任何一条被改动或删除都会暴露。"""
        rows = records if records is not None else self.read_all()
        broken: List[Dict[str, Any]] = []
        prev = ''
        for i, rec in enumerate(rows):
            body = dict(rec)
            got = body.pop('hash', None)
            if body.get('prev_hash', '') != prev:
                broken.append({'index': i, 'reason': 'prev_hash 断链',
                               'record_id': rec.get('record_id')})
            if got != compute_hash(body):
                broken.append({'index': i, 'reason': '内容哈希不符（可能被篡改）',
                               'record_id': rec.get('record_id')})
            prev = got or ''
        return {'ok': not broken, 'count': len(rows), 'broken': broken}

    # ---- 导出 ----

    def export_json(self, records: Optional[List[Dict[str, Any]]] = None,
                    path: Optional[str] = None) -> str:
        rows = records if records is not None else self.read_all()
        target = path or os.path.join(self._dir, 'audit_export.json')
        os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
        with open(target, 'w', encoding='utf-8', newline='\n') as fh:
            json.dump({'exported_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
                       'chain': self.verify_chain(rows), 'records': rows},
                      fh, ensure_ascii=False, indent=2)
        return target


def render_markdown(record: Dict[str, Any]) -> str:
    """把凭证渲染成人可读的审计报告。"""
    r = _finalize(dict(record))
    lines = [f"### 审计凭证 {r['record_id']}", '']
    lines += [f"- **输出类型**：{r['kind']}　**标的**：{r['subject']}",
              f"- **数据截止**：{r['as_of']}　**生成时间**：{r['created_at']}　**责任人**：{r['actor']}"]
    if r.get('model') or r.get('prompt_version'):
        lines.append(f"- **模型**：{r.get('model') or '—'}　**提示词版本**：{r.get('prompt_version') or '—'}")
    comp = r['compliance']
    lines += ['', f"**合规裁定**：{comp['verdict_cn']}"]
    for f in comp['findings']:
        lines.append(f"  - （{f.get('level', '')}）{f.get('label', f.get('rule', ''))}：{f.get('advice', '')}")
    lines += ['', '**数据来源（按优先级）**', '',
              '| 指标 | 来源 | 层级 | 报告期 | 抓取时间 | 定位 |',
              '|---|---|---|---|---|---|']
    for s in r['sources']:
        lines.append(f"| {s['metric']} | {s['name']} | {s['tier_cn']} | {s['period']} "
                     f"| {s['retrieved_at']} | {s['locator']} |")
    if r['computations']:
        lines += ['', '**计算留痕**', '', '| 项目 | 算式 | 输入 | 输出 |', '|---|---|---|---|']
        for c in r['computations']:
            lines.append(f"| {c['name']} | `{c['expr']}` | {json.dumps(c['inputs'], ensure_ascii=False)} "
                         f"| {c['output']} {c['unit']} |")
    if r['warnings']:
        lines += ['', '**完整性告警**'] + [f"  - {w}" for w in r['warnings']]
    lines += ['', f"- **可对外追溯**：{'是' if r['traceable'] else '否'}", f"- **内容哈希**：`{r.get('hash', '待生成')}`"]
    return '\n'.join(lines)
