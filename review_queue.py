# -*- coding: utf-8 -*-
"""用户反馈闭环：错误标记 → 人工复核队列。

流程
----
用户标记错误 → ``pending``（待复核）
  → ``triaged``（已定级/分类）
    → ``confirmed``（确认为错误）→ ``fixed``（已修复）
    → ``rejected``（驳回：并非错误）

每条记录带 **SLA 到期时间**（按严重度）、**状态变更历史**（谁在何时改的）、
**关联上下文**（会话 id / 消息 id / 原始输出片段），便于事后审计。

零项目依赖（仅标准库），可与 ``feedback_loop.BadCaseCollector`` 对接（可选 hook）。
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# ==================== 枚举 ====================

STATUSES = ('pending', 'triaged', 'confirmed', 'fixed', 'rejected')
FINAL_STATUSES = ('fixed', 'rejected')
STATUS_CN = {'pending': '待复核', 'triaged': '已定级', 'confirmed': '已确认错误',
             'fixed': '已修复', 'rejected': '已驳回'}

# 合法状态转移
TRANSITIONS: Dict[str, tuple] = {
    'pending':   ('triaged', 'rejected'),
    'triaged':   ('confirmed', 'rejected'),
    'confirmed': ('fixed', 'rejected'),
    'fixed':     (),
    'rejected':  (),
}

CATEGORIES = ('data_error', 'caliber_error', 'conclusion_error', 'format_issue',
              'hallucination', 'cash_flow_missing', 'low_evidence',
              'risk_false_positive', 'number_parse_error', 'other')
CATEGORY_CN = {'data_error': '数据错误', 'caliber_error': '口径/币种错误',
               'conclusion_error': '结论错误', 'format_issue': '格式问题',
               'hallucination': '疑似编造',
               'cash_flow_missing': '现金流未补', 'low_evidence': '证据等级低',
               'risk_false_positive': '风控误报', 'number_parse_error': '数字解析错误',
               'other': '其他'}

SEVERITIES = ('high', 'mid', 'low')
SEVERITY_CN = {'high': '高', 'mid': '中', 'low': '低'}

# SLA 时限（小时）
SLA_HOURS = {'high': 4, 'mid': 24, 'low': 72}

# 分类 → 默认严重度（用户只描述现象时给出合理默认）
# 「现金流未补」「证据等级低」是**缺项与质量问题**，非编造，故默认中；
# 「风控误报」进**规则修复队列**——反复误报说明规则要修，默认中；
# 「数字解析错误」可能导致漏核验，默认中。
_CATEGORY_SEVERITY = {'data_error': 'high', 'caliber_error': 'high',
                      'hallucination': 'high', 'conclusion_error': 'mid',
                      'cash_flow_missing': 'mid', 'low_evidence': 'mid',
                      'risk_false_positive': 'mid', 'number_parse_error': 'mid',
                      'format_issue': 'low', 'other': 'low'}

_DEFAULT_DIR = os.environ.get('REVIEW_QUEUE_DIR') or os.path.join('data', 'review_queue')


# ==================== 队列 ====================


class ReviewQueue:
    """人工复核队列（JSONL 追加式存储 + 内存索引）。"""

    def __init__(self, path: Optional[str] = None, *, enabled: bool = True,
                 badcase_hook: Optional[Callable[[Dict[str, Any]], None]] = None):
        self.path = path or os.path.join(_DEFAULT_DIR, 'queue.jsonl')
        self.enabled = enabled
        self._dir = os.path.dirname(os.path.abspath(self.path))
        self._items: Dict[str, Dict[str, Any]] = {}
        self._badcase_hook = badcase_hook
        self._load()

    # ---------- 载入 ----------

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding='utf-8', newline='') as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError as e:
                        logger.debug('复核队列行解析失败，已跳过: %s', e)
                        continue
                    if rec.get('id'):
                        self._items[rec['id']] = rec
        except OSError as e:
            logger.warning('复核队列载入失败: %s', e)

    def _persist(self, rec: Dict[str, Any]) -> None:
        if not self.enabled:
            return
        try:
            os.makedirs(self._dir, exist_ok=True)
            with open(self.path, 'a', encoding='utf-8', newline='\n') as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
        except OSError as e:
            logger.warning('复核队列落盘失败（内存保留）: %s', e)

    # ---------- 提交 ----------

    def submit(self, *, reason: str, category: str = 'other',
               severity: Optional[str] = None, context: Optional[Dict[str, Any]] = None,
               reporter: str = 'user', output_excerpt: str = '') -> Dict[str, Any]:
        """用户标记错误 → 入队。

        ``context`` 建议含 ``{'session_id','message_id','symbol','source'}``，
        使复核人能一键回到原始输出。
        """
        cat = category if category in CATEGORIES else 'other'
        sev = severity if severity in SEVERITIES else _CATEGORY_SEVERITY.get(cat, 'low')
        now = time.time()
        rec = {
            'id': uuid.uuid4().hex[:12],
            'created_at': _ts(now),
            'updated_at': _ts(now),
            'status': 'pending',
            'status_cn': STATUS_CN['pending'],
            'category': cat,
            'category_cn': CATEGORY_CN.get(cat, cat),
            'severity': sev,
            'severity_cn': SEVERITY_CN.get(sev, sev),
            'reason': str(reason or '').strip(),
            'output_excerpt': str(output_excerpt or '')[:800],
            'context': dict(context or {}),
            'reporter': reporter,
            'assignee': None,
            'sla_due': _ts(now + SLA_HOURS.get(sev, 72) * 3600),
            'resolved_at': None,
            'resolution': '',
            'history': [{'at': _ts(now), 'from': None, 'to': 'pending',
                         'actor': reporter, 'note': '用户标记错误'}],
        }
        self._items[rec['id']] = rec
        self._persist(rec)
        if self._badcase_hook:
            try:
                self._badcase_hook(rec)
            except Exception as e:              # 下游 hook 异常不得影响入队
                logger.debug('badcase hook 失败: %s', e)
        return rec

    # ---------- 流转 ----------

    def transition(self, rec_id: str, to_status: str, *, actor: str = 'reviewer',
                   note: str = '', resolution: str = '') -> Dict[str, Any]:
        rec = self._items.get(rec_id)
        if not rec:
            raise KeyError(f'复核项不存在: {rec_id}')
        if to_status not in STATUSES:
            raise ValueError(f'未知状态: {to_status}')
        cur = rec['status']
        if to_status not in TRANSITIONS.get(cur, ()):
            raise ValueError(f'非法状态转移：{cur} → {to_status}'
                             f"（允许：{'、'.join(TRANSITIONS.get(cur) or ['无'])}）")
        now = time.time()
        rec['status'] = to_status
        rec['status_cn'] = STATUS_CN.get(to_status, to_status)
        rec['updated_at'] = _ts(now)
        if resolution:
            rec['resolution'] = resolution
        if to_status in FINAL_STATUSES:
            rec['resolved_at'] = _ts(now)
        rec['history'].append({'at': _ts(now), 'from': cur, 'to': to_status,
                               'actor': actor, 'note': note})
        self._persist(rec)
        return rec

    def assign(self, rec_id: str, assignee: str, *, actor: str = 'system') -> Dict[str, Any]:
        rec = self._items.get(rec_id)
        if not rec:
            raise KeyError(f'复核项不存在: {rec_id}')
        rec['assignee'] = assignee
        rec['updated_at'] = _ts(time.time())
        rec['history'].append({'at': rec['updated_at'], 'from': rec['status'],
                               'to': rec['status'], 'actor': actor,
                               'note': f'指派给 {assignee}'})
        self._persist(rec)
        return rec

    # ---------- 查询 ----------

    def get(self, rec_id: str) -> Optional[Dict[str, Any]]:
        return self._items.get(rec_id)

    def list(self, *, status: Optional[str] = None, severity: Optional[str] = None,
             category: Optional[str] = None, limit: int = 100,
             oldest_first: bool = True) -> List[Dict[str, Any]]:
        rows = list(self._items.values())
        if status:
            rows = [r for r in rows if r['status'] == status]
        if severity:
            rows = [r for r in rows if r['severity'] == severity]
        if category:
            rows = [r for r in rows if r['category'] == category]
        rows.sort(key=lambda r: r['created_at'], reverse=not oldest_first)
        return rows[:max(1, limit)]

    def overdue(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """未结项中已超过 SLA 的（按严重度排序）。"""
        now = now or time.time()
        out = []
        for r in self._items.values():
            if r['status'] in FINAL_STATUSES:
                continue
            try:
                due = time.mktime(time.strptime(r['sla_due'], '%Y-%m-%dT%H:%M:%S'))
            except (ValueError, TypeError) as e:
                logger.debug('SLA 时间解析失败 %s: %s', r.get('id'), e)
                continue
            if now > due:
                out.append(dict(r, overdue_hours=round((now - due) / 3600, 1)))
        order = {'high': 0, 'mid': 1, 'low': 2}
        out.sort(key=lambda r: (order.get(r['severity'], 9), -r['overdue_hours']))
        return out

    def stats(self) -> Dict[str, Any]:
        by_status = {s: 0 for s in STATUSES}
        by_sev = {s: 0 for s in SEVERITIES}
        by_cat: Dict[str, int] = {}
        for r in self._items.values():
            by_status[r['status']] = by_status.get(r['status'], 0) + 1
            by_sev[r['severity']] = by_sev.get(r['severity'], 0) + 1
            by_cat[r['category']] = by_cat.get(r['category'], 0) + 1
        open_n = sum(1 for r in self._items.values() if r['status'] not in FINAL_STATUSES)
        return {'total': len(self._items), 'open': open_n,
                'by_status': by_status, 'by_severity': by_sev, 'by_category': by_cat,
                'overdue': len(self.overdue()),
                'fix_rate': _rate(self._items, 'fixed')}


def _rate(items: Dict[str, Any], status: str) -> float:
    if not items:
        return 0.0
    return round(sum(1 for r in items.values() if r['status'] == status) / len(items), 4)


def _ts(epoch: float) -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(epoch))


def enqueue_from_feedback(feedback: str, payload: Optional[Dict[str, Any]] = None, *,
                          session_id: str = '', reply: str = '') -> Optional[str]:
    """把一次**负面**反馈转成人工复核项，返回 review id（非负面返回 None）。

    接在 ``/api/assistant/feedback`` 的 dislike/inaccurate 分支后，即实现
    「用户标记错误 → 进入人工复核队列」。**绝不抛异常**（反馈路径不能因入队失败而失败）。
    """
    if str(feedback or '').lower() not in ('dislike', 'inaccurate'):
        return None
    data = payload or {}
    try:
        rec = ReviewQueue().submit(
            reason=str(data.get('reason') or '用户标记回答有误'),
            category=str(data.get('category') or 'conclusion_error'),
            context={'session_id': session_id,
                     'message_id': data.get('message_id') or '',
                     'source': data.get('source') or ''},
            output_excerpt=str(reply or data.get('reply') or ''),
            reporter='user')
        return rec['id']
    except Exception as e:                  # 入队失败只告警，不影响用户操作
        logger.warning('反馈入队人工复核失败: %s', e)
        return None


def render_item(rec: Dict[str, Any]) -> str:
    """把一条复核项渲染成可读文本（给复核人看）。"""
    lines = [f"### 复核项 {rec['id']} · {rec.get('status_cn')}",
             f"- **类别**：{rec.get('category_cn')}　**严重度**：{rec.get('severity_cn')}"
             f"　**SLA 到期**：{rec.get('sla_due')}",
             f"- **提交人**：{rec.get('reporter')}　**处理人**：{rec.get('assignee') or '未指派'}",
             f"- **问题描述**：{rec.get('reason')}"]
    ctx = rec.get('context') or {}
    if ctx:
        lines.append('- **上下文**：' + '　'.join(f'{k}={v}' for k, v in ctx.items()))
    if rec.get('output_excerpt'):
        lines += ['', '**原始输出片段**', '', '```', rec['output_excerpt'], '```']
    if rec.get('resolution'):
        lines.append(f"- **处置结论**：{rec['resolution']}")
    lines += ['', '**流转历史**']
    for h in rec.get('history', []):
        lines.append(f"  - {h['at']} {h.get('from') or '—'} → {h['to']}（{h.get('actor')}）"
                     + (f"：{h['note']}" if h.get('note') else ''))
    return '\n'.join(lines)
