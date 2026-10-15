#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""company_deep_llm.py — 企业深析的 LLM 输入构造 + 输出容错解析

## 为什么需要这个模块

`company_deep_pipeline._ai_draft()` 原先有两处结构性问题：

1. **送给 LLM 的 facts 缺 12 个字段**：只有 profitability/solvency/growth/dupont 与行情估值，
   **完全没有资产负债表与现金流量表**（assets/equity/idebt/cash/ocf/fcf/gw/arG/invG/
   inventory/payout/statements）。而提示词同时要求 LLM 写
   `conclusions.a3`「账靠不靠谱」、`model.weakest`「最脆弱一环」、`sources.audit`（审计排雷），
   并且**明令"禁止编造具体数字（数字只能来自输入 facts）"** →
   结构上自相矛盾，LLM 只能输出"需人工核实"一类套话。
   （`_extract_statements()` 的 docstring 本就写着"供企业深析流水线回填财务表单"，
   数据取到了却没送达。）

2. **JSON 解析全有或全无**：一次 `json.loads` 失败即整体降级为规则草稿，
   `max_tokens=2200` 要装 60+ 字段、尾部多一个逗号就静默降级。

本模块把 facts 构造、提示词、JSON 容错解析集中到一处，供 pipeline 调用。
`company_deep_pipeline` 已达 R1 行数基线（0 余量），故新增逻辑一律落在这里。

## 依赖方向

叶子模块：只依赖标准库。**不 import 项目内任何模块** → 不会造成循环 import（R4）。
"""
from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

# 送入 LLM 的资产负债表 / 现金流量表字段（原先全部缺失）
BS_KEYS = (
    'assets', 'equity', 'liabilities', 'cash', 'idebt', 'gw',
    'ocf', 'fcf', 'ar', 'inventory', 'arG', 'invG', 'gmD', 'payout',
)


def build_facts(ctx: dict) -> dict:
    """把各阶段已取到的真实数据整理成 LLM 的输入事实集（**含资产负债表**）。

    只放"有值"的项：None/'' 一律剔除，避免 LLM 把空值当作 0 参与推理。
    """
    ctx = ctx or {}
    profile = ctx.get('profile') or {}
    market = ctx.get('market') or {}
    ratios = (ctx.get('financial_analysis') or {}).get('ratios') or {}
    st = ctx.get('statements') or {}
    val = ctx.get('valuation_raw') or {}
    news = ctx.get('news') or []
    competitors = ctx.get('competitors') or []

    bs = {k: st[k] for k in BS_KEYS if st.get(k) is not None}
    facts = {
        'name': ctx.get('name'),
        'code': ctx.get('code'),
        'industry': profile.get('industry'),
        'market_cap_yi': profile.get('market_cap_yi') or market.get('market_cap_yi'),
        'profitability': ratios.get('profitability'),
        'solvency': ratios.get('solvency'),
        'growth': ratios.get('growth'),
        'operational': ratios.get('operational'),
        'dupont': ratios.get('dupont'),
        # ↓ 本次补入的 12 类资产/现金流字段（原实现完全未送）
        'balance_sheet_and_cashflow': bs or None,
        'report_date': st.get('report_date'),
        'pe': market.get('pe'),
        'pb': market.get('pb'),
        'price': market.get('price'),
        'high_52': market.get('high_52'),
        'low_52': market.get('low_52'),
        'dcf': val.get('dcf') or None,
        'industry_stats': (ctx.get('industry_stats_em') or None),
        'swot': ctx.get('swot'),
        'risks': (ctx.get('risks_rule') or [])[:6],
        'news_titles': [str(n.get('title') or n.get('标题') or '') for n in news[:6] if n],
        'competitors': competitors[:5],
    }
    # 剔除 None，减少 token 并避免 LLM 把"缺数据"当"值为 0"
    return {k: v for k, v in facts.items() if v not in (None, '', [], {})}


PROMPT_TEMPLATE = """你是资深卖方/买方研究员。基于以下真实数据与规则初稿，为「{name}」撰写企业深度分析字段。
要求：
1. 只输出 JSON，不要 markdown 代码围栏
2. 中文，客观、可核查，禁止编造具体数字（数字只能来自输入 facts）
3. 未知信息写「需人工核实」，不要瞎猜
4. 评分量纲**分两类，不可混用**：
   · `moat` 六维（tech/brand/channel/cost/network/license）为 **0-10 整数**（10=极强，0=无）
   · 其余评分（business.score / model.modelScore / model.pricing / gov.stable / gov.score）为 **1-5 整数**
5. 若 facts 的 `balance_sheet_and_cashflow` 存在，**必须**用它回答 a3（账靠不靠谱）
   与 sources.audit（审计排雷），并明确指出商誉/应收/存货/经营现金流的具体数字

JSON 结构：
{{
  "company": {{"oneliner": "一句话靠什么赚钱"}},
  "business": {{
    "what": "靠什么赚钱", "ceiling": "行业天花板", "kfs": "关键成功要素",
    "position": "行业位置", "lifecycle": "导入期|成长期|成熟期|衰退期",
    "policy": "强支持|中性|监管收紧|限制/淘汰", "score": 3,
    "size": "", "growth": "", "share": "", "cr3": "", "cr5": ""
  }},
  "model": {{
    "customer": "", "value": "", "channel": "", "revenue": "", "cost": "", "cash": "",
    "weakest": "最脆弱一环", "formula": "利润公式",
    "gm": "", "nm": "", "bizType": "品牌消费|平台/网络效应|订阅/SaaS|制造/代工|渠道分销|项目/工程|资源/周期|金融牌照|其他",
    "modelScore": 3, "pricing": 3, "payment": "先款后货|现款现货|信用期垫资|项目制回款"
  }},
  "moat": {{
    "tech": 6, "brand": 6, "channel": 6, "cost": 6, "network": 6, "license": 6,
    "shareTrend": "", "marginPeers": "", "whyChoose": "", "whyBlocked": "",
    "dur": "5|4|3|2|1"
  }},
  "gov": {{
    "controller": "需人工核实或从 facts 推断", "structure": "",
    "stable": 3, "score": 3
  }},
  "val": {{
    "pe": "", "pb": "", "verdict": "低估|合理|偏高|高估", "reason": ""
  }},
  "pb": {{
    "strength": "我方优势", "weak": "我方软肋", "rival": "最大对手",
    "move": "对手打法", "impact": "致命|重大|中等|有限",
    "conclusion": "守不守得住", "invest": "投资含义"
  }},
  "conclusions": {{
    "a1": "生意好不好", "a2": "公司强不强", "a3": "账靠不靠谱",
    "a4": "谁在管+风险", "a5": "价格值不值"
  }},
  "sources": {{"ipo": "", "mda": "", "audit": "", "research": ""}}
}}

facts = {facts}
"""


def build_prompt(name: str, facts: dict) -> str:
    """构造 AI 草稿提示词（moat 量纲 = 0-10，其余 1-5）。"""
    payload = json.dumps(facts, ensure_ascii=False, indent=1, default=str)
    return PROMPT_TEMPLATE.format(name=name or '该公司', facts=payload)


_FENCE_RE = re.compile(r'^\s*```(?:json)?\s*|\s*```\s*$', re.I)
_TRAILING_COMMA_RE = re.compile(r',\s*([}\]])')


def parse_json_block(text: str):
    """从 LLM 回复中稳健提取 JSON 对象；失败返回 None（由调用方决定是否重试）。

    容错点：
      · 剥离 ```/```json 代码围栏
      · 用花括号配平取**最外层**对象（不怕前后有解释性文字）
      · 容忍尾随逗号
      · 容忍字符串内的转义花括号
    """
    if not text:
        return None
    s = _FENCE_RE.sub('', str(text).strip()).strip()
    start = s.find('{')
    if start < 0:
        return None
    depth, in_str, esc, end = 0, False, False, -1
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                end = i
                break
    if end <= start:
        return None
    body = s[start:end + 1]
    for i, candidate in enumerate((body, _TRAILING_COMMA_RE.sub(r'\1', body))):
        try:
            data = json.loads(candidate)
        except (ValueError, TypeError) as e:
            logger.debug("parse_json_block: 第 %d 个候选解析失败: %s", i + 1, e)
            continue
        if isinstance(data, dict):
            return data
    return None


REPAIR_NUDGE = ("上一次回复不是合法 JSON。请**只输出一个 JSON 对象**，"
                "不要任何解释文字、不要 markdown 代码围栏、不要尾随逗号。")
