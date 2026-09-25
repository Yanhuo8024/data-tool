#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""business_agent.py - 业务能力编排门面

把三条新增业务能力收敛为**唯一入口**，供对话链路调用：

| 能力 | 模块 | 作用点 |
|---|---|---|
| 知识库可溯源 | `kb_cite` | 回答补「📚 依据」+ 引用编号核验 |
| 智能运营 | `ops_assistant` | 客户咨询应答 / 产品品类匹配 / 流程引导 |
| 风控初审 | `risk_review` | 确定性合规规则 + 资料初筛 + 转人工标记 |

为什么要有这一层：`ai_assistant.py` / `routes/ai_assistant_routes.py` 都受架构守卫
R1 行数棘轮约束（余量 ≤ 8 行），接入点必须能用**一行替换**原有代码块。门面把
"剥离思维链 → 风控初审 → 引用补注"串成一个 `enrich_outbound()`，调用方一行完成。

`try_ops_reply()` 是运营能力的**前置分流**：命中则直接返回，不进入 LLM 链路。
"""
import logging
from typing import Any, Dict, List, Optional

import ops_assistant
import risk_review
from kb_cite import enhance_answer
from text_clean import strip_thinking

logger = logging.getLogger(__name__)

# 涉政策/税率/利率类知识点：答案需附时效性免责（原散落在 ai_assistant 分支里）
_TIMELY_CATEGORIES = ('tax', 'bank', 'finance', 'enterprise')
_TIMELY_DISCLAIMER = ('\n\n> ⚠️ 以上为通用知识，具体政策、税率、利率以官方最新发布为准。')


def kb_answer(answer: str, entries: List[Dict[str, Any]]) -> str:
    """知识库回答统一增强：补「📚 依据」块 + 引用核验 + 按分类附时效免责（幂等）。"""
    if not answer:
        return answer
    timely = any(
        str(e.get('category') or '') in _TIMELY_CATEGORIES
        for e in (entries or []) if isinstance(e, dict)
    )
    return enhance_answer(answer, entries,
                          disclaimer=_TIMELY_DISCLAIMER if timely else '')


def try_ops_reply(message: str, current_file: Optional[str] = None,
                  fast: bool = False) -> Optional[Dict[str, Any]]:
    """运营前置分流：命中返回完整回复体（不调 LLM），未命中返回 None。"""
    try:
        return ops_assistant.try_handle(message, current_file=current_file, fast=fast)
    except Exception as e:                                # 运营失败绝不影响主链路
        logger.warning('智能运营分流失败，交回主链路: %s', e)
        return None


def enrich_outbound(payload: Any, message: str = None,
                    current_file: str = None) -> Any:
    """对话出口统一后处理（同步 + SSE done 共用）。

    顺序：剥离思维链标签 → 风控初审（附规则结论与转人工标记）。
    非 dict 或非成功响应原样返回；已处理过的结果幂等。
    """
    if not isinstance(payload, dict):
        return payload
    out = payload
    reply = out.get('reply')
    if isinstance(reply, str):
        cleaned = strip_thinking(reply)
        if cleaned != reply:
            out = dict(out)
            out['reply'] = cleaned
    if out.get('ok') is False:
        return out
    try:
        return risk_review.attach(out, current_file=current_file,
                                  fact_stats=out.get('fact_audit'))
    except Exception as e:
        logger.warning('出口风控初审失败，返回原响应: %s', e)
        return out
