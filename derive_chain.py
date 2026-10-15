# -*- coding: utf-8 -*-
"""derive_chain.py — 衍生数字计算链（2026-10-10 用户 P0-2）

## 为什么需要

报告里「108%」这类数字被标"未核验"，实际它是**已核验数字算出来的**：
``108% ≈ 57 亿 ÷ 52.48 亿``，而 ``57 亿 = 1418.78 亿 × 4%``。
用户要求：整条计算链可追溯，标注「本系统计算」，而不是标未核验。

## 缺口根因

`number_sanity.derive_range` 只做单步除法且**生产链路从未调用**——
`stats['derived_data']` 恒为空，`fact_triplet._matches_derived` 从未生效。

## 本模块做法

- 输入：已核验 token 集（verified）+ 未核验 token 集（unverified）
- 运算规则（相对容差 1.5%）：
  - ``u = a / b``（u 带百分号时 = a/b×100）
  - ``u = a × b``；``u(带%) = a × p%``（p 需带百分号）
  - ``u = a + b`` / ``u = a − b``
- **链式**（深度 2）：第一轮推出的衍生值回池，再推第二轮，
  生成完整链文案 ``108% ≈ 57 ÷ 52.48（57 = 1418.78 × 4%）``
- 输出 fact_triplet.derived_data 兼容结构，正文照常渲染「本系统计算」节，
  未核验清单自动剔除这些数字。

零第三方依赖；年份 token 由调用方过滤（本模块再兜一层）。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_REL_TOL = 0.015          # 相对容差 1.5%
_MAX_DEPTH = 2            # 链式深度：已核验 → 衍生1 → 衍生2
_NUM = re.compile(r'(-?\d+(?:\.\d+)?)')
_YEAR = re.compile(r'(?:19|20)\d{2}')

# 每步运算：名称, 求值函数, 相对容差（加/减极易撞数，收紧到 0.5%）
_OPS: Tuple[Tuple[str, Any, float], ...] = (
    ('除', lambda a, b: a / b if b not in (0,) else None, _REL_TOL),
    ('乘', lambda a, b: a * b, _REL_TOL),
    ('加', lambda a, b: a + b, 0.005),
    ('减', lambda a, b: a - b if a > b else None, 0.005),
)


def _val(tok: str) -> Optional[float]:
    """token → float（剥逗号/单位/百分号；百分号单独用 _is_pct 判断）。"""
    s = str(tok or '').strip().replace(',', '')
    if not s or _YEAR.fullmatch(s.split('.')[0]):
        return None
    m = _NUM.search(s)
    return float(m.group(1)) if m else None


def _is_pct(tok: str) -> bool:
    return '%' in str(tok or '')


def _fmt(x: float) -> str:
    s = f'{x:,.2f}'.rstrip('0').rstrip('.')
    return s if s else '0'


def _try_ops(u: float, u_pct: bool, pool: List[Tuple[str, float, bool]]
             ) -> Optional[Dict[str, str]]:
    """在池中两两尝试运算，返回 {'formula','inputs'} 或 None。

    命中判定：
    - 目标带 %：``u ≈ r × 100``（如 108% ≈ 57÷52.48 的 1.086）
      或乘法形态 ``u ≈ a × b/100``（如 57 ≈ 1418.78 × 4%，b 需带百分号）
    - 目标不带 %：``u ≈ r``
    """
    n = len(pool)
    for i in range(n):
        ta, va, pa = pool[i]
        for j in range(i + 1, n):
            tb, vb, pb = pool[j]
            for op, fn, tol in _OPS:
                if op == '除' and vb == 0:
                    continue
                # 双向尝试（除/减不满足交换律）：va op vb 与 vb op va
                for ra, rb, la, lb in ((va, vb, ta, tb), (vb, va, tb, ta)):
                    if op == '除' and rb == 0:
                        continue
                    r = fn(ra, rb)
                    if r is None or r == 0:
                        continue
                    hit = False
                    label = ''
                    if op == '乘' and (pa or pb):
                        # a × p% 形态（57 = 1418.78 × 4%），目标带不带 % 都允许
                        rp = ra * rb / 100.0
                        if rp != 0 and abs(u - rp) / abs(rp) <= tol:
                            hit, label = True, f'{_fmt(ra)} × {_fmt(rb)}%'
                    if not hit and u_pct:
                        r100 = r * 100.0
                        if abs(u - r100) / abs(r100) <= tol:
                            hit, label = True, f'{_fmt(ra)} {op} {_fmt(rb)} × 100%'
                    if not hit and not u_pct:
                        hit = abs(u - r) / abs(r) <= tol
                        label = f'{_fmt(ra)} {op} {_fmt(rb)}'
                    if hit:
                        pct = '%' if u_pct else ''
                        return {'formula': f'{label} → {_fmt(u)}{pct}',
                                'inputs': f'{la} 、 {lb}'}
            continue
    return None


def compute(verified: List[str], unverified: List[str]) -> List[Dict[str, Any]]:
    """主入口：返回 fact_triplet 兼容的 derived_data 列表。

    链式深度 2：第一轮由已核验推出衍生值并回池，第二轮推出链上后续值。
    """
    vpool: List[Tuple[str, float, bool]] = []
    for t in (verified or []):
        v = _val(t)
        if v is not None and v != 0:
            vpool.append((str(t), v, _is_pct(t)))

    derived: List[Dict[str, Any]] = []
    hit: Dict[str, Dict[str, str]] = {}
    frontier = [t for t in (unverified or []) if _val(t) is not None]

    for _depth in range(_MAX_DEPTH):
        nxt: List[str] = []
        for tok in frontier:
            if tok in hit or tok in (verified or []):
                continue
            u, upct = _val(tok), _is_pct(tok)
            r = _try_ops(u, upct, vpool)
            if not r:
                nxt.append(tok)
                continue
            hit[tok] = r
            chain = r['formula']
            if _depth > 0:                      # 第二轮：把上游链拼上
                up = next((d for d in derived
                           if any(x in r['inputs'] for x in (str(d.get('value_text')),))), None)
                if up:
                    chain = f"{r['formula']}（{up.get('formula')}）"
            derived.append({
                'value_text': str(tok),
                'value_low': round(u, 4), 'value_high': round(u, 4),
                'out_unit': '%' if upct else '',
                'label': '衍生（本系统计算）',
                'formula': chain,
                'note': f"输入已核验/本系统推导：{r['inputs']}",
            })
            vpool.append((str(tok), u, upct))   # 回池供下一轮链式
        if not nxt or len(nxt) == len(frontier):
            break
        frontier = nxt
    if derived:
        logger.info('derive_chain 命中 %d 条衍生链', len(derived))
    return derived
