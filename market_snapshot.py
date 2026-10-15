#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""market_snapshot.py — 可比公司快照的**并行批量取数**

## 为什么需要这个模块

`company_analysis.get_competitors()` 原先对可比公司**串行**逐个取快照：

    for p in peers:            # PEERS_BY_CODE 常见 3 个
        sn = _snapshot(p)      # 实测单次 3.3–5.1s
    → 实测总耗时 30.06s（贵州茅台，2026-10-02 基线测量），是全管线最慢的一步。

`valuation.peer_comparison()` 早已用 `ThreadPoolExecutor` 并行取同级快照，
本模块把同一做法抽出来给竞品复用，并保持**返回顺序与入参顺序一致**
（"营收前 N" 的次序本身有信息量，不能因并发而乱）。

## 依赖方向

叶子模块：运行期惰性 `from valuation import _snapshot`（后者已带 TTL 缓存）。
**不在模块顶层 import valuation** —— 避免与 `valuation` 形成 import 期循环（触 R4）。
"""
from __future__ import annotations

import concurrent.futures as cf
import logging

logger = logging.getLogger(__name__)

# 并发度：akshare 内部对 V8（py_mini_racer）有全局锁，开太大只会互相等
MAX_WORKERS = 4


def snapshot_many(codes, max_workers: int = None) -> dict:
    """并行取多只股票快照，返回 `{code: snapshot_dict}`；失败的 code 不出现在结果里。"""
    codes = [str(c).strip() for c in (codes or []) if str(c).strip()]
    if not codes:
        return {}
    from valuation import _snapshot
    workers = max(1, min(int(max_workers or MAX_WORKERS), len(codes)))
    out = {}
    with cf.ThreadPoolExecutor(max_workers=workers, thread_name_prefix='snap') as ex:
        futs = {c: ex.submit(_snapshot, c) for c in codes}
        for c in codes:                       # 按入参顺序收集，保证顺序稳定
            try:
                sn = futs[c].result()
            except Exception as e:
                logger.debug("snapshot_many: %s 取数失败: %s", c, e)
                continue
            if isinstance(sn, dict):
                out[c] = sn
    return out


def competitor_rows(code: str, peers, name_of=None) -> list:
    """可比公司列表 `[{code, name}]`（并行取快照版）。

    `name_of` 为"代码 → 名称"的回退函数（通常是 `code_to_name`）；
    快照拿不到名称时用它兜底，再兜底才用代码本身作名称。
    取数失败**不丢条目**——宁可只有代码，也不要让竞品列表少一家。
    """
    codes = [str(p).strip() for p in (peers or []) if str(p).strip()]
    if not codes:
        return []
    snaps = snapshot_many(codes)
    rows = []
    for c in codes:
        nm = str((snaps.get(c) or {}).get('name') or '').strip()
        if not nm and callable(name_of):
            try:
                nm = str(name_of(c) or '').strip()
            except Exception as e:
                logger.debug("competitor_rows: name_of(%s) 失败: %s", c, e)
        rows.append({'code': c, 'name': nm or c})
    return rows
