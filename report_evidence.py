# -*- coding: utf-8 -*-
"""report_evidence.py — 可解释性基座：每个结论都要能回答「凭什么、多可信、什么范围」

## 用户要求（2026-10-08 第 5 项）

- 每个结论后附**依据**和**置信度**；
- 估值结果给**区间**，不给单点；
- 显示**关键假设**，允许用户调整；
- 报告**版本化**，记录**数据截止日**。

## 设计原则

1. **置信度必须是可推导的**，不能是模型随口给的"我觉得挺准"。
   本模块只从四个客观维度算：输入完整度、结论离散度、数据来源层级、方法数量。
2. **区间优于单点**：估值结论一律携带 `range`，缺失即视为不合格结论。
3. **假设必须显式**：凡进入计算的外部参数都进 `assumptions`，并标 `adjustable=True`
   让上层能做"用户可调"的界面。
4. 报告元信息（版本 / 数据截止日）随结论一起产出，避免"报告不知道是哪天的数据"。

零项目依赖（仅标准库；来源层级复用 data_provenance 的分类）。
"""
import datetime
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 置信度档位
HIGH, MEDIUM, LOW = 'high', 'medium', 'low'
CONFIDENCE_CN = {HIGH: '高', MEDIUM: '中', LOW: '低'}
CONFIDENCE_ICON = {HIGH: '🟢', MEDIUM: '🟡', LOW: '🔴'}

# 结论类型 → 最低可接受置信度（低于此值必须显式降级提示）
_MIN_ACCEPTABLE = {HIGH: HIGH, MEDIUM: MEDIUM, LOW: LOW}

# 来源层级（与 data_provenance.SOURCE_TIERS 对齐）→ 置信度上限
_TIER_CEILING = {1: HIGH, 2: HIGH, 3: MEDIUM, 4: LOW, 5: LOW, 6: LOW}


def confidence(*, inputs_total: int = 0, inputs_missing: int = 0,
               dispersion_pct: float = None, source_tier: int = None,
               method_count: int = 1) -> Dict[str, Any]:
    """按四个客观维度推导置信度，并给出**为什么**。

    - 完整度：缺输入越多越低；
    - 离散度：多模型/敏感性区间跨度越大越低（>60% 直接判低）；
    - 来源层级：百科/未标注来源压低上限；
    - 方法数：单一方法（尤其单点 DCF）不给高置信度。

    返回 {'level','reasons','score'}；score 仅供排序与展示，不对外当概率。
    """
    reasons: List[str] = []
    score = 100.0

    if inputs_total:
        miss_ratio = max(0, inputs_missing) / inputs_total
        if miss_ratio > 0:
            score -= miss_ratio * 60
            reasons.append(f'输入缺失 {inputs_missing}/{inputs_total}')
    else:
        score -= 20
        reasons.append('未标注输入完整度')

    if dispersion_pct is not None:
        if dispersion_pct > 60:
            score -= 45
            reasons.append(f'区间跨度 {dispersion_pct:.0f}%（模型分歧过大，超过半个身位）')
        elif dispersion_pct > 30:
            score -= 18
            reasons.append(f'区间跨度 {dispersion_pct:.0f}%（有一定分歧）')

    if source_tier is not None:
        ceiling = _TIER_CEILING.get(int(source_tier), LOW)
        if ceiling == LOW:
            score = min(score, 45)
            reasons.append(f'数据来源层级偏低（第 {source_tier} 级）')
        elif ceiling == MEDIUM:
            score = min(score, 78)
            reasons.append(f'来源为第 {source_tier} 级（非公告/IR）')

    if method_count <= 1:
        score -= 25
        reasons.append('仅单一模型，未交叉验证')
    elif method_count == 2:
        score -= 8
        reasons.append('仅两种模型交叉')

    level = HIGH if score >= 80 else (MEDIUM if score >= 62 else LOW)
    if not reasons:
        reasons.append('输入完整、来源可靠、多模型互证')
    return {'level': level, 'score': round(score, 1), 'reasons': reasons}


def claim(text: str, *, evidence: List[str], conf: Dict[str, Any] = None,
          method: str = None, value_range: Any = None,
          unit: str = '', caveats: List[str] = None) -> Dict[str, Any]:
    """构造一条**可解释结论**：正文 + 依据 + 置信度 +（估值类必带）区间。

    `evidence` 为空视为不合格结论 —— 没有依据的判断不应出现在报告里。
    """
    ev = [str(e).strip() for e in (evidence or []) if str(e).strip()]
    c = conf or confidence(method_count=1)
    out = {
        'text': str(text or '').strip(),
        'evidence': ev,
        'confidence': c['level'],
        'confidence_cn': CONFIDENCE_CN.get(c['level'], c['level']),
        'confidence_icon': CONFIDENCE_ICON.get(c['level'], ''),
        'confidence_reasons': c.get('reasons') or [],
        'method': method,
        'range': _norm_range(value_range),
        'unit': unit,
        'caveats': [str(x) for x in (caveats or [])],
    }
    if not ev:
        out['caveats'].append('缺少依据，该结论不应作为决策输入')
    if out['range'] is None and method:
        out['caveats'].append('未提供区间，估值结论应给区间而非单点')
    return out


def _norm_range(v) -> Optional[Dict[str, Any]]:
    """接受 (low, high) / {'low','high'} / 单值；统一成 dict。"""
    if v is None:
        return None
    if isinstance(v, dict):
        lo, hi = v.get('low'), v.get('high')
    elif isinstance(v, (list, tuple)) and len(v) == 2:
        lo, hi = v[0], v[1]
    else:
        lo = hi = v
    if lo is None and hi is None:
        return None
    try:
        lo_f = float(lo) if lo is not None else None
        hi_f = float(hi) if hi is not None else None
    except (TypeError, ValueError):
        return {'low': lo, 'high': hi}
    if lo_f is not None and hi_f is not None and lo_f > hi_f:
        lo_f, hi_f = hi_f, lo_f
    span = None
    if lo_f and hi_f and lo_f > 0:
        span = round((hi_f - lo_f) / lo_f * 100, 1)
    return {'low': lo_f, 'high': hi_f, 'mid': round((lo_f + hi_f) / 2, 4)
            if (lo_f is not None and hi_f is not None) else None, 'span_pct': span}


def assumption(name: str, value: Any, *, basis: str = '', adjustable: bool = True,
               unit: str = '') -> Dict[str, Any]:
    """记录一个进入计算的关键假设，供用户在界面上调整。"""
    return {'name': name, 'value': value, 'unit': unit, 'basis': basis,
            'adjustable': bool(adjustable)}


def report_meta(*, as_of: str = None, version: str = None,
                data_sources: List[str] = None, generated_at: str = None) -> Dict[str, Any]:
    """报告元信息：**数据截止日**与**版本**（用户点名的两项）。"""
    now = generated_at or datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    return {
        'as_of': as_of or '未标注',                     # 数据截止日
        'version': version or _auto_version(now),
        'generated_at': now,
        'data_sources': [s for s in (data_sources or []) if s],
    }


def _auto_version(now: str) -> str:
    return 'v' + now.replace('-', '').replace(':', '').replace(' ', 'T')


def render(claims: List[Dict[str, Any]] = None, assumptions: List[Dict[str, Any]] = None,
           meta: Dict[str, Any] = None, title: str = '依据与置信度') -> str:
    """渲染为 Markdown。各部分为空则整段省略（不产出空标题）。"""
    claims = [c for c in (claims or []) if c]
    assumptions = [a for a in (assumptions or []) if a]
    if not claims and not assumptions and not meta:
        return ''
    lines: List[str] = []

    if claims:
        lines += [f'\n#### 📎 {title}', '']
        for c in claims:
            rng = c.get('range')
            head = f"- {c.get('confidence_icon', '')} **{c.get('confidence_cn', '')}置信度**"
            if c.get('method'):
                head += f"（{c['method']}）"
            lines.append(f"{head}：{c.get('text', '')}")
            if rng:
                lo, hi = rng.get('low'), rng.get('high')
                if lo is not None and hi is not None:
                    span = f"，跨度 {rng['span_pct']}%" if rng.get('span_pct') else ''
                    lines.append(f"  - 区间：{lo} ~ {hi}{c.get('unit') or ''}{span}")
            for e in c.get('evidence') or []:
                lines.append(f"  - 依据：{e}")
            for r in c.get('confidence_reasons') or []:
                lines.append(f"  - 置信度说明：{r}")
            for w in c.get('caveats') or []:
                lines.append(f"  - ⚠️ {w}")

    if assumptions:
        lines += ['', '#### 🔧 关键假设（可调整）', '',
                  '| 假设 | 取值 | 单位 | 依据 | 可调 |', '| --- | --- | --- | --- | --- |']
        for a in assumptions:
            lines.append('| {n} | {v} | {u} | {b} | {adj} |'.format(
                n=a.get('name'), v=a.get('value'), u=a.get('unit') or '—',
                b=a.get('basis') or '—', adj='✅' if a.get('adjustable') else '—'))

    if meta:
        lines += ['', f"> 📄 报告版本 **{meta.get('version')}** ｜ "
                      f"数据截止日 **{meta.get('as_of')}** ｜ 生成于 {meta.get('generated_at')}"]
        if meta.get('data_sources'):
            lines.append(f"> 数据来源：{'、'.join(meta['data_sources'])}")
    return '\n'.join(lines)


def gate(claims: List[Dict[str, Any]], min_level: str = MEDIUM) -> Dict[str, Any]:
    """结论质量闸门：整体置信度是否达到发布标准。

    单点、无依据、或低置信度的估值结论不得直接对外发布。
    """
    items = [c for c in (claims or []) if c]
    if not items:
        return {'ok': False, 'reason': '无可解释结论', 'blocked': []}
    order = {LOW: 0, MEDIUM: 1, HIGH: 2}
    blocked = [c for c in items if order.get(c.get('confidence'), 0) < order[min_level]]
    unspecified = [c for c in items if not c.get('evidence')]
    ok = not blocked and not unspecified
    reason = '结论质量达标' if ok else (
        f'{len(blocked)} 条置信度低于「{CONFIDENCE_CN[min_level]}」'
        + (f'，{len(unspecified)} 条缺依据' if unspecified else ''))
    return {'ok': ok, 'reason': reason, 'blocked': blocked, 'no_evidence': unspecified}