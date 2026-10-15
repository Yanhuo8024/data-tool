# -*- coding: utf-8 -*-
"""个性化投研：按**风险偏好 / 投资期限 / 投资目标**调整估值口径与输出。

三件事
------
1. **方法权重**：同一组模型给不同权重（保守偏 DCF/PB，积极偏 PE/PS），
   使 ``multi_model_valuation`` 的共识区间随用户画像移动。
2. **取档规则**：敏感性区间（悲观/中性/乐观）取哪一档作为"参考值"，以及**安全边际**。
3. **输出口径**：风险提示强度、措辞约束、必须声明的前提 —— 产出可直接拼进提示词的片段。

零项目依赖（仅标准库），供 ``ai_assistant`` / ``ai_agent`` 在构造提示词时调用。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ==================== 画像字典 ====================

RISK_LEVELS = ('conservative', 'balanced', 'aggressive')
RISK_CN = {'conservative': '保守型', 'balanced': '稳健型', 'aggressive': '积极型'}

HORIZONS = ('short', 'mid', 'long')
HORIZON_CN = {'short': '短期（<1 年）', 'mid': '中期（1–3 年）', 'long': '长期（>3 年）'}

GOALS = ('preservation', 'growth', 'speculation')
GOAL_CN = {'preservation': '本金保值', 'growth': '稳健增值', 'speculation': '博取高弹性'}

# 参考的估值模型键（与 multi_model_valuation 的模型名对齐）
METHOD_KEYS = ('dcf', 'pe', 'pb', 'ps', 'ev_ebitda', 'sotp')

# 风险偏好 → 基础权重（会再按期限/目标微调）
_BASE_WEIGHTS = {
    'conservative': {'dcf': 0.30, 'pb': 0.25, 'pe': 0.20, 'ev_ebitda': 0.15, 'ps': 0.05, 'sotp': 0.05},
    'balanced':     {'dcf': 0.25, 'pe': 0.20, 'pb': 0.15, 'ps': 0.15, 'ev_ebitda': 0.15, 'sotp': 0.10},
    'aggressive':   {'dcf': 0.15, 'pe': 0.25, 'ps': 0.25, 'sotp': 0.20, 'pb': 0.05, 'ev_ebitda': 0.10},
}

# 风险偏好 → 折现率调整（百分点）、安全边际、取哪一档敏感性
_RISK_TUNING = {
    'conservative': {'wacc_adj': +2.0, 'margin_of_safety': 0.30, 'range_pick': 'pessimistic'},
    'balanced':     {'wacc_adj': 0.0, 'margin_of_safety': 0.20, 'range_pick': 'neutral'},
    'aggressive':   {'wacc_adj': -1.0, 'margin_of_safety': 0.10, 'range_pick': 'optimistic'},
}

# 投资期限 → 权重再分配（长期看 DCF/CAGR，短期看当期 PE/股息）
_HORIZON_SHIFT = {
    'short': {'dcf': -0.05, 'pe': +0.05, 'ev_ebitda': +0.03, 'ps': -0.03},
    'mid': {},
    'long': {'dcf': +0.10, 'ps': -0.05, 'pe': -0.05},
}

# 投资目标 → 权重再分配
_GOAL_SHIFT = {
    'preservation': {'pb': +0.05, 'ev_ebitda': +0.03, 'ps': -0.08},
    'growth': {'ps': +0.03, 'sotp': +0.02, 'pb': -0.05},
    'speculation': {'ps': +0.05, 'sotp': +0.03, 'pb': -0.08},
}

# 风险提示强度
_RISK_NOTICE = {
    'conservative': ('strong', '必须在结论前显式声明下行风险与本金损失可能；'
                               '禁止使用"稳赚""必涨""抄底"等措辞；任何结论须附安全边际测算。'),
    'balanced':     ('normal', '结论后附风险提示与关键假设，说明结论在何种条件下会失效。'),
    'aggressive':   ('light', '结论后附简要风险提示；可讨论弹性与上行空间，但须同时给出回撤风险。'),
}


# ==================== 画像构建 ====================


def build_profile(*, risk: str = 'balanced', horizon: str = 'mid',
                  goal: str = 'growth', name: str = '',
                  constraints: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """构建用户投研画像。非法取值回落到默认并记 ``warnings``。"""
    warn = []
    if risk not in RISK_LEVELS:
        warn.append(f'未知风险偏好 {risk!r}，已回落到 balanced')
        risk = 'balanced'
    if horizon not in HORIZONS:
        warn.append(f'未知投资期限 {horizon!r}，已回落到 mid')
        horizon = 'mid'
    if goal not in GOALS:
        warn.append(f'未知投资目标 {goal!r}，已回落到 growth')
        goal = 'growth'

    weights = dict(_BASE_WEIGHTS[risk])
    for k, v in _HORIZON_SHIFT[horizon].items():
        weights[k] = weights.get(k, 0.0) + v
    for k, v in _GOAL_SHIFT[goal].items():
        weights[k] = weights.get(k, 0.0) + v
    weights = _normalize(weights)

    tuning = dict(_RISK_TUNING[risk])
    notice_level, notice = _RISK_NOTICE[risk]

    prof = {
        'name': name,
        'risk': risk, 'risk_cn': RISK_CN[risk],
        'horizon': horizon, 'horizon_cn': HORIZON_CN[horizon],
        'goal': goal, 'goal_cn': GOAL_CN[goal],
        'weights': weights,
        'wacc_adj': tuning['wacc_adj'],
        'margin_of_safety': tuning['margin_of_safety'],
        'range_pick': tuning['range_pick'],
        'notice_level': notice_level,
        'notice': notice,
        'constraints': dict(constraints or {}),
        'warnings': warn,
        'consistency': _consistency(risk, horizon, goal),
    }
    return prof


def _normalize(weights: Dict[str, float]) -> Dict[str, float]:
    """裁剪负权重 → 归一化到 1.0，保留 4 位小数。"""
    pos = {k: max(0.0, float(v)) for k, v in weights.items() if k in METHOD_KEYS}
    total = sum(pos.values()) or 1.0
    return {k: round(v / total, 4) for k, v in sorted(pos.items())}


def _consistency(risk: str, horizon: str, goal: str) -> Dict[str, Any]:
    """检出相互矛盾的组合（提示而非阻止，最终由用户决定）。"""
    issues = []
    if risk == 'conservative' and goal == 'speculation':
        issues.append('保守型偏好 + 博取高弹性：目标与风险承受度矛盾')
    if risk == 'conservative' and horizon == 'short':
        issues.append('保守型 + 短期：短期波动大，注意本金波动风险')
    if risk == 'aggressive' and goal == 'preservation' and horizon == 'short':
        issues.append('积极型 + 本金保值 + 短期：三者难以同时满足')
    return {'ok': not issues, 'issues': issues}


# ==================== 应用 ====================


def apply_to_sensitivity(sensitivity_range: Optional[Dict[str, Any]],
                         profile: Dict[str, Any]) -> Dict[str, Any]:
    """从敏感性区间中按画像取档，并施加安全边际。

    输入形如 ``{'pessimistic':44.9,'neutral':68.08,'optimistic':122.33}``。
    """
    rng = sensitivity_range or {}
    pick = profile.get('range_pick', 'neutral')
    base = rng.get(pick)
    if base is None:
        base = rng.get('neutral')
    mos = float(profile.get('margin_of_safety') or 0)
    out = {'pick': pick, 'base_value': base, 'margin_of_safety': mos,
           'entry_value': None}          # 键恒存在，缺档时为 None（调用方无需判键）
    if base is not None:
        # 安全边际：保守型要求价格低于该档价值一定比例才具吸引力
        out['entry_value'] = round(float(base) * (1 - mos), 4)
    return out


def pick_consensus(consensus: Optional[Dict[str, Any]],
                   profile: Dict[str, Any]) -> Dict[str, Any]:
    """按画像从多模型共识区间中选出"参考值"（加权中位/偏保守档）。"""
    c = consensus or {}
    q = 0.5
    if profile.get('range_pick') == 'pessimistic':
        q = 0.25
    elif profile.get('range_pick') == 'optimistic':
        q = 0.75
    lo, hi = c.get('low'), c.get('high')
    ref = None
    if lo is not None and hi is not None:
        ref = round(float(lo) + (float(hi) - float(lo)) * q, 4)
    elif c.get('median') is not None:
        ref = c['median']
    return {'quantile': q, 'reference_value': ref, 'binding': c.get('binding')}


def method_weights(profile: Dict[str, Any]) -> Dict[str, float]:
    return dict(profile.get('weights') or {})


# ==================== 输出口径 ====================


def prompt_fragment(profile: Dict[str, Any]) -> str:
    """生成可拼进系统提示词的个性化口径（中文、紧凑）。"""
    w = profile.get('weights') or {}
    ws = '、'.join(f'{k} {v:.0%}' for k, v in w.items() if v > 0)
    lines = [
        '# 投资者画像（须据此调整口径）',
        f"- 风险偏好：{profile.get('risk_cn')}　投资期限：{profile.get('horizon_cn')}"
        f"　目标：{profile.get('goal_cn')}",
        f"- 估值方法权重：{ws}（权重高者优先采信，冲突时以高权重模型为主）",
        f"- 敏感性取档：{profile.get('range_pick')}；折现率调整 {profile.get('wacc_adj'):+.1f} 个百分点；"
        f"安全边际 {profile.get('margin_of_safety'):.0%}",
        f"- 风险提示强度：{profile.get('notice_level')} —— {profile.get('notice')}",
    ]
    cons = (profile.get('consistency') or {}).get('issues') or []
    if cons:
        lines.append('- ⚠️ 画像一致性提示：' + '；'.join(cons) + '（已按用户设定执行，但请在回答中说明矛盾）')
    return '\n'.join(lines)


def describe(profile: Dict[str, Any]) -> str:
    """人可读的一段画像说明。"""
    w = method_weights(profile)
    top = sorted(w.items(), key=lambda kv: -kv[1])[:2]
    return (f"{profile.get('risk_cn')} / {profile.get('horizon_cn')} / {profile.get('goal_cn')}："
            f"优先采信 {'、'.join(f'{k}({v:.0%})' for k, v in top)}；"
            f"取 {profile.get('range_pick')} 档，安全边际 {profile.get('margin_of_safety'):.0%}；"
            f"风险提示 {profile.get('notice_level')}。")


# ==================== 从文本推断（便于前端一句话设置） ====================

_RISK_KW = {'保守': 'conservative', '稳健': 'balanced', '平衡': 'balanced',
            '积极': 'aggressive', '激进': 'aggressive', '进取': 'aggressive'}
_HORIZON_KW = {'短线': 'short', '短期': 'short', '中期': 'mid', '中长线': 'mid',
               '长线': 'long', '长期': 'long'}
_GOAL_KW = {'保值': 'preservation', '稳健': 'growth', '增值': 'growth',
            '成长': 'growth', '弹性': 'speculation', '投机': 'speculation'}


def infer_profile(text: str) -> Optional[Dict[str, Any]]:
    """从自然语言里推断画像（仅当命中关键词时返回，避免误setting）。"""
    t = str(text or '')
    risk = next((v for k, v in _RISK_KW.items() if k in t), None)
    horizon = next((v for k, v in _HORIZON_KW.items() if k in t), None)
    goal = next((v for k, v in _GOAL_KW.items() if k in t), None)
    if not (risk or horizon or goal):
        return None
    return build_profile(risk=risk or 'balanced', horizon=horizon or 'mid',
                         goal=goal or 'growth')
