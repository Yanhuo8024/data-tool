#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
financial_knowledge.py - 金融业务约束层（A+B）

A. 金融业务规则库 loader：从 rules/financial_rules.json 加载"必守约束"
   （指标计算口径、交易日历、合规表述），作为不可违反的前置指令。
B. 金融术语-公式映射 + 负样本：召回前实体链接，命中术语就把对应公式作为
   硬约束拼进 Prompt，并注入"错误/过时口径"负样本，让模型规避。

纯本地、无网络；任何失败静默降级（不阻断主流程）。
"""

import re
import logging
from pathlib import Path
from typing import Dict, List, Any, Optional

logger = logging.getLogger(__name__)

_RULES_PATH = Path(__file__).parent / "rules" / "financial_rules.json"

# ---------- 术语 → 规则条目名 的链接关键字 ----------
TERM_KEYWORDS: Dict[str, List[str]] = {
    "夏普比率": ["夏普", "sharpe", "风险收益比"],
    "年化收益率": ["年化收益", "年化"],
    "最大回撤": ["回撤", "drawdown"],
    "卡玛比率": ["卡玛", "calmar"],
    "久期": ["久期", "duration", "利率敏感"],
    "CSF敏感度/信用利差": ["信用利差", "利差", "spread"],
    "DID处理效应": ["did", "双重差分", "政策评估", "试点"],
    "信息价值IV": ["iv", "信息价值"],
}

# ---------- 负样本：错误/过时口径（第三层"规避错误"）----------
NEGATIVE_SAMPLES: List[Dict[str, str]] = [
    {"错误": "用简单收益率(区间累计)冒充年化收益率", "正确": "必须按 (1+R)^(252/N)-1 折算成年化，并注明交易日/自然日口径"},
    {"错误": "把相关性当作因果关系直接下结论", "正确": "下因果结论前先做平行趋势检验 / 双重差分 / 控制变量，或明确说明相关非因果"},
    {"错误": "不同频率(日/月)的波动率、收益率口径混用直接比较夏普", "正确": "统一到同一频率与年化基准后再比较"},
    {"错误": "忽略现金流/分红，直接用未复权净值算收益", "正确": "复权(含分红再投)后再计算收益率与回撤"},
    {"错误": "样本只保留「存活」标的后回测，存在幸存者偏差", "正确": "应包含退市/清盘样本，避免高估平均表现"},
    {"错误": "预测给出确定性点位或承诺收益", "正确": "预测必须带置信区间与假设条件，且不承诺收益"},
]


def _safe_load_rules() -> Dict[str, Any]:
    try:
        if not _RULES_PATH.exists():
            return {}
        import json
        with open(_RULES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.debug("加载金融规则库失败: %s", e)
        return {}


_RULES = _safe_load_rules()


def link_terms(message: str) -> List[str]:
    """实体链接：识别消息中出现的金融术语，返回术语名列表"""
    low = message.lower()
    hits = []
    for term, kws in TERM_KEYWORDS.items():
        if any(k in low for k in kws):
            hits.append(term)
    return hits


def get_applicable_rules(message: str) -> Dict[str, Any]:
    """返回命中术语对应的规则条目 + 交易日历 + 合规表述"""
    terms = link_terms(message)
    meta = {}
    for item in _RULES.get("metric_calibration", []):
        if item.get("term") in terms:
            meta[item["term"]] = item
    return {
        "terms": terms,
        "metric_rules": meta,
        "calendar": _RULES.get("trading_calendar", []),
        "compliance": _RULES.get("compliance_phrases", []),
    }


def build_constraint_block(message: str) -> str:
    """
    构造"不可违反的前置指令"文本：命中术语 → 公式硬约束 + 负样本规避 + 合规表述。

    2026-09-22：无金融语境（寒暄/闲聊/非金融）**不得**注入交易日历与「必须自动附上
    免责」约束——此前 get_applicable_rules 对「你好」也返回 calendar/compliance，
    导致模型在问候时开始推演免责条款并把思考链写进回复。
    """
    rules = get_applicable_rules(message)
    financial = bool(rules["terms"]) or has_financial_msg(message)
    # 纯寒暄/极短社交语不注入任何约束
    low = (message or "").strip().lower()
    if low in ("你好", "您好", "hi", "hello", "嗨", "哈喽", "你好呀", "你好！"):
        return ""
    if not financial and not rules["terms"]:
        return ""
    parts = []
    if rules["terms"]:
        parts.append("# 金融必守约束（不可违反，违反则视为错误答案）")
    for term in rules["terms"]:
        item = rules["metric_rules"].get(term)
        if not item:
            continue
        parts.append(f"### {term}")
        parts.append(f"- 公式: {item.get('formula', '')}")
        notes = item.get("notes", "")
        if notes:
            parts.append(f"- 口径: {notes}")
        must = item.get("must", "")
        if must:
            parts.append(f"- 必须: {must}")
        parts.append("- 切勿: 使用简单/错误口径（见下）")
    if financial and rules["calendar"]:
        parts.append("### 交易日历口径（按市场区分）")
        for c in rules["calendar"]:
            parts.append(f"- {c.get('market', '')}: {c.get('note', '')}")
    if financial and rules["compliance"]:
        # 口径从「必须自动附上」改为「涉及投资/金融结论时附上」——寒暄不得逼模型纠结免责
        parts.append("### 合规与免责（仅在输出投资/金融结论时附上，闲聊/寒暄无需）")
        for p in rules["compliance"]:
            parts.append(f"- {p}")
    return "\n".join(parts)


def relevance_boost(message: str) -> float:
    """若命中金融术语，给 RAG / 实体链接一个加分（供召回排序使用）"""
    return min(1.5, 0.3 + 0.2 * len(link_terms(message)))


def match_negative_samples(message: str) -> List[Dict[str, str]]:
    """返回与问题相关的负样本（错误口径规避）"""
    low = message.lower()
    out = []
    # 简易相关性：出现"年化/收益/回撤/因果/预测"时提示对应负样本
    for s in NEGATIVE_SAMPLES:
        wrong = s.get("错误", "")
        if any(k in low for k in ["年化", "收益"]) and any(k in wrong for k in ["年化", "收益"]):
            out.append(s)
        if "回撤" in low and "回撤" in wrong:
            out.append(s)
        if ("因果" in low or "相关" in low) and ("因果" in wrong or "相关" in wrong):
            out.append(s)
        if "预测" in low and "预测" in wrong:
            out.append(s)
    return out[:3]


def has_financial_msg(message: str) -> bool:
    return bool(link_terms(message))


# ==================== D. 显式 Thinking 强思维链 + 金融专家角色 ====================

EXPERT_ROLE = (
    "你是一位具备 CFA 持证资格、拥有 10 年量化风控与投研经验的金融量化专家。"
    "你说话专业、有观点、有判断力，不模棱两可。"
    "所有涉及数值的结论**必须附带计算逻辑或假设**（用到的公式、输入口径、无风险利率、交易日数等），"
    "不得只报一个裸数字。"
)

THINKING_INSTRUCTIONS = """# 内部思维链（在脑中完成，不要写出来）
给出最终答案前，请在内部按四步拆解（"问题拆解 → 所需数据 → 分析框架 → 潜在陷阱"）。
**严禁**在回答中输出 <thinking> 标签、拆解步骤或任何思考过程——只输出最终答案本身。
1. 问题拆解：用户真正想要什么（问收益、风控还是持仓/评级？）想清楚再答。
2. 所需数据：回答需要哪些字段/指标；若缺数据，在答案中明确指出。
3. 分析框架：用哪套方法/公式（例：判断基金须用夏普、最大回撤、卡玛等指标，拒绝笼统评价）。
4. 潜在陷阱：时间口径（年化/非年化）、复权、交易日历、幸存者偏差、相关≠因果 等，用约束规避。

# 输出格式约束（严格执行）
- **禁用**"首先、其次、然后、最后、综上所述"这类模板化衔接词。
- 结论**前置**：开头直接给核心结论，再给依据。
- 优先使用**表格**组织对比数据；关键数字**加粗**。
- 涉及金融业务时，必须附上相关合规/免责表述（如"基金过往业绩不预示未来表现"）。
- 若基于后端提供的真实计算结果，必须如实引用真实数值；若未提供且无法计算，**明确说明无法提供实值，绝不编造数字**。"""


def build_expert_inference_block(financial: bool = True) -> str:
    """组装 D 层：专家角色 + 思维链 + 输出约束（作为 System 前置）"""
    return (EXPERT_ROLE + "\n\n" + THINKING_INSTRUCTIONS) if financial else THINKING_INSTRUCTIONS


# ==================== G. 场景化整体召回（第四层：按场景召回，保证上下文完整）====================

def recall_scenario_blocks(message: str, top_k: int = 3) -> str:
    """
    按"场景知识块"整体召回：命中金融场景(如基金/风控/固收/误区)时，
    返回对应场景知识全文作为上下文（而非零散片段），保证场景上下文完整。
    未命中金融场景返回空串。失败静默降级。
    """
    try:
        low = message.lower()
        if not any(k in low for k in ["基金", "回撤", "夏普", "固收", "债券", "久期", "利差",
                                      "风控", "评分卡", "woe", "iv", "psi", "did", "政策",
                                      "误区", "监管", "归因", "业绩", "净值"]):
            return ""
        from knowledge_rag import search_knowledge_scored
        scored = search_knowledge_scored(message, top_k)
        blocks = []
        for _, e in scored:
            name = str(e.get("name", ""))
            detail = e.get("detail") or e.get("desc") or ""
            # 只整体召回"场景/推理"知识块（含"场景""误区""知识"等标记，且内容较长）
            if any(k in name for k in ("场景", "误区", "口径", "框图", "知识")) or len(detail) > 200:
                blocks.append(f"## {name}\n{detail[:2500]}")
        if not blocks:
            return ""
        return "\n\n# 场景知识（整体召回，供分析使用）\n" + "\n\n".join(blocks)
    except Exception as e:
        logger.debug("场景召回失败（忽略）: %s", e)
        return ""