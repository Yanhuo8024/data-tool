# -*- coding: utf-8 -*-
"""事件驱动估值更新（实时行情 / 公告 / 经营数据 → 自动重估）。

设计要点
--------
1. **零项目依赖**：只 import 标准库。重估函数与取数函数一律**由调用方注入**，
   避免与 ``valuation`` / ``finance_data`` 形成 import 环（会触门禁 R6）。
2. **事件三类**：``quote``（行情异动）、``announcement``（公告）、``delivery``（交付/销量等经营数据）。
3. **自动更新估值**：事件 → 重估 → 与上一次结果对比 → 产出可读的「变动摘要」，
   并判断新结论是否落入闸门（冻结 / 假设极端 / 模型异常）或发生方向翻转 →
   标记 ``needs_review``，交人工复核（见 ``review_queue``）。
4. **去重**：同一 ``symbol + kind + 指纹`` 在 TTL 内只触发一次，防止轮询刷屏。

供 ``scheduler_manager`` 定时调用 ``scan()`` 即可实现"行情变化自动重估"。
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# ==================== 阈值 ====================

QUOTE_MOVE_PCT = 5.0          # 单日涨跌幅超过该值 → 行情事件
QUOTE_GAP_PCT = 3.0           # 相对上次快照的变动超过该值 → 行情事件
DEDUP_TTL_SEC = 6 * 3600      # 同一事件指纹的去重窗口
MAX_HISTORY = 300             # 保留的估值历史条数

# 公告关键词：命中即视为可能影响估值
ANNOUNCEMENT_KW = (
    '业绩', '财报', '年报', '中报', '季报', '盈警', '盈喜', '盈利警告', '盈利预告',
    '分红', '派息', '配股', '供股', '回购', '收购', '并购', '重组', '重大合同',
    '停产', '复产', '减产', '诉讼', '仲裁', '处罚', '退市', '更名', '股权变动',
    '增减持', '解禁', '定增', '可转债', 'earnings', 'guidance', 'profit warning',
)

# 经营数据关键词（交付量 / 销量等）
DELIVERY_KW = ('交付', '销量', '出货', '产量', '装机', '订单', '产能', '门店', '用户数',
               'deliveries', 'shipments', 'orders')

# 结论落入这些闸门 → 必须人工复核
REVIEW_VERDICTS = ('frozen', 'assumption_extreme', 'model_anomaly')


# ==================== 事件模型 ====================


@dataclass
class Event:
    """一条待处理的事件。"""

    kind: str                                  # quote | announcement | delivery
    symbol: str
    market: str = 'a'                          # a | hk | us
    payload: Dict[str, Any] = field(default_factory=dict)
    source: str = ''
    ts: float = field(default_factory=time.time)
    eid: str = ''

    def __post_init__(self) -> None:
        if not self.eid:
            self.eid = self.fingerprint()

    def fingerprint(self) -> str:
        """指纹：同标的同类同内容 → 视为同一事件。"""
        raw = f"{self.kind}|{self.symbol}|{self.market}|{_stable(self.payload)}"
        return hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]

    def to_dict(self) -> Dict[str, Any]:
        return {'eid': self.eid, 'kind': self.kind, 'symbol': self.symbol,
                'market': self.market, 'source': self.source, 'ts': self.ts,
                'payload': self.payload}


def _stable(obj: Any) -> str:
    """把 dict 稳定序列化（排序键），保证指纹可复现。"""
    if isinstance(obj, dict):
        return '{' + ','.join(f'{k}:{_stable(obj[k])}' for k in sorted(obj, key=str)) + '}'
    if isinstance(obj, (list, tuple)):
        return '[' + ','.join(_stable(x) for x in obj) + ']'
    return str(obj)


# ==================== 事件检测 ====================


def detect_quote_event(old: Optional[Dict[str, Any]], new: Dict[str, Any],
                       threshold: float = QUOTE_MOVE_PCT) -> Optional[Event]:
    """行情异动：单日涨跌幅或相对上次快照的变动超阈值。

    ``old`` / ``new`` 形如 ``{'symbol','market','price','change_pct'}``。
    """
    if not new or new.get('price') in (None, 0):
        return None
    symbol = str(new.get('symbol') or '')
    if not symbol:
        return None
    move = _f(new.get('change_pct'))
    drift = None
    if old and _f(old.get('price')):
        drift = (float(new['price']) - float(old['price'])) / float(old['price']) * 100.0
    hit = (move is not None and abs(move) >= threshold) or \
          (drift is not None and abs(drift) >= QUOTE_GAP_PCT)
    if not hit:
        return None
    why = [] if move is None or abs(move) < threshold else [f'单日 {move:+.2f}%']
    if drift is not None and abs(drift) >= QUOTE_GAP_PCT:
        why.append(f'较上次快照 {drift:+.2f}%')
    return Event(kind='quote', symbol=symbol, market=str(new.get('market') or 'a'),
                 payload={'price': new['price'], 'change_pct': move,
                          'drift_pct': round(drift, 2) if drift is not None else None},
                 source=str(new.get('source') or '行情'))


def detect_announcement_events(symbol: str, market: str,
                               items: Iterable[Any]) -> List[Event]:
    """公告事件：标题/正文命中关键词即产出。``items`` 可为 str 或 dict(title/date/url)。"""
    out: List[Event] = []
    for it in items or []:
        if isinstance(it, dict):
            title, url, date = str(it.get('title') or ''), str(it.get('url') or ''), \
                str(it.get('date') or it.get('time') or '')
        else:
            title, url, date = str(it), '', ''
        if not title:
            continue
        hit = [k for k in ANNOUNCEMENT_KW if k in title]
        if not hit:
            continue
        out.append(Event(kind='announcement', symbol=symbol, market=market,
                         payload={'title': title, 'url': url, 'date': date, 'hit': hit},
                         source='公告'))
    return out


def detect_delivery_events(symbol: str, market: str, records: Iterable[Any]) -> List[Event]:
    """经营数据事件：交付量/销量类。``records`` 为 str 或 dict(title/value/period)。"""
    out: List[Event] = []
    for rec in records or []:
        if isinstance(rec, dict):
            text = str(rec.get('title') or rec.get('text') or '')
            period = str(rec.get('period') or rec.get('date') or '')
            value = rec.get('value')
        else:
            text, period, value = str(rec), '', None
        if not text or not any(k in text for k in DELIVERY_KW):
            continue
        out.append(Event(kind='delivery', symbol=symbol, market=market,
                         payload={'title': text, 'period': period, 'value': value},
                         source='经营数据'))
    return out


# ==================== 引擎 ====================


class ValuationEventEngine:
    """事件 → 重估 → 变更摘要。重估函数由调用方注入（零循环依赖）。

    ``revalue_fn(symbol, market, event)`` 返回形如::

        {'ok': True, 'per_share': 68.08, 'price': 23.94, 'verdict': 'assumption_extreme',
         'verdict_cn': '…', 'flags': [...], 'as_of': '2025-12-31', 'guard': {...}}

    返回 ``None`` 或 ``ok=False`` 表示本次无法重估（会如实记录原因，不伪造结论）。
    """

    def __init__(self, revalue_fn: Callable[..., Optional[Dict[str, Any]]], *,
                 dedup_ttl: int = DEDUP_TTL_SEC, max_history: int = MAX_HISTORY):
        self._revalue_fn = revalue_fn
        self._dedup_ttl = dedup_ttl
        self._max_history = max_history
        self._last: Dict[str, Dict[str, Any]] = {}      # symbol -> 上次估值
        self._snap: Dict[str, Dict[str, Any]] = {}      # symbol -> 上次行情快照
        self._seen: Dict[str, float] = {}               # fingerprint -> ts
        self._history: List[Dict[str, Any]] = []
        self._subs: Dict[str, List[Callable[[Dict[str, Any]], None]]] = {}
        self._stats = {'ingested': 0, 'deduped': 0, 'revalued': 0, 'failed': 0, 'review': 0}

    # ---- 订阅 ----

    def subscribe(self, kind: str, cb: Callable[[Dict[str, Any]], None]) -> None:
        """订阅某类事件的估值结果（``kind='*'`` 订阅全部）。"""
        self._subs.setdefault(kind, []).append(cb)

    # ---- 去重 ----

    def _is_dup(self, fp: str, now: float) -> bool:
        ts = self._seen.get(fp)
        if ts is not None and now - ts < self._dedup_ttl:
            return True
        self._seen[fp] = now
        # 清理过期指纹，避免无界增长
        if len(self._seen) > 5000:
            cut = now - self._dedup_ttl
            self._seen = {k: v for k, v in self._seen.items() if v >= cut}
        return False

    # ---- 主流程 ----

    def ingest(self, events: Iterable[Event]) -> List[Dict[str, Any]]:
        """处理一批事件，返回每条的估值变更结果。"""
        now = time.time()
        outcomes: List[Dict[str, Any]] = []
        for ev in events or []:
            self._stats['ingested'] += 1
            if self._is_dup(ev.fingerprint(), now):
                self._stats['deduped'] += 1
                continue
            outcome = self._handle(ev)
            if outcome:
                outcomes.append(outcome)
                for cb in self._subs.get(ev.kind, []) + self._subs.get('*', []):
                    try:
                        cb(outcome)
                    except Exception as e:      # 订阅者异常不得影响主流程
                        logger.debug('估值事件订阅者回调失败: %s', e)
        return outcomes

    def _handle(self, ev: Event) -> Optional[Dict[str, Any]]:
        old = self._last.get(ev.symbol)
        try:
            new = self._revalue_fn(ev.symbol, ev.market, ev)
        except Exception as e:                  # 取数/重估失败如实记录
            self._stats['failed'] += 1
            logger.warning('事件重估失败 %s/%s: %s', ev.kind, ev.symbol, e)
            return {'ok': False, 'event': ev.to_dict(), 'symbol': ev.symbol,
                    'error': f'{type(e).__name__}: {e}', 'ts': time.time()}
        if not new or new.get('ok') is False:
            self._stats['failed'] += 1
            return {'ok': False, 'event': ev.to_dict(), 'symbol': ev.symbol,
                    'error': (new or {}).get('error') or '重估未返回结果', 'ts': time.time()}

        self._stats['revalued'] += 1
        verdict = str(new.get('verdict') or '')
        flags = list(new.get('flags') or [])
        flipped = bool(old and old.get('verdict') and verdict and
                       old['verdict'] != verdict)
        gate = verdict in REVIEW_VERDICTS
        needs_review = gate or flipped
        if needs_review:
            self._stats['review'] += 1

        delta = None
        if old and _f(old.get('per_share')) and _f(new.get('per_share')):
            base = float(old['per_share']) or 1e-9
            delta = (float(new['per_share']) - base) / base * 100.0

        cur = {'per_share': new.get('per_share'), 'verdict': verdict,
               'verdict_cn': new.get('verdict_cn'), 'price': new.get('price'),
               'as_of': new.get('as_of'), 'ts': time.time()}
        self._last[ev.symbol] = cur

        outcome = {
            'ok': True,
            'event': ev.to_dict(),
            'symbol': ev.symbol,
            'market': ev.market,
            'trigger': _trigger_cn(ev),
            'old': old,
            'new': cur,
            'delta_pct': round(delta, 2) if delta is not None else None,
            'verdict_changed': flipped,
            'guard_flags': flags,
            'needs_review': needs_review,
            'summary': _summarize(ev, old, cur, delta, flipped),
            'ts': time.time(),
        }
        self._history.append(outcome)
        if len(self._history) > self._max_history:
            del self._history[:len(self._history) - self._max_history]
        return outcome

    # ---- 轮询扫描（供 scheduler 调用）----

    def scan(self, symbols: Iterable[Dict[str, Any]],
             fetch_fn: Callable[[str, str], Dict[str, Any]]) -> List[Dict[str, Any]]:
        """对比最新快照与上次快照，产出并处理行情/公告/经营数据事件。

        ``symbols`` 每项 ``{'symbol','market','announcements','deliveries'}``；
        ``fetch_fn(symbol, market)`` 返回最新行情 ``{'symbol','market','price','change_pct'}``。
        """
        events: List[Event] = []
        for item in symbols or []:
            sym = str(item.get('symbol') or '')
            mkt = str(item.get('market') or 'a')
            if not sym:
                continue
            old = self._snap.get(sym)
            try:
                snap = fetch_fn(sym, mkt) or {}
            except Exception as e:
                logger.warning('行情拉取失败 %s: %s', sym, e)
                snap = {}
            if snap:
                snap.setdefault('symbol', sym)
                snap.setdefault('market', mkt)
                q = detect_quote_event(old, snap)
                if q:
                    events.append(q)
                self._snap[sym] = snap
            events += detect_announcement_events(sym, mkt, item.get('announcements') or [])
            events += detect_delivery_events(sym, mkt, item.get('deliveries') or [])
        return self.ingest(events)

    # ---- 观测 ----

    def status(self) -> Dict[str, Any]:
        return dict(self._stats, tracked=len(self._last), history=len(self._history),
                    last_by_symbol={k: v for k, v in list(self._last.items())[:20]})

    def recent(self, n: int = 20) -> List[Dict[str, Any]]:
        return list(self._history[-max(1, n):])


# ==================== 文案 ====================


def _trigger_cn(ev: Event) -> str:
    if ev.kind == 'quote':
        p = ev.payload or {}
        if p.get('change_pct') is not None:
            return f"行情异动（单日 {float(p['change_pct']):+.2f}%）"
        return f"行情异动（较上次快照 {float(p.get('drift_pct') or 0):+.2f}%）"
    if ev.kind == 'announcement':
        p = ev.payload or {}
        return f"公告：{p.get('title', '')[:40]}"
    if ev.kind == 'delivery':
        p = ev.payload or {}
        return f"经营数据：{p.get('title', '')[:40]}"
    return ev.kind


def _summarize(ev: Event, old: Optional[Dict[str, Any]], new: Dict[str, Any],
               delta: Optional[float], flipped: bool) -> str:
    seg = [f"[{ev.symbol}] {_trigger_cn(ev)}"]
    if old and old.get('per_share') is not None and new.get('per_share') is not None:
        seg.append(f"每股内在价值 {old['per_share']} → {new['per_share']} 元"
                   + (f"（{delta:+.2f}%）" if delta is not None else ''))
    else:
        seg.append(f"每股内在价值 {new.get('per_share')} 元（首次）")
    if new.get('verdict_cn'):
        seg.append(f"结论：{new['verdict_cn']}")
    if flipped:
        seg.append("⚠️ 结论方向已变化")
    if new.get('verdict') in REVIEW_VERDICTS:
        seg.append("⚠️ 落入闸门，转人工复核")
    return '；'.join(seg)


def _f(v: Any) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None
