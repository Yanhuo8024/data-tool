# -*- coding: utf-8 -*-
"""segment_tool.py — 把分部（主营构成）数据注册为 Agent 工具

接入方式说明（重要）：本模块**不 import agent_tool_registry**，
而是由调用方把 `registry` 实例传进来（`register_segment_tools(registry)`）。
这样既能把工具挂在同一注册表上，又不会产生 `agent_tool_registry ⇄ segment_tool`
循环 import（arch_guard R6 会拦，且循环 import 在冷启动路径上是真实风险）。

数据能力见 segment_data.py；来源分级与溯源戳见 data_provenance.py。
"""
import logging

from segment_data import SEGMENT_FIELDS, fetch_segments, render_segments

logger = logging.getLogger(__name__)

DESCRIPTION = (
    'Query a company\'s segment (business-line) breakdown from the financial-report notes: '
    'revenue, YoY, revenue share, gross margin and segment profit per business line '
    '(e.g. smartphone / IoT / internet services / EV). '
    'Call this whenever the user asks about revenue mix, revenue share, 收入占比, 收入结构, '
    '分部收入, or which business drives growth — do NOT reply "not included". '
    'A-share codes return real data; HK codes return an explicit gap with the exact source to consult. '
    'Params: code (e.g. 600519 or HK01810), optional market ("a"/"hk"), optional category (按产品分类/按地区分类/按行业分类).'
)

PARAMS = ['code', 'market', 'category']


def tool_segment_breakdown(code: str = None, market: str = None, category: str = None) -> str:
    """分部（主营构成）查询：收入 / 同比 / 占比 / 毛利率 / 经营利润。"""
    if not code:
        return ('Error: 请提供股票代码（A 股 6 位如 600519，港股 HK+4 位如 HK01810）。'
                f'需要字段：{" / ".join(SEGMENT_FIELDS)}')
    try:
        data = fetch_segments(str(code), market=market,
                              category=category or '按产品分类')
    except Exception as e:                       # 外部接口异常不应中断对话
        logger.warning('分部数据查询失败 %s: %s', code, e)
        return f'Error: 分部数据查询失败（{type(e).__name__}）'
    out = render_segments(data)
    if not data.get('ok'):
        return ('【分部数据缺口】' + out.replace('\n#### 🧩 ', '\n')
                + '\n\n请把上述缺口与获取路径**明确告知用户**，不要回答「暂未包含」。')
    return out


def register_segment_tools(registry) -> None:
    """把分部数据工具注册到传入的注册表实例。"""
    registry.register('segment_breakdown', tool_segment_breakdown, DESCRIPTION, PARAMS)
