#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ops_assistant.py - 智能运营 Agent

券商 AI Agent 能力清单中的「智能运营 Agent」（客户咨询应答、流程引导、产品匹配、
信息推荐）在本项目中此前**完全缺失**——只有 `operation` 意图（"这个功能怎么用"）
的技术支持话术，没有任何面向客户业务的运营能力。

本模块提供四件事，全部走**确定性规则**（不交给 LLM 自由发挥，避免编造产品与费率）：
  1. `match_faq()`      客户咨询应答：标准问法 → 标准答案（`data/ops_faq.json`）；
  2. `match_products()` 产品匹配：客户属性 → **产品品类**打分（不下沉到具体产品，
     避免构成实质性投资建议），风险等级超限的品类**直接排除**而非降权；
  3. `guide_steps()`    流程引导：开户/申购/定投/赎回等业务分步指引；
  4. `recommend_next()` 信息推荐：依据当前上下文推荐下一步动作。

词表与目录数据在 `ops_catalog.py`（本文件受架构守卫 R1 行数约束，数据另置）。
对外统一入口 `try_handle()`：命中返回完整回复体，未命中返回 None 交回主链路。
**不抢分析任务**：命中分析动作词、或挂了文件时一律让路。
"""
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

from ops_catalog import (GUIDES, GUIDE_TRIGGERS, HORIZON_WORDS, LIQUIDITY_WORDS,
                         PRODUCT_CATALOG, RISK_WORDS)

logger = logging.getLogger(__name__)

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_FAQ_PATH = os.path.join(_DATA_DIR, "ops_faq.json")

_faq_cache: Optional[List[Dict[str, Any]]] = None
_faq_stamp: float = 0.0


# ==================== FAQ 库 ====================

def load_faq() -> List[Dict[str, Any]]:
    """加载运营应答库；文件缺失或损坏时返回空列表（运营能力降级，不阻断对话）。"""
    global _faq_cache, _faq_stamp
    try:
        stamp = os.path.getmtime(_FAQ_PATH)
    except OSError:
        return _faq_cache or []
    if _faq_cache is not None and stamp == _faq_stamp:
        return _faq_cache
    try:
        with open(_FAQ_PATH, encoding="utf-8") as f:
            data = json.load(f)
        entries = data.get("entries") if isinstance(data, dict) else data
        _faq_cache = [e for e in (entries or []) if isinstance(e, dict)]
        _faq_stamp = stamp
    except (OSError, ValueError) as e:
        logger.warning("运营应答库加载失败: %s", e)
        _faq_cache = _faq_cache or []
    return _faq_cache


def _faq_score(message: str, entry: Dict[str, Any]) -> int:
    """问法匹配打分：按命中别名的**长度**加权——长别名更具体 → 权重更高。

    这样"基金申购费"（6 字别名）会压过泛词"基金"（2 字），避免泛词误配。
    """
    q = str(message or "").lower()
    best = 0
    for alias in [entry.get("q", "")] + list(entry.get("aliases") or []):
        a = str(alias).strip().lower()
        if a and a in q:
            best = max(best, len(a))
    return best


def match_faq(message: str, threshold: int = 3) -> Optional[Dict[str, Any]]:
    """匹配运营应答条目；未达阈值返回 None。

    阈值 3 = 至少命中一个 3 字别名（"申购费""最大回撤"够，泛词"基金""净值"2 字不够），
    这样既能召回业务问法，又不会被泛词触发。
    并列最高分且分属不同条目 → 话题歧义，不硬猜，交回主链路。
    """
    entries = load_faq()
    if not entries or not message:
        return None
    scored = [(_faq_score(message, e), i, e) for i, e in enumerate(entries)]
    scored = [t for t in scored if t[0] >= threshold]
    if not scored:
        return None
    scored.sort(key=lambda t: (-t[0], t[1]))
    best_score, _, entry = scored[0]
    if len(scored) > 1 and scored[1][0] == best_score:
        return None
    return entry


def render_faq(entry: Dict[str, Any]) -> str:
    """把 FAQ 条目渲染为带合规尾注的回复。"""
    body = str(entry.get("answer") or "").strip()
    if entry.get("risk"):
        body += ("\n\n> ⚠️ 以上为通用业务口径，不构成投资建议；具体产品条款与费率以基金合同、"
                 "产品说明书及官方最新公告为准。")
    return body


# ==================== 客户画像 ====================

_AMOUNT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(万|千|k|w|元|块)?", re.IGNORECASE)


def parse_profile(message: str) -> Dict[str, Any]:
    """从自然语言中抽取客户属性（风险上限 / 期限 / 金额 / 流动性诉求）。

    `signals` 表示抽到的属性个数——属性太少时不做产品匹配（无依据的推荐比不推荐更糟）。
    """
    msg = str(message or "")
    profile: Dict[str, Any] = {
        'risk_cap': None, 'horizon_months': None, 'amount': None,
        'liquidity': False, 'signals': 0,
    }
    for words, cap in RISK_WORDS:
        if any(w in msg for w in words):
            profile['risk_cap'] = cap
            profile['signals'] += 1
            break
    for words, months in HORIZON_WORDS:
        if any(w in msg for w in words):
            profile['horizon_months'] = months
            profile['signals'] += 1
            break
    m = _AMOUNT_RE.search(msg)
    if m and m.group(1):
        try:
            val = float(m.group(1))
            unit = (m.group(2) or '').lower()
            val = val * 10000 if unit in ('万', 'w') else val * 1000 if unit in ('千', 'k') else val
            if 10 <= val <= 100000000:
                profile['amount'] = val
                profile['signals'] += 1
        except (TypeError, ValueError):
            logger.debug('金额解析跳过: %r', m.group(0))
    if any(w in msg for w in LIQUIDITY_WORDS):
        profile['liquidity'] = True
        profile['signals'] += 1
    return profile


def match_products(profile: Dict[str, Any], top_k: int = 3) -> Dict[str, Any]:
    """按客户属性对产品**品类**打分。

    硬约束（安全优先，不做"降权保留"）：风险等级高于客户承受上限、或起投金额高于
    客户资金的品类**直接排除**。返回 {'matched': [...], 'excluded': [(prod, 原因)]}。
    """
    cap = profile.get('risk_cap')
    amount = profile.get('amount')
    horizon = profile.get('horizon_months')
    matched, excluded = [], []
    for p in PRODUCT_CATALOG:
        if cap is not None and p['risk'] > cap:
            excluded.append((p, f"风险等级 R{p['risk']} 高于你的承受上限的 R{cap}"))
            continue
        if amount is not None and p['min_amount'] > amount:
            excluded.append((p, f"起投 {p['min_amount']:,} 元高于你的可投金额"))
            continue
        score, reasons = 0, []
        if cap is not None:
            score += 3 if p['risk'] == cap else 2
            reasons.append(f"风险等级 R{p['risk']} 在你可承受范围内")
        if horizon is not None:
            if p['hmin'] <= horizon <= p['hmax']:
                score += 3
                reasons.append(f"适宜持有期覆盖你的 {horizon} 个月计划")
            else:
                score -= 2
        if profile.get('liquidity'):
            score += p['liquidity']
            if p['liquidity'] >= 4:
                reasons.append("流动性好，可随时或快速取用")
        if amount is not None and p['min_amount'] <= amount:
            score += 1
        if cap is None and horizon is None:
            score += p['risk'] <= 2          # 属性不足 → 保守偏置
        matched.append({'prod': p, 'score': score, 'reasons': reasons})
    matched.sort(key=lambda x: (-x['score'], x['prod']['min_amount']))
    return {'matched': matched[:top_k], 'excluded': excluded}


def render_product_match(profile: Dict[str, Any], result: Dict[str, Any]) -> str:
    """把匹配结果渲染为运营应答（含排除说明与合规边界）。"""
    lines = ["#### 🎯 产品类型匹配（按你的条件筛出的**品类**）", ""]
    conds = []
    if profile.get('risk_cap'):
        conds.append(f"风险承受 R{profile['risk_cap']}")
    if profile.get('horizon_months'):
        conds.append(f"计划持有 ≈{profile['horizon_months']} 个月")
    if profile.get('amount'):
        conds.append(f"可投金额 ≈{profile['amount']:,.0f} 元")
    if profile.get('liquidity'):
        conds.append("要求流动性好")
    lines.append("**你的条件**：" + ("、".join(conds) if conds else "（信息较少，按保守偏置给出）"))
    lines.append("")
    if not result['matched']:
        lines.append("按当前条件没有可匹配的品类——通常因为风险上限与期限要求互相冲突，"
                     "或可投金额低于所有品类的起投门槛。")
    for i, item in enumerate(result['matched'], 1):
        p = item['prod']
        lines.append(f"**{i}. {p['name']}**（R{p['risk']}，起投 {p['min_amount']:,} 元，"
                     f"预期 {p['expected']}）")
        lines.append(f"- {p['desc']}")
        if item['reasons']:
            lines.append("- 匹配理由：" + "；".join(item['reasons']))
        lines.append("")
    if result['excluded']:
        lines.append("**以下品类已按风险/门槛排除**（不做降权保留）：")
        for p, why in result['excluded'][:4]:
            lines.append(f"- {p['name']}：{why}")
        lines.append("")
    lines.append("> ⚠️ 以上仅按**产品类型**做条件匹配，不推荐任何具体产品，也不构成投资建议。"
                 "购买前需完成风险测评，并以基金合同/产品说明书的条款为准。")
    return "\n".join(lines)


# ==================== 流程引导与信息推荐 ====================

def guide_steps(message: str) -> Optional[Dict[str, Any]]:
    """业务流引导匹配（客户业务流，不是站内功能操作）。"""
    msg = str(message or "")
    for key, triggers in GUIDE_TRIGGERS.items():
        if any(t in msg for t in triggers):
            return GUIDES.get(key)
    return None


def render_guide(guide: Dict[str, Any]) -> str:
    lines = [f"#### 🧭 {guide['title']}", ""]
    for i, s in enumerate(guide['steps'], 1):
        lines.append(f"{i}. {s}")
    if guide.get('tab'):
        lines.append("")
        lines.append(f"> 相关功能可在本站「**{guide['tab']}**」标签页找到。")
    return "\n".join(lines)


def recommend_next(message: str, current_file: Optional[str] = None) -> List[str]:
    """依据上下文推荐下一步动作（追加在回复末尾，不单独成答）。"""
    msg = str(message or "")
    if current_file:
        return [f"对 `{current_file}` 直接说「描述统计 + 相关矩阵热力图」，我会调用真实工具出结果",
                "说「训练模型」或「做信用评分卡」可进入建模链路"]
    if any(k in msg for k in ('股票', '估值', '行业', '公司', '财报')):
        return ["补充股票名称或代码，我走估值与基本面链路（PE/PB/ROE/杜邦分解）"]
    if any(k in msg for k in ('基金', '理财', '债券', '产品')):
        return ["告诉我风险偏好、计划持有期与可投金额，我做产品类型匹配"]
    return ["上传 CSV/Excel 后可直接做分析与建模；不传数据也能查行情、宏观与固定收益知识"]


# ==================== 统一入口 ====================

# 分析动作词：命中即让路（运营话术不得抢分析任务）
_ANALYSIS_WORDS = (
    '分析', '训练', '预测', '建模', '回测', '画图', '生成图表', '清洗', '筛选',
    '计算', '回归', '聚类', '降维', '跑模型', '拉数据', '做因子', '出报告', '导出',
)


def try_handle(message: str, current_file: Optional[str] = None,
               fast: bool = False) -> Optional[Dict[str, Any]]:
    """运营能力统一入口：命中返回回复体，未命中返回 None。

    优先级：流程引导 > FAQ 应答 > 产品匹配（后者需 ≥2 个属性信号，避免"基金"这类
    泛词触发无依据推荐）。
    """
    msg = str(message or "").strip()
    if not msg or len(msg) > 200:
        return None
    # 让路规则：挂文件或含明确分析动作时，运营不抢答
    if current_file or any(w in msg for w in _ANALYSIS_WORDS):
        return None

    guide = guide_steps(msg)
    entry = match_faq(msg) if not guide else None
    reply, channel = "", ""
    if guide:
        reply, channel = render_guide(guide), "ops_guide"
    elif entry:
        reply, channel = render_faq(entry), "ops_faq"
    else:
        profile = parse_profile(msg)
        if profile.get('signals') >= 2:
            result = match_products(profile)
            if result['matched'] or result['excluded']:
                reply = render_product_match(profile, result)
                channel = "ops_product_match"
    if not reply:
        return None
    tips = recommend_next(msg, current_file)
    if tips:
        reply += "\n\n#### 💡 下一步可以做\n" + "\n".join(f"- {t}" for t in tips)
    return {
        'ok': True,
        'reply': reply,
        'source': channel,
        'llm_synthesized': False,
        'intent': {'action': 'operation', 'params': {}, 'confidence': 0.9, 'raw_text': ''},
    }
