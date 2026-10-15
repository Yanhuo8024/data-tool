# -*- coding: utf-8 -*-
"""multi_model_valuation.py — 多模型交叉验证（禁止单点 DCF）

## 用户要求（2026-10-08 第 2 项）

> DCF 只能作为参考。建议同时跑：DCF、PE、PB、PS、EV/EBITDA、分部加总。
> 如果 DCF 结果与其他模型差异超过 50%，自动标记「模型冲突」。

## 为什么单点 DCF 不可信

DCF 对**增长率与折现率**极度敏感，且基数口径（净利润 vs 自由现金流）会系统性改变结果 ——
上一轮正是「单年增速 +106.77% 外推」把内在价值从 68 元拉到 313 元。
相对估值（PE/PB/PS/EV-EBITDA）锚定市场可比定价，能立刻暴露 DCF 的偏离。

本模块把六个模型都跑一遍，输出：
  - 各模型每股价值与口径说明；
  - **DCF vs 其余模型的背离度**，超阈值即标 `model_conflict`；
  - 共识区间（中位数 ± 离散度）与整体置信度。

结论经 `report_evidence` 产出，带依据与置信度。
"""
import logging
from statistics import median
from typing import Any, Dict, List, Optional

from report_evidence import claim, confidence

logger = logging.getLogger(__name__)

MODELS = ('DCF', 'PE', 'PB', 'PS', 'EV/EBITDA', 'SOTP')
CONFLICT_PCT = 50.0          # DCF 与其他模型差异阈值（用户指定）


def _f(v, d=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _per_share_pe(sn: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    eps, m = _f(sn.get('eps')), _f(sn.get('target_pe'))
    if eps is None or m is None:
        return None
    return {'value': round(eps * m, 2), 'formula': f'EPS {eps} × 目标 PE {m}',
            'basis': '权益'}


def _per_share_pb(sn: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    bps, m = _f(sn.get('bps')), _f(sn.get('target_pb'))
    if bps is None or m is None:
        return None
    return {'value': round(bps * m, 2), 'formula': f'BPS {bps} × 目标 PB {m}',
            'basis': '权益'}


def _per_share_ps(sn: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    rev_ps, m = _f(sn.get('revenue_per_share')), _f(sn.get('target_ps'))
    if rev_ps is None or m is None:
        return None
    return {'value': round(rev_ps * m, 2), 'formula': f'每股收入 {rev_ps} × 目标 PS {m}',
            'basis': '权益'}


def _per_share_ev_ebitda(sn: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ebitda, m, shares = _f(sn.get('ebitda')), _f(sn.get('target_ev_ebitda')), _f(sn.get('shares'))
    if not ebitda or m is None or not shares:
        return None
    ev = ebitda * m
    eq = ev - (_f(sn.get('net_debt'), 0.0) or 0.0)
    return {'value': round(eq / shares, 2),
            'formula': f'(EBITDA {ebitda} × {m} − 净负债 {sn.get("net_debt")}) / 股本 {shares}',
            'basis': 'EV 扣净负债'}


def _per_share_dcf(sn: Dict[str, Any], dcf: Dict[str, Any] = None) -> Optional[Dict[str, Any]]:
    """优先复用已算好的 DCF 结果（避免两套口径漂移）。"""
    if dcf and dcf.get('per_share') is not None:
        return {'value': round(_f(dcf['per_share']), 2),
                'formula': f"DCF（基数 {dcf.get('base_mode', 'net_profit')}，"
                           f"g1 {dcf.get('g1')}%，WACC {dcf.get('r')}%）",
                'basis': 'EV 扣净负债'}
    return None


def run_all(sn: Dict[str, Any], *, dcf: Dict[str, Any] = None,
            sotp: Dict[str, Any] = None) -> Dict[str, Any]:
    """并行跑六个模型的每股价值。缺输入的模型记入 `skipped`，不静默省略。"""
    raw = {
        'DCF': _per_share_dcf(sn, dcf),
        'PE': _per_share_pe(sn),
        'PB': _per_share_pb(sn),
        'PS': _per_share_ps(sn),
        'EV/EBITDA': _per_share_ev_ebitda(sn),
        'SOTP': ({'value': sotp['per_share'],
                  'formula': '分部加总 − 净负债',
                  'basis': '权益'}
                 if sotp and sotp.get('per_share') is not None else None),
    }
    results = {k: v for k, v in raw.items() if v and v.get('value') is not None}
    skipped = [k for k, v in raw.items() if v is None]
    return {'results': results, 'skipped': skipped, 'models_run': len(results)}


def cross_validate(bundle: Dict[str, Any], *, price: float = None,
                   conflict_pct: float = CONFLICT_PCT,
                   base_model: str = 'DCF') -> Dict[str, Any]:
    """以 `base_model`（默认 DCF）为基准，与其他模型两两比对，超阈值标冲突。"""
    results = (bundle or {}).get('results') or {}
    base = results.get(base_model)
    conflicts, comparisons = [], []
    if base and base.get('value'):
        b = _f(base['value'])
        for name, r in results.items():
            if name == base_model:
                continue
            v = _f(r.get('value'))
            if not v:
                continue
            dev = abs(v - b) / abs(b) * 100
            comparisons.append({'model': name, 'value': v, 'deviation_pct': round(dev, 1),
                                'conflict': dev > conflict_pct})
            if dev > conflict_pct:
                conflicts.append(f'{name}（{v}）与 {base_model}（{b}）差异 {dev:.1f}%')

    vals = [_f(r['value']) for r in results.values() if _f(r.get('value'))]
    consensus = None
    if vals:
        med = median(vals)
        lo, hi = min(vals), max(vals)
        span = (hi - lo) / lo * 100 if lo else None
        consensus = {'median': round(med, 2), 'low': round(lo, 2), 'high': round(hi, 2),
                     'span_pct': round(span, 1) if span is not None else None,
                     'models': len(vals)}
        if price:
            consensus['price'] = price
            consensus['covers_price'] = bool(lo <= price <= hi)
            consensus['vs_price_pct'] = round((med - price) / price * 100, 1) if price else None

    conflicts_flag = bool(conflicts)
    disp = consensus.get('span_pct') if consensus else None
    conf = confidence(inputs_total=len(MODELS),
                      inputs_missing=len((bundle or {}).get('skipped') or []),
                      dispersion_pct=disp,
                      method_count=len(vals),
                      source_tier=(bundle or {}).get('source_tier'))
    if conflicts_flag:
        conf['level'] = 'low' if conf['level'] != 'low' else conf['level']
        conf['reasons'] = list(conf.get('reasons') or []) + [f'{len(conflicts)} 个模型与基准冲突']

    rng = (consensus['low'], consensus['high']) if consensus else None
    _caveats: List[str] = []
    if conflicts_flag:
        _caveats.append('⚠️ 模型冲突：' + '；'.join(conflicts))
    if consensus and price and not consensus.get('covers_price'):
        _caveats.append('共识区间未覆盖现价，方向性结论需谨慎')
    c = claim(
        (f"六模型交叉：{len(vals)}/{len(MODELS)} 个可算，中位每股 {consensus['median']} 元"
         if consensus else '多模型交叉：无可用模型'),
        evidence=[f"{n}：{r['value']} 元（{r['formula']}）" for n, r in results.items()]
        + ([f"未纳入（缺输入）：{'、'.join(bundle['skipped'])}"] if bundle.get('skipped') else []),
        conf=conf, method='多模型交叉验证', value_range=rng, unit='元/股',
        caveats=_caveats or None)

    return {'comparisons': comparisons, 'conflicts': conflicts,
            'model_conflict': conflicts_flag, 'consensus': consensus,
            'claim': c, 'confidence': conf,
            'threshold_pct': conflict_pct, 'base_model': base_model}


def evaluate(sn: Dict[str, Any], *, dcf: Dict[str, Any] = None,
             sotp: Dict[str, Any] = None, price: float = None) -> Dict[str, Any]:
    """一步到位：跑全部模型 → 交叉验证 → 返回结论（供 agent 工具与报表直接调用）。"""
    bundle = run_all(sn, dcf=dcf, sotp=sotp)
    cross = cross_validate(bundle, price=price)
    return {**bundle, **cross, 'unit': '元/股'}