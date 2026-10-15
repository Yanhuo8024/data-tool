# -*- coding: utf-8 -*-
"""agent_tool_registry.py — 工具注册表 + 工具实现（从 ai_agent 拆出）"""
from __future__ import annotations

import ast as _ast
import hashlib
import json
import logging
import math as _math
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from agent_tool_runtime import (
    ToolCache,
    ToolTimeoutError,
    _call_tool_with_timeout,
    _file_fingerprint,
    _tool_ttl,
    tool_cache,
)

logger = logging.getLogger(__name__)

from egress_policy import tool_allowed as _tool_allowed, tool_disabled_message as _egress_off  # noqa: E402
from egress_policy import (tool_side_effect_allowed as _side_effect_allowed,
                           tool_side_effect_message as _side_effect)  # noqa: E402
from tool_boundary import *  # noqa: F401,F403  # 若有相关守卫

# ---- 运行期符号（避免循环导入，调用方再注入）----
_memory_manager = None
get_long_term = None
_begin_run = None
_trace_event = None
_end_run = None
_trace_file = None
llm_client = None
_parse_calls = None
_lg_emit_usage = None
_extract_json_objects = None

def bind_runtime(**kw):
    """由 ai_agent 注入跨模块符号，保持工具实现零改动。"""
    g = globals()
    for k, v in kw.items():
        g[k] = v

# 供测试 monkeypatch / 外部引用
from agent_fact_audit import _audit_fact_sources, _real_tool_evidence, _verified_number_whitelist

# ==================== Tool Registry ====================

class ToolRegistry:
    """Registry of available tools for the agent"""
    
    def __init__(self):
        self._tools: Dict[str, Dict] = {}
    
    def register(self, name: str, func: Callable, description: str, params: List[str]):
        """Register a tool"""
        self._tools[name] = {
            'func': func,
            'description': description,
            'params': params
        }
    
    def _visible(self, name: str) -> bool:
        """被任一闸门禁用的工具，对 prompt 与分发都不可见（与 get_tool 共用同一判定）"""
        return (_tool_allowed(name) and _side_effect_allowed(name)
                and name in self._tools)

    def get_tool(self, name: str) -> Optional[Dict]:
        """取工具。两种拦截共用**同一处收口**：

        1. 「数据出境」方向（`_tool_allowed`）——东西能不能往外发；
        2. 「工具副作用」方向（`_side_effect_allowed`）——工具本身动没动副作用。
        任一拦截即返回只读桩：不进 prompt，误调用时给出明确可读的原因。

        dsh 把权限做成独立插件层；本仓库工具总量小且单仓，两向闸门在此收口即可，
        不引入额外抽象（见 arch_guard 的行数棘轮与依赖方向约束）。
        """
        if not _tool_allowed(name):
            return {'func': lambda **_kw: _egress_off(name), 'description': '', 'params': []}
        if not _side_effect_allowed(name):
            return {'func': lambda **_kw: _side_effect(name), 'description': '', 'params': []}
        return self._tools.get(name)

    def list_tools(self, names: Optional[List[str]] = None) -> List[Dict]:
        """List available tools. names 非空时按白名单过滤（保持 names 给出的顺序）；
        出境策略或副作用策略禁用的工具一律不列出——与 get_tool 共用同一闸门，
        防止"看着拦住了其实没拦"。"""
        keys = [n for n in (names or self._tools) if self._visible(n)]
        return [{'name': n, 'description': self._tools[n]['description'],
                 'params': self._tools[n]['params']} for n in keys]
    def get_tools_prompt(self, names: Optional[List[str]] = None) -> str:
        """Generate tool descriptions for LLM prompt

        names 非空时只描述白名单工具 —— 多Agent管线按阶段职责裁剪工具集，
        使每轮迭代重发的 tool 描述大幅缩短（实测 32 个工具 ≈ 5.7k 字符）。
        """
        lines = ["## Available Tools"]
        for tool in self.list_tools(names):
            params = ', '.join([f'<{p}>' for p in tool['params']])
            lines.append(f"- `{tool['name']}`: {tool['description']} (params: {params})")
        return '\n'.join(lines)

# Global registry
registry = ToolRegistry()

def register_tool(name: str, description: str, params: List[str]):
    """Decorator to register a tool"""
    def decorator(func):
        registry.register(name, func, description, params)
        return func
    return decorator

# ==================== Call Parser ====================
# 工具调用解析（parse_calls / _extract_json_objects / _extract_reasoning）与工具 JSON 净化
# （_strip_tool_json / _clean_final_output）已迁至 agent_text_protocol.py，见文件头 re-export。

# ==================== 原生 Function Calling & 参数 Schema 校验（P1a） ====================
# 支持 OpenAI 兼容 tools 参数的 provider 走原生工具调用路径；其余回退正则解析兜底。
_NATIVE_TOOL_PROVIDERS = {'openai', 'deepseek', 'groq', 'zhipu', 'dashscope', 'sensenova'}
# 开关：设置 AGENT_FORCE_NATIVE_TOOLS=0 可关闭原生路径（退回纯正则解析）
_NATIVE_TOOLS_ENABLED = os.environ.get('AGENT_FORCE_NATIVE_TOOLS', '1') != '0'
# P1-6：按模型自适应降级（连续 N 次带 tools 却未返回 tool_calls → 该模型后续不再下发 tools）
_NATIVE_AUTO_DEGRADE = os.environ.get('AGENT_NATIVE_AUTO_DEGRADE', '1') != '0'
_NATIVE_MISS_THRESHOLD = max(1, int(os.environ.get('AGENT_NATIVE_MISS_THRESHOLD', '3')))
_native_miss: Dict[str, int] = {}
_native_disabled: set = set()
_native_guard = threading.Lock()

def _native_model_key(provider: str, model: str) -> str:
    return f"{(provider or '').lower()}/{(model or '').lower()}"

# 兜底跳步提示（P1a）：原生/正则都未解析到工具调用时，最多强制重试一次引导再调用
NO_CALLS_NATIVE_RETRY = (
    "你刚才的回答中没有包含任何工具调用（tool_calls / {'tool':...} JSON）。\n"
    "请重新输出一次：必须真正调用工具获取数据后再回答。若确无可用工具，请明确说明。"
)

def _first_round_retry_prompt(use_native: bool, has_file: bool) -> str:
    """首轮零工具时的重试提示。

    修复（2026-09-18）：原生协议 + 有数据文件时误用通用 NO_CALLS_NATIVE_RETRY，
    未点名 columns/describe 等文件工具，导致「有文件却零工具」告警偏高。
    有文件时一律用文件感知提示，并在原生路径追加调用格式约束。
    """
    if has_file:
        base = NO_TOOLS_PROMPT
        if use_native:
            return base + (
                "\n【调用格式】本轮必须返回真正的 tool_calls（或以 { 开头的 JSON 工具调用），"
                "不要只用文字描述要做什么。")
        return base
    return NO_CALLS_NATIVE_RETRY if use_native else NO_TOOLS_NO_FILE_PROMPT

def _provider_native_tools(provider: str, model: str = None) -> bool:
    """判断 provider 是否走原生 function calling 路径。

    P1-6：增加**按模型自适应降级**。轨迹统计显示 glm-4.7-flash 等在 327 次携带
    tools 的调用中仅 4 次真正返回 tool_calls（1.2%），其余靠文本协议兜底 + 一次
    额外重试。对连续多次未采纳原生协议的模型自动关闭 tools 下发，省掉 schema token
    与重试往返（可用 AGENT_NATIVE_AUTO_DEGRADE=0 关闭该行为）。
    """
    if not _NATIVE_TOOLS_ENABLED or (provider or '').lower() not in _NATIVE_TOOL_PROVIDERS:
        return False
    if _NATIVE_AUTO_DEGRADE and model:
        with _native_guard:
            if _native_model_key(provider, model) in _native_disabled:
                return False
    return True

def _note_native_miss(provider: str, model: str, trace_id: str = None) -> bool:
    """记一次「带 tools 但未返回 tool_calls」；累计达标则对该模型自动降级。返回是否刚触发降级。"""
    if not _NATIVE_AUTO_DEGRADE:
        return False
    key = _native_model_key(provider, model)
    if not key or key.endswith('/'):
        return False
    triggered = False
    with _native_guard:
        _native_miss[key] = _native_miss.get(key, 0) + 1
        if _native_miss[key] >= _NATIVE_MISS_THRESHOLD and key not in _native_disabled:
            _native_disabled.add(key)
            triggered = True
    if triggered:
        logger.info("模型 %s 连续 %d 次未采纳原生 tool_calls，自动降级为文本工具协议",
                    key, _NATIVE_MISS_THRESHOLD)
        if trace_id:
            try:
                _trace_event(trace_id, 'native_tools_degraded',
                             {'model': key, 'misses': _NATIVE_MISS_THRESHOLD})
            except (Exception, SystemExit) as _e:
                logger.debug("agent_tool_registry.py:207 swallowed: %s", _e)
    return triggered


# 工具参数元数据（覆盖默认「全 string、全必填」的 schema 缺陷）。
# 说明：type ∈ str|integer|number|boolean|array; required 只标真正必填项;
# description: 参数含义/格式示例，帮助模型正确填参。
# file 类参数默认可选（后端在已打开文件时会自动注入），避免模型被迫编造文件名。
_TOOL_PARAM_SCHEMA: dict = {
    'web_search': {'query': {'type': 'string', 'required': True, 'description': '联网搜索关键词（具体、含时间背景更佳）'}},
    'file_rag': {'query': {'type': 'string', 'required': True, 'description': '针对已上传数据文件数据的检索问题'}},
    'run_skill': {'skill_name': {'type': 'string', 'required': True, 'description': '技能名称（用 list_skills 查看）'},
                  'task': {'type': 'string', 'required': True, 'description': '交给该技能的待分析任务/问题'}},
    'run_analysis_api': {'endpoint': {'type': 'string', 'required': True, 'description': '后端分析接口路径，如 /api/ts/arima（用 list_analysis_api 查看）'},
                         'params': {'type': 'string', 'required': False, 'description': '接口参数，JSON 字符串，如 {"periods":12}'},
                         'file': {'type': 'string', 'required': False, 'description': '数据文件（后端自动注入时可省略）'}},
    'group_compare': {'outcome': {'type': 'string', 'required': True, 'description': '结果变量列名'}},
    'did_regression': {'dep_var': {'type': 'string', 'required': True, 'description': '被解释变量列名'}},
    'head': {'file': {'type': 'string', 'required': False, 'description': '待展示的数据文件（后端自动注入时可省略）'},
             'n': {'type': 'integer', 'required': False, 'description': '展示前 N 行，默认 5'}},
    'trend': {'column': {'type': 'string', 'required': True, 'description': '纵轴指标的列名'}},
    'hypothesis_test': {'test': {'type': 'string', 'required': True, 'description': '检验类型：t_test/anova/chi_square/mannwhitney/ks 等',
                                 'enum': ['t_test', 'anova', 'chi_square', 'mannwhitney', 'ks']},
                        'col1': {'type': 'string', 'required': True, 'description': '第一个变量/数值列'},
                        'col2': {'type': 'string', 'required': False, 'description': '第二个变量列（独立样本/配对时使用）'}},
    'adf_test': {'column': {'type': 'string', 'required': True, 'description': '待检验平稳性的列名'}},
    'arima': {'column': {'type': 'string', 'required': True, 'description': '待预测的时间序列列名'},
              'periods': {'type': 'integer', 'required': False, 'description': '预测期数，默认 12'}},
    'ml_train': {'target': {'type': 'string', 'required': True, 'description': '目标变量列名'},
                 'task': {'type': 'string', 'required': False, 'description': 'regression 或 classification，默认按目标推断'}},
    'generate_chart': {'chart_type': {'type': 'string', 'required': True, 'description': '图表类型：scatter/line/bar/hist/box/heatmap 等',
                                      'enum': ['scatter', 'line', 'bar', 'hist', 'box', 'boxplot', 'heatmap']},
                       'col1': {'type': 'string', 'required': False, 'description': '横轴/主要列'},
                       'col2': {'type': 'string', 'required': False, 'description': '纵轴/次要列'}},
    'company_analysis': {'company': {'type': 'string', 'required': True, 'description': '公司名或代码，如 贵州茅台/腾讯/600519/HK0700'}},
    'industry_stats': {
        'company': {'type': 'string', 'required': True, 'description': '公司名或代码，如 贵州茅台/600519/佰维存储（自动识别行业）'},
    },
    'company_source_fetch': {
        'company': {'type': 'string', 'required': True, 'description': '公司名或代码'},
        'source_id': {'type': 'string', 'required': False,
                      'description': '信源：ipo 招股书 / ar 年报 / mda MD&A / cninfo 巨潮公告 / ir 投资者关系 / edgar / qcc 工商 / court 裁判文书 / ip 专利 / report 研报 / wind 估值 / assoc 行业协会；默认 report 研报',
                      'enum': ['ipo', 'ar', 'mda', 'cninfo', 'exchange', 'ir', 'edgar', 'qcc', 'court', 'ip', 'report', 'wind', 'assoc']},
    },
    'company_deep_analyze': {
        'company': {'type': 'string', 'required': True, 'description': '公司名或代码，如 贵州茅台/600519/HK0700'},
        'use_ai': {'type': 'boolean', 'required': False, 'description': '是否启用 AI 草稿，默认 true；false 则仅用规则文案（更快）'},
    },
    'company_deep_export_report': {
        'company': {'type': 'string', 'required': False, 'description': '公司名或代码；缺省则复用上次 company_deep_analyze 的结果'},
        'format': {'type': 'string', 'required': False, 'description': '导出格式 docx 或 pptx，默认 docx', 'enum': ['docx', 'pptx']},
        'template': {'type': 'string', 'required': False,
                     'description': 'PPT 模板：navy_business 商务深蓝 / dark_tech 科技深色 / growth_green 增长青绿 / minimal_light 简约汇报 / invest_memo 投研备忘录',
                     'enum': ['navy_business', 'dark_tech', 'growth_green', 'minimal_light', 'invest_memo']},
    },
    'company_deep_list_templates': {},
    'company_deep_dcf': {
        'fcf': {'type': 'number', 'required': True, 'description': '未来约 5 年 FCF 均值（亿元）'},
        'g': {'type': 'number', 'required': False, 'description': '永续增长率 %，默认 2'},
        'wacc': {'type': 'number', 'required': False, 'description': '折现率 WACC %，默认 9'},
        'shares': {'type': 'number', 'required': False, 'description': '股本（亿股），用于每股内在价值'},
        'net_cash': {'type': 'number', 'required': False, 'description': '净现金（亿元，可为负）'},
    },
    'company_deep_redflags': {
        'ocf': {'type': 'number', 'required': False, 'description': '经营现金流净额（亿元）'},
        'net_profit': {'type': 'number', 'required': False, 'description': '净利润（亿元）'},
        'ar_growth': {'type': 'number', 'required': False, 'description': '应收同比增速 %'},
        'revenue_growth': {'type': 'number', 'required': False, 'description': '营收同比增速 %'},
        'goodwill': {'type': 'number', 'required': False, 'description': '商誉（亿元）'},
        'equity': {'type': 'number', 'required': False, 'description': '净资产（亿元）'},
        'cash': {'type': 'number', 'required': False, 'description': '货币资金（亿元）'},
        'interest_debt': {'type': 'number', 'required': False, 'description': '有息负债（亿元）'},
        'assets': {'type': 'number', 'required': False, 'description': '总资产（亿元）'},
    },
    'company_deep_moat_score': {
        'tech': {'type': 'number', 'required': False, 'description': '技术/专利 0-10'},
        'brand': {'type': 'number', 'required': False, 'description': '品牌心智 0-10'},
        'channel': {'type': 'number', 'required': False, 'description': '渠道网络 0-10'},
        'cost': {'type': 'number', 'required': False, 'description': '成本/规模 0-10'},
        'network': {'type': 'number', 'required': False, 'description': '网络效应 0-10'},
        'license': {'type': 'number', 'required': False, 'description': '牌照/资质 0-10'},
    },
    'company_deep_playbook': {
        'rival': {'type': 'string', 'required': False, 'description': '最大竞争对手名称'},
        'impact': {'type': 'string', 'required': False, 'description': '冲击评估：致命/重大/中等/有限'},
        'company': {'type': 'string', 'required': False, 'description': '目标公司（默认取上次分析）'},
        'our_weakness': {'type': 'string', 'required': False, 'description': '我方软肋'},
        'rival_move': {'type': 'string', 'required': False, 'description': '对手最可能打法'},
    },
    'company_deep_fill': {
        'fields_json': {'type': 'string', 'required': False, 'description': 'JSON 补丁，如 {"conclusions":{"a1":"..."},"val":{"verdict":"合理"}}'},
        'a1': {'type': 'string', 'required': False, 'description': '这门生意好不好'},
        'a2': {'type': 'string', 'required': False, 'description': '这家公司强不强'},
        'a3': {'type': 'string', 'required': False, 'description': '账靠不靠谱'},
        'a4': {'type': 'string', 'required': False, 'description': '谁在管、有什么风险'},
        'a5': {'type': 'string', 'required': False, 'description': '当前价格值不值'},
        'risk': {'type': 'string', 'required': False, 'description': '新增风险描述'},
        'risk_level': {'type': 'string', 'required': False, 'description': '高/中/低'},
        'risk_type': {'type': 'string', 'required': False, 'description': '经营/财务/治理/政策/技术/市场'},
        'valuation_verdict': {'type': 'string', 'required': False, 'description': '低估/合理/偏高/高估'},
    },
    'company_deep_scorecard': {},
    'finance_data': {'symbol': {'type': 'string', 'required': True, 'description': '股票/指数/基金代码或名称'},
                     'period': {'type': 'string', 'required': False, 'description': '周期：daily/weekly/monthly，默认 daily'}},
    'stock_valuation': {'symbol': {'type': 'string', 'required': True, 'description': 'A股 6 位或港股 HK+4 位代码，如 600519/HK0700'},
                        'peers': {'type': 'string', 'required': False, 'description': '可比公司代码，逗号分隔，如 "000858,000568"'}},
    'financial_statements': {'symbol': {'type': 'string', 'required': True, 'description': '股票代码（A 股 6 位或 HK+4 位）'}},
    'financial_ratios': {'symbol': {'type': 'string', 'required': True, 'description': '股票代码（A 股 6 位或 HK+4 位）'}},
    'db_query': {'sql': {'type': 'string', 'required': True, 'description': '只读 SQL 语句（自动限行、禁止 DDL/DML）'},
                 'db_type': {'type': 'string', 'required': False, 'description': '数据库类型：sqlite/mysql/postgresql/clickhouse，默认 sqlite'},
                 'database': {'type': 'string', 'required': False, 'description': '库名/文件（凭据由服务端注入，无需提供口令）'}},
    'safe_calc': {'expression': {'type': 'string', 'required': True,
                                 'description': '待求值的数学表达式，如 "(10000*1.08)-350" 或 "sqrt(160000)" 或 "12.5/4*3.2"。支持 + - * / % // **、括号，函数 abs/round/sqrt/min/max/log/log10/exp/floor/ceil，常量 pi/e'}},
    'percent_change': {'mode': {'type': 'string', 'required': True, 'description': '计算模式：rate=涨跌幅(from→to)；apply=按幅度求目标值',
                                'enum': ['rate', 'apply']},
                       'from_value': {'type': 'number', 'required': False, 'description': 'rate 模式：起始值'},
                       'to_value': {'type': 'number', 'required': False, 'description': 'rate 模式：目标值'},
                       'base': {'type': 'number', 'required': False, 'description': 'apply 模式：基准值'},
                       'pct': {'type': 'number', 'required': False, 'description': 'apply 模式：涨跌幅百分比（正为增、负为减）'}},
    'data_quality': {'file': {'type': 'string', 'required': False, 'description': '待体检的数据文件（后端注入时可省略）'}},
    'segment_breakdown': {
        'code': {'type': 'string', 'required': True, 'description': '股票代码（A 股 6 位如 600519 / 港股 HK+4 位如 HK01810）'},
        'market': {'type': 'string', 'required': False, 'description': 'a=A 股、hk=港股（省略时按代码推断）', 'enum': ['a', 'hk']},
        'category': {'type': 'string', 'required': False, 'description': '分部口径：按产品分类/按地区分类/按行业分类'}},
    # P2-5：补齐此前 9 个无 schema 的工具（describe/overview/columns/correlation 等
    # 纯 file 口径工具，以及无参工具），使 schema 覆盖率从 23/32 → 32/32。
    'describe': {'file': {'type': 'string', 'required': False, 'description': '待描述统计的数据文件（后端注入时可省略）'}},
    'overview': {'file': {'type': 'string', 'required': False, 'description': '待总览的数据文件（后端注入时可省略）'}},
    'columns': {'file': {'type': 'string', 'required': False, 'description': '待列出字段的数据文件（后端注入时可省略）'}},
    'correlation': {'file': {'type': 'string', 'required': False, 'description': '待做相关性分析的数据文件（后端注入时可省略）'}},
    'did_full': {'file': {'type': 'string', 'required': False, 'description': '待做双重差分(DID)的数据文件（后端注入时可省略）'}},
    'feature_eng': {'file': {'type': 'string', 'required': False, 'description': '待做特征工程的数据文件（后端注入时可省略）'}},
    'clean_data': {'file': {'type': 'string', 'required': False, 'description': '待清洗的数据文件（后端注入时可省略）'}},
    'list_skills': {},
    'list_analysis_api': {},
    'list_files': {},
}


# P2-5 参数别名：模型有时输出同义参数名，若不归一会被「未知键丢弃」直接失效
_TOOL_PARAM_ALIASES: dict = {
    'company_analysis': {'company_name': 'company', 'name': 'company', 'code': 'company'},
    'industry_stats': {'company_name': 'company', 'name': 'company', 'code': 'company'},
    'company_source_fetch': {'company_name': 'company', 'name': 'company', 'code': 'company', 'source': 'source_id'},
    'company_deep_analyze': {'company_name': 'company', 'name': 'company', 'code': 'company', 'ai': 'use_ai'},
    'company_deep_export_report': {'company_name': 'company', 'name': 'company', 'code': 'company', 'fmt': 'format', 'tpl': 'template', 'ppt_template': 'template'},
    'company_deep_list_templates': {},
    'company_deep_dcf': {'fcf_yi': 'fcf', 'g_pct': 'g', 'wacc_pct': 'wacc', 'shares_yi': 'shares', 'net_cash_yi': 'net_cash'},
    'company_deep_redflags': {'np': 'net_profit', 'ocf_ni': 'ocf'},
    'company_deep_moat_score': {'tech_score': 'tech', 'brand_score': 'brand'},
    'company_deep_playbook': {'competitor': 'rival', 'company_name': 'company'},
    'company_deep_fill': {'patch': 'fields_json', 'verdict': 'valuation_verdict'},
    'company_deep_scorecard': {},
    'finance_data': {'code': 'symbol', 'stock': 'symbol'},
    'stock_valuation': {'code': 'symbol', 'peers_codes': 'peers'},
    'financial_statements': {'code': 'symbol'},
    'financial_ratios': {'code': 'symbol'},
    'head': {'n_rows': 'n', 'rows': 'n'},
    'arima': {'column_name': 'column', 'period': 'periods'},
    'ml_train': {'target_column': 'target'},
    'generate_chart': {'type': 'chart_type', 'x': 'col1', 'y': 'col2'},
    'hypothesis_test': {'test_type': 'test'},
}

# P0-2 工具统一契约补充：结果预算（借鉴 ZCode ModelToolContract 的 result budget）。
# 每个工具可声明「原始结果最大字符预算」，超出即在注入 LLM 前裁剪，避免单条工具结果
# 撑爆上下文。未声明 → _DEFAULT_RETURN_BUDGET（仍是 string 且超限才裁剪，
# 短结果/数字结果原样保留，保证与改造前行为零回归）。
# run_skill / run_analysis_api 会返回较长的技能/接口产出，保留原 9500 的特殊语义。
_DEFAULT_RETURN_BUDGET = int(os.environ.get("AGENT_TOOL_RESULT_BUDGET", "2000"))
_TOOL_RETURN_BUDGET: dict = {
    'run_skill': 9500,
    'run_analysis_api': 9500,
    'company_deep_analyze': 4500,
    'company_deep_export_report': 1500,
    'company_deep_list_templates': 1200,
    'company_deep_dcf': 800,
    'company_deep_redflags': 1500,
    'company_deep_moat_score': 1200,
    'company_deep_playbook': 1800,
    'company_deep_fill': 800,
    'company_deep_scorecard': 1500,
    'company_analysis': 3500,
    'industry_stats': 1800,
    'company_source_fetch': 2200,
    # 大表输出类工具通常只需前几项要点，收紧预算以压上下文噪音
    'head': 1200,
    'columns': 1200,
    'describe': 1800,
    'overview': 1500,
    'correlation': 1800,
    'db_query': 1500,
    'clean_data': 1600,
    'data_quality': 1500,
    'feature_eng': 1500,
    'ml_train': 1600,
    'arima': 1200,
    'hypothesis_test': 1500,
    'adf_test': 1200,
    'trend': 1600,
    'percent_change': 800,
    'safe_calc': 400,
}


def _tool_return_budget(tool_name: str) -> int:
    """取工具原始结果预算（字符）。未声明用全局默认。"""
    return int(_TOOL_RETURN_BUDGET.get(tool_name, _DEFAULT_RETURN_BUDGET))


def _clip_tool_result(tool_name: str, result) -> Any:
    """按工具结果预算裁剪注入前的原始结果。

    - 未超预算：字符串原样返回，非字符串（int/float 等）原对象返回（零回归）。
    - 超预算：截断为预算长度并附说明（run_skill/run_analysis_api 依旧宽松）。
    """
    if isinstance(result, str) and len(result) <= _tool_return_budget(tool_name):
        return result
    if not isinstance(result, str):
        s = str(result)
        if len(s) <= _tool_return_budget(tool_name):
            return result
    budget = _tool_return_budget(tool_name)
    return str(result)[:budget] + f"\n... (truncated to {budget} chars)"


def _param_meta(tool_name: str, p: str) -> dict:
    """取工具参数元数据，未显式声明时用保守默认（string、可选）。"""
    return (_TOOL_PARAM_SCHEMA.get(tool_name, {}).get(p)
            or {'type': 'string', 'required': False, 'description': f'参数 {p}'})


def _build_openai_tools(names: list = None) -> list:
    """从工具注册表生成 OpenAI 兼容 tools 定义（供原生 function calling）。
    不再「全 string、全 required」：按 _TOOL_PARAM_SCHEMA 给出类型/必填/描述/枚举，
    避免模型为可选参数编造值而触发 schema 校验失败。
    names 非空时只下发白名单工具（多Agent管线按阶段裁剪，减少每轮 payload 与选错工具）。"""
    tools = []
    for t in registry.list_tools(names):
        props = {}
        required = []
        for p in t['params']:
            meta = _param_meta(t['name'], p)
            prop = {'type': meta['type'], 'description': meta['description']}
            if meta.get('enum'):
                prop['enum'] = meta['enum']
            props[p] = prop
            if meta.get('required'):
                required.append(p)
        tools.append({
            "type": "function",
            "function": {
                "name": t['name'],
                "description": t['description'],
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": required,
                },
            },
        })
    return tools


def _normalize_tool_params(tool_name: str, params: dict, info) -> dict:
    """按工具声明的参数 schema 规整：仅保留声明的参数（丢弃未知键），避免越权注入；
    并按类型把 string 参数转化为 int/float（原生 schemaless provider 常把数字当字符串传）。
    返回 (cleaned_params, dropped_keys)。缺失的声明参数不硬删，交由工具自行报错。"""
    if not info or not info.get('params') or not params:
        return (params or {}), []
    declared = set(info['params'])
    # P2-5 别名归一：把模型常输出的同义参数名映射到声明名（否则会被当未知键丢弃）
    _aliases = _TOOL_PARAM_ALIASES.get(tool_name)
    if _aliases:
        for _ak, _dv in list(_aliases.items()):
            if _ak in params and _dv not in params:
                params[_dv] = params[_ak]
    unknown = [k for k in params if k not in declared]
    cleaned = {}
    for k, v in params.items():
        if k not in declared:
            continue
        meta = _param_meta(tool_name, k)
        _t = meta.get('type', 'string')
        if _t in ('integer', 'number') and isinstance(v, str):
            cv = v.strip().replace(',', '')
            try:
                cleaned[k] = int(cv) if _t == 'integer' else float(cv)
            except (ValueError, TypeError):
                cleaned[k] = v  # 无法转换则保原文，交由工具报错
        else:
            cleaned[k] = v
    return cleaned, unknown


# ==================== 单一模型入口 model_tool_chat（P0 移植自修复版） ====================
# 历史：修复版 ai_agent.py:512 有 model_tool_chat 主入口，F 盘曾因重构移除。
# 现回迁以支撑 agent tool-calling 重构 + 对应测试（test_model_tool_chat.py）。
# 行为：始终把工具交给模型，让模型自主决定是否调用；原生 tool_calls 时执行工具回传。
# 不做意图路由、快速/联网模式、Planner、强制工具调用、Agent 分步守卫。
def model_tool_chat(user_message: str, llm_config: Dict[str, Any], history: list = None,
                    file_name: str = None, images: list = None,
                    session_id: str = None, max_iterations: int = 4) -> Dict[str, Any]:
    """单一模型对话入口：始终把工具交给模型，由模型自主决定是否调用。

    这里没有意图路由、快速/联网模式、Planner、强制工具调用或 Agent 分步守卫。
    仅在模型原生返回 tool_calls 时执行对应工具，并把结果回传给同一模型。
    """
    messages = []
    for item in (history or [])[-20:]:
        role = item.get('role')
        content = item.get('content')
        if role in ('user', 'assistant') and content is not None:
            messages.append({'role': role, 'content': str(content)})

    user_content: Any = user_message
    if images:
        user_content = [{'type': 'text', 'text': user_message or '请分析图片'}]
        for image in images[:4]:
            user_content.append({'type': 'image_url', 'image_url': {'url': image}})
    messages.append({'role': 'user', 'content': user_content})

    tools = _build_openai_tools()
    calls_made = []
    usage_total = {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}
    last_content = ''

    for iteration in range(1, max_iterations + 1):
        if session_id and is_cancelled(session_id):
            return {
                'ok': False, 'output': last_content or '生成已停止', 'calls': calls_made,
                'iterations': iteration - 1, 'usage': usage_total, 'cancelled': True,
            }

        response = llm_client.chat(
            messages, llm_config, tools=tools, tool_choice='auto', max_tokens=1800
        )
        _lg_emit_usage(usage_total, response)
        last_content = (response.get('content') or '') if isinstance(response, dict) else str(response)
        tool_calls = response.get('tool_calls') if isinstance(response, dict) else None
        if not tool_calls:
            return {
                'ok': True, 'output': last_content, 'calls': calls_made,
                'iterations': iteration, 'usage': usage_total,
            }

        assistant_message = {
            'role': 'assistant', 'content': last_content or None,
            'tool_calls': tool_calls,
        }
        messages.append(assistant_message)

        for call_index, tool_call in enumerate(tool_calls):
            function = tool_call.get('function') or {}
            tool_name = str(function.get('name') or '')
            try:
                params = json.loads(function.get('arguments') or '{}')
                if not isinstance(params, dict):
                    params = {}
            except (TypeError, ValueError, json.JSONDecodeError):
                params = {}

            tool_info = registry.get_tool(tool_name)
            if tool_info and file_name and 'file' in (tool_info.get('params') or []) and not params.get('file'):
                params['file'] = file_name
            params, _ = _normalize_tool_params(tool_name, params, tool_info)

            started = time.time()
            status = 'ok'
            if tool_info is None:
                result = f"Error: Unknown tool '{tool_name}'"
                status = 'error'
            else:
                try:
                    result = tool_info['func'](**params)
                except Exception as exc:
                    result = f"Error executing {tool_name}: {exc}"
                    status = 'error'
            result_text = str(result)
            if len(result_text) > 12000:
                result_text = result_text[:12000] + '\n... (truncated)'
            elapsed_ms = int((time.time() - started) * 1000)
            calls_made.append({
                'tool': tool_name, 'params': params, 'status': status,
                'duration_ms': elapsed_ms, 'result_preview': result_text[:200],
            })
            messages.append({
                'role': 'tool',
                'tool_call_id': tool_call.get('id') or f'call_{iteration}_{call_index}',
                'name': tool_name,
                'content': result_text,
            })

    # 安全上限：模型连续调用工具达到上限后，只让它基于已有结果完成回答。
    messages.append({
        'role': 'user',
        'content': '请基于以上工具返回结果直接回答最初的问题，不再调用工具。',
    })
    response = llm_client.chat(messages, llm_config, max_tokens=1800)
    _lg_emit_usage(usage_total, response)
    last_content = (response.get('content') or '') if isinstance(response, dict) else str(response)
    return {
        'ok': True, 'output': last_content, 'calls': calls_made,
        'iterations': max_iterations + 1, 'usage': usage_total, 'truncated': True,
    }


# ==================== System Prompt 预算化（P1b + P0-1 语义化压缩）====================
_SYSTEM_BUDGET_CHARS = int(os.environ.get("AGENT_SYSTEM_BUDGET", "15000"))
_LOW_PRIORITY_HINTS = ("会话上下文", "用户文件语义检索", "知识点库", "领域框架",
                       "DID 因果", "面板数据", "金融思考", "金融分析框架", "分析框架")
# P0-1：接近预算时先用 LLM 把低优先级分节压缩成"结论摘要锚点"，仍超限才按优先级截断。
# 借鉴 ZCode TurnMachine 的 microcompact/autoCompact：先语义压缩，而非直接硬切。
# AGENT_SEMANTIC_COMPACT=0 可关闭（回退为纯优先级截断，旧行为、零 LLM 成本）。
_SEMANTIC_COMPACT_ENABLED = (
    os.environ.get("AGENT_SEMANTIC_COMPACT", "1") not in ('0', '', 'false', 'False'))
# 单次压缩送入 LLM 的正文上限 vs 摘要输出长度（控制压缩成本与延迟，可与 ~0ms 首调节流配合）
_SEMANTIC_COMPACT_INPUT_CAP = int(os.environ.get("AGENT_SEMANTIC_COMPACT_INPUT", "12000"))
_SEMANTIC_COMPACT_MAX_TOKENS = int(os.environ.get("AGENT_SEMANTIC_COMPACT_MAX_TOKENS", "300"))


def _section_header(pr: str, body: str) -> str:
    """提取分节标题（用于压缩时的定位与锚点标题）。"""
    m = re.match(r'^(\S.*?)(:|\n|，|。|$)', body)
    return (pr + body)[:60]


def _llm_compact_low_sections(low_sections, llm_config=None, user_message=None) -> Optional[str]:
    """用 LLM 把多段低优先级背景资料整体压缩为一个「结论摘要锚点」。

    借鉴 ZCode 的 autoCompact（多条历史段合并为一条语义摘要）：
      输入 = 若干《背景资料 N + 标题 + 正文》；
      输出 = 一串保留关键数字/结论/名字与来源的精简中文摘要。
    任何异常（LLM 调用失败/空返回）都返回 None，由调用方回退到优先级裁剪——
    语义压缩绝不能阻塞主流程。返回 None 即"不压缩"。
    """
    if not llm_config:
        return None
    try:
        from llm_client import chat
        parts = []
        for i, (_pr, _body) in enumerate(low_sections, 1):
            _b = str(_body)
            parts.append(
                f"[背景资料 {i}] {_section_header(_pr, _body)}\n{_b[:_SEMANTIC_COMPACT_INPUT_CAP // max(len(low_sections), 1)]}")
        payload = '\n\n'.join(parts)[:_SEMANTIC_COMPACT_INPUT_CAP]
        sys = ("你是上下文压缩器。把下面若干段『背景资料』无损提炼成尽量精简、但信息不减的"
               "中文摘要锚点：保留关键数字、结论、公司/指标名称与来源，剔除重复表述与铺垫。"
               "只输出压缩后的摘要正文，不要标题、不要解释、不要无关寒暄。")
        user = (
            f"用户问题（作压缩语义参考）: {str(user_message or '')[:500]}\n\n{payload}"
            if user_message else payload)
        resp = chat([{"role": "system", "content": sys},
                     {"role": "user", "content": user}],
                    llm_config, max_tokens=_SEMANTIC_COMPACT_MAX_TOKENS)
        content = (resp.get('content') if isinstance(resp, dict) else str(resp)) or ''
        content = content.strip()
        return content if len(content) >= 8 else None
    except Exception as e:
        logger.debug("语义压缩失败（回退优先级裁剪）: %s", e)
        return None


# ---- P2-10 知识注入自适应：RAG 注入深度按查询复杂度/意图调整（简单=2，深度分析=4）----
_KB_DEEP_KW = ('全面', '深度', '详细', '完整', '所有', '报告', '分析', '对比', '区别',
               'DID', '双重差分', '方法', '步骤', '如何', '怎么', '流程', '框架',
               '基金', '估值', '投资', '行业', '策略')


def _kb_inject_top_k(message: str) -> int:
    msg = str(message)
    if len(msg) > 60:          # 长问题隐含多维诉求
        return 4
    return 4 if any(k in msg for k in _KB_DEEP_KW) else 2


# ---- P2-10 联网结论闭环：无文件联结论证报告的"数字—来源"一致性审计 ----
# 2026-09-18 强化：数据必须真实。仅「成功工具的真实返回」可作为核验依据；
# 报告中的每个数字都要对上工具结果，产出【核验数据】清单（已核验/未核验）。
_RESEARCH_TOOLS = ('web_search', 'search', 'web', 'fetch', 'news', 'crawl', 'scrape', 'browser')
_FAIL_CALL_STATUS = {'error', 'failed', 'fail', 'timeout', 'exception'}
_NUM_TOKEN_RE = re.compile(
    r'\d{1,3}(?:,\d{3})+(?:\.\d+)?|'      # 千分位大数，如 1,234,567
    r'\d+(?:\.\d+)?'                      # 普通数字（含小数与 4 位年份）
    r'\s*(?:个百分点|万亿元|亿元|万元|亿吨|万吨|万|亿|%|桶|美元|元|倍|点)?'
)


def _has_research_call(calls) -> bool:
    if not calls:
        return False
    for c in calls:
        t = str((c or {}).get('tool') or (c or {}).get('name') or '').lower()
        if any(r in t for r in _RESEARCH_TOOLS):
            return True
    return False


def _extract_numeric_tokens(text: str) -> set:
    toks = set()
    for m in _NUM_TOKEN_RE.finditer(str(text)):
        tok = m.group().strip()
        bare = re.sub(r'\D', '', tok)
        if re.fullmatch(r'(?:19|20)\d{2}', bare):   # 年份不算证据缺口
            continue
        # 过长串更像编号/ID；财务大数（如 1234567 万元）必须保留以便核验
        if len(bare) > 12:
            continue
        toks.add(tok)
    return toks


def _real_tool_evidence(calls) -> List[Dict[str, Any]]:
    """从工具调用中提取「真实数据源」。

    规则（数据必须真实）：
      - 仅 status 为成功（默认 ok / 缺省）的调用计入证据；
      - 失败/超时的工具返回不得当作事实依据；
      - 优先使用 result / result_brief（比 preview 更完整，减少截断误判）。
    """
    out: List[Dict[str, Any]] = []
    for c in (calls or []):
        if not isinstance(c, dict):
            text = str(c)
            if text.strip():
                out.append({'tool': '', 'text': text, 'params': {}})
            continue
        st = str(c.get('status') or 'ok').lower()
        if st in _FAIL_CALL_STATUS:
            continue
        text = str(c.get('result') or c.get('result_brief')
                   or c.get('result_preview') or c.get('preview') or '')
        if not text.strip():
            continue
        params = c.get('params') if isinstance(c.get('params'), dict) else {}
        out.append({'tool': str(c.get('tool') or ''), 'text': text, 'params': params})
    return out


def _tok_variants(tok: str) -> set:
    out = {tok, tok.replace(',', '')}
    bare = re.sub(r'[,\s]', '', tok)
    no_unit = re.sub(r'(?:个百分点|万亿元|亿元|万吨|亿吨|万元|亿|万|桶|美元|元|倍|点|%)+$',
                     '', bare).strip()
    out.add(no_unit)
    try:
        f = float(no_unit)
        out.add(str(int(round(f))))          # 12.34 → 12；2845.6 → 2846
        if 0 < abs(f) < 1000:
            out.add(f'{f:.1f}')              # 12 → 12.0
    except (ValueError, OverflowError) as e:
        logger.debug('词元 %r 非数值，跳过数值变体: %s', no_unit, e)
    return {v for v in out if v}


def _source_blob_nums(blob_nc: str) -> set:
    nums = set()
    for _m in re.finditer(r'\d+(?:\.\d+)?', blob_nc):
        _s = _m.group()
        nums.add(_s)
        try:
            nums.add(str(int(round(float(_s)))))
        except ValueError as e:
            logger.debug('来源文本数值 %r 非可转整型，跳过: %s', _s, e)
    return nums


def _match_in_text(tok: str, text: str, text_nc: str, nums: set) -> bool:
    for v in _tok_variants(tok):
        if v in text or v in text_nc or v in nums:
            return True
    return False


# 核验渲染/事实核查已收敛到 `agent_fact_audit`（本文件曾有一份重复实现，2026-10-09 删除）



def _verified_number_whitelist(calls) -> str:
    """从成功工具返回抽取数字白名单，供终稿生成前约束「只准写真实数」。"""
    evidence = _real_tool_evidence(calls)
    if not evidence:
        return ''
    nums = set()
    for e in evidence:
        for m in re.finditer(r'\d+(?:\.\d+)?', (e.get('text') or '').replace(',', '')):
            v = m.group()
            if re.fullmatch(r'(?:19|20)\d{2}', v):
                continue
            if len(v) > 12:
                continue
            nums.add(v)
            try:
                nums.add(str(int(round(float(v)))))
            except ValueError as e:
                logger.debug('数值 %r 无法转整型，跳过该变体: %s', v, e)
    if not nums:
        return ''
    ordered = sorted(nums, key=lambda x: (len(x), x))[:80]
    tools = '、'.join(dict.fromkeys(e.get('tool') or '工具' for e in evidence))
    return f"（来源工具：{tools}）" + '、'.join(ordered)


def _should_skip_heavy_inject(user_message: str) -> bool:
    """简单闲聊/短问句跳过知识库等重注入，压 p95 长尾。"""
    msg = (user_message or '').strip()
    if len(msg) > 80:
        return False
    heavy = ('全面', '深度', '报告', '研究', '分析', '对比', '尽调', 'DID',
             '回归', '建模', '回测', '归因', '估值', '风险')
    return not any(k in msg for k in heavy)


_RETRIEVAL_WARMUP_DONE = False


def _maybe_warmup_retrieval() -> None:
    """后台预热 knowledge / hybrid 索引（每进程一次，失败静默）。"""
    global _RETRIEVAL_WARMUP_DONE
    if _RETRIEVAL_WARMUP_DONE:
        return
    _RETRIEVAL_WARMUP_DONE = True

    def _bg():
        try:
            from knowledge_rag import get_knowledge_context
            get_knowledge_context('估值 预热', top_k=1)
        except Exception as e:
            logger.debug('知识库预热失败（不影响主流程）: %s', e)
        try:
            import hybrid_retrieval as hr
            hr.retrieval_status()
        except Exception as e:
            logger.debug('检索状态预热失败（不影响主流程）: %s', e)

    try:
        threading.Thread(target=_bg, daemon=True, name='retrieval-warmup').start()
    except Exception as e:
        logger.debug('检索预热线程启动失败: %s', e)


def _budget_system_prompt(prompt: str, llm_config: dict = None,
                          user_message: str = None) -> str:
    """System Prompt 预算化（P1b + P0-1 语义化压缩）。

    高优先级保留：主提示 + 工具说明 + 已加载技能 + 执行计划 + 分步纪律；
    低优先级可裁：会话记忆、文件 RAG、知识库、各领域 CoT 框架。

    P0-1（借鉴 ZCode 的 microcompact/autoCompact）：总长接近预算时，**先**用 LLM 把
    低优先级分节整体压缩成「结论摘要锚点」（_llm_compact_low_sections），若压缩后
    所有内容（含锚点）能落入预算则直接用锚点替换低优先级分节；仅当压缩不可用、
    失败、或锚点仍无法压入预算时，才退化为原有的按优先级截断。语义压缩绝不硬切。
    """
    if len(prompt) <= _SYSTEM_BUDGET_CHARS:
        return prompt
    # 按顶层分节符切分（保留 "\n\n# " 前缀，避免拼接后丢失格式）
    tokens = re.split(r'(\n\n# )', prompt)
    head = tokens[0]                      # 首个 "#" 之前：主提示 + 工具说明
    sections = []                         # (prefix, body)
    i = 1
    while i < len(tokens) - 1:
        sections.append((tokens[i], tokens[i + 1]))
        i += 2
    if not sections:
        return prompt[: _SYSTEM_BUDGET_CHARS]

    # 区分高低优先级
    low, high = [], []
    for pr, body in sections:
        if any(k in body for k in _LOW_PRIORITY_HINTS):
            low.append((pr, body))
        else:
            high.append((pr, body))

    # 高优先级分节恒保留（是否落地交由下方压缩/截断统一控制预算）
    high_text = ''.join(pr + body for pr, body in high)

    dropped = 0
    # ---- P0-1 语义化压缩优先：把低优先级分节整体压成摘要锚点 ----
    if low and _SEMANTIC_COMPACT_ENABLED and llm_config:
        anchor = _llm_compact_low_sections(low, llm_config, user_message)
        if anchor:
            anchor_text = "\n\n# 历史背景摘要（已压缩）\n" + anchor
            if len(head) + len(high_text) + len(anchor_text) <= _SYSTEM_BUDGET_CHARS:
                return head + high_text + anchor_text
    # 压缩未启用/失败/仍超限 → 回退原有优先级截断
    out = head + high_text
    low_kept = []
    for pr, body in low:
        if len(out) + len(pr) + len(body) <= _SYSTEM_BUDGET_CHARS:
            out += pr + body
            low_kept.append(body)
        else:
            break
    dropped = len(low) - len(low_kept)
    if dropped > 0:
        out += (
            f"\n\n# 上下文已精简\n（为控制上下文长度，已省略 {dropped} 段低优先级背景资料。"
            "如需补充，可用 file_rag / 联网工具检索对应内容。）"
        )
    return out


# ==================== Agent Loop ====================
# 提示词统一由 prompts.py 集中管理（支持 data/prompt_versions.json 版本化覆盖）
from prompts import get_prompt

AGENT_SYSTEM_PROMPT = get_prompt('agent_system')
# 投资合规：所有投资/估值/财务分析类输出必须附免责声明
AGENT_SYSTEM_PROMPT = AGENT_SYSTEM_PROMPT + (
    "\n\n# 投资合规要求（必须遵守）\n"
    "凡涉及股票/基金/债券等投资分析、估值判断的回答，结论处必须注明："
    "「本结论为客观数据分析，仅供参考，不构成任何投资建议」。"
)
# 后端全量分析能力：提示模型发现通用分析 API 桥接，覆盖 DID/时序/假设检验/ML/评分卡/固收/金融/透视/图表/清洗/NLQ 等
AGENT_SYSTEM_PROMPT = AGENT_SYSTEM_PROMPT + (
    "\n\n# 全量分析能力（优先使用）\n"
    "除上方专用工具外，网站后端还有全部分析能力（DID 因果、时序、假设检验、ML 建模、评分卡、"
    "固收、金融组合、透视表、图表、数据清洗、自然语言查询等）。当专用工具无法覆盖需求时：\n"
    "1) 先用 `list_analysis_api` 查看可调用的分析端点与参数；\n"
    "2) 再用 `run_analysis_api` 以 JSON 调用对应端点执行真实计算，把结构化结果融入分析结论。\n"
    "尽量用真实计算工具产出数字，而不是虚构数值。"
)
AGENT_SYSTEM_PROMPT = AGENT_SYSTEM_PROMPT + (
    "\n\n# 企业深度分析工具箱（独立成篇时必用）\n"
    "用户要求「深度分析某家公司 / 出研报 / 判断值不值得买 / 拆商业模式与护城河 / 出汇报 PPT」时，"
    "按下列流水线组合调用，不要只空谈：\n"
    "1) **主流程** `company_deep_analyze`（company=公司名或代码，use_ai 默认 true）→ 五问判断卡+指标；\n"
    "2) **补强分析**（按需）：\n"
    "   - `company_deep_scorecard` 财务健康/杜邦/护城河/风险一屏\n"
    "   - `company_deep_redflags` 现金流照妖镜排雷\n"
    "   - `company_deep_moat_score` 护城河六维打分\n"
    "   - `company_deep_dcf` 简易内在价值\n"
    "   - `company_deep_playbook` 竞争推演（对手怎么打、守不守得住）\n"
    "3) **润色结论** `company_deep_fill` 写入 a1–a5 / 风险 / 估值结论；\n"
    "4) **交付** `company_deep_list_templates` 选模板 → `company_deep_export_report`（format=pptx|docx，template=navy_business|dark_tech|growth_green|minimal_light|invest_memo）。\n"
    "也可经 `run_analysis_api` 调 `/api/company-deep/auto|export|ppt-templates`。\n"
    "输出结构：一句话结论 → 五问 → 主要风险 → 投资含义 →（可选）报告文件路径；"
    "估值相关必须附「仅供参考，不构成投资建议」。"
)
REFINEMENT_PROMPT = get_prompt('refinement')
FORCE_FINAL_PROMPT = get_prompt('force_final')
NO_TOOLS_PROMPT = get_prompt('no_tools_retry')
NO_TOOLS_NO_FILE_PROMPT = get_prompt('no_tools_no_file')
REFINEMENT_NO_FILE_PROMPT = get_prompt('refinement_no_file')
FORCE_FINAL_NO_FILE_PROMPT = get_prompt('force_final_no_file')
PLANNER_PROMPT = get_prompt('planner')
REFLECTION_PROMPT = get_prompt('reflection')
STEP_DISCIPLINE_PROMPT = get_prompt('step_discipline')

# 分步分析守卫：终稿前最少成功执行的工具调用次数（可通过环境变量调整）。
# 低于该值时模型试图直接写结论会被拦截，要求继续补资料（严禁跳步）。
MIN_SUCCESS_CALLS = max(1, int(os.environ.get('AGENT_MIN_TOOL_CALLS', '2')))
_STEP_GUARD_MAX = 2  # 守卫最多拦截次数，防止无限循环空耗额度


# ==================== Built-in Tools ====================

@register_tool('web_search', 'Search the web for up-to-date information (news, facts, latest data) to answer questions the data cannot cover', ['query'])
def tool_web_search(query: str) -> str:
    """网页搜索：获取最新资料（受「数据出境」设置管制，关闭时不外呼）"""
    if not _tool_allowed('web_search'):
        return _egress_off('web_search')
    try:
        from web_search import web_search as _ws
        res = _ws(query, top_k=5)
    except Exception as e:
        return f"联网搜索不可用: {e}"
    if not res.get("ok"):
        return f"联网搜索失败: {res.get('error', '')}"
    items = res.get("results", [])
    if not items:
        return "未找到相关网页结果。"
    lines = [f"针对「{query}」的搜索结果："]
    for i, it in enumerate(items, 1):
        lines.append(f"{i}. {it['title']}\n   链接: {it['url']}\n   摘要: {it['snippet']}")
    return "\n".join(lines)


@register_tool('file_rag', 'Semantically search the currently uploaded data file(s): retrieve rows/columns/concepts relevant to the question from user-uploaded files. Call this when the answer needs content/meaning from inside the uploaded file beyond simple SQL (e.g. which columns relate to a concept, key value ranges, what the data represents).', ['query', 'file'])
def tool_file_rag(query: str, file: str = None) -> str:
    """用户上传文件的语义检索（RAG 增强）。"""
    fname = file or getattr(tool_file_rag, '_active_file', None)
    try:
        import file_rag
        if fname:
            file_rag.index_current_file(fname)
        hits = file_rag.search_file_rag(query, top_k=4)
    except Exception as e:
        return f"文件语义检索不可用: {e}"
    if not file_rag._indexed_files():
        return "当前未索引任何用户文件。请先选择并上传数据文件后重试。"
    if not hits:
        return f"在已上传文件（{', '.join(file_rag._indexed_files())}）中未检索到与「{query}」相关的内容。"
    lines = [f"在用户文件「{query}」检索到的相关内容："]
    for h in hits:
        lines.append(f"- [{h['file']}] (匹配度 {h['score']}): {h['text']}")
    return "\n".join(lines)


@register_tool('list_skills', 'List available Skills (expert deep-analysis workflows/instruction sets) that can be loaded via run_skill. Call this to see which enabled skills exist.', [])
def tool_list_skills() -> str:
    """列出已启用 Skills 的索引（名称+描述），帮助 LLM 判断是否调用"""
    try:
        from skill_manager import get_skills_index
        idx = get_skills_index()
        if not idx:
            return "当前没有已启用的 Skill。仍可基于内置工具作答。可在 AI 助手界面导入并启用 Skill 后再试。"
        return json.dumps(idx, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Error listing skills: {e}"


@register_tool('run_skill', 'Load and apply a named Skill (expert deep-analysis workflow) for the given task. Call this when the question requires professional/deep analysis matching an available skill; strictly follow the returned workflow to carry out the analysis.', ['skill_name', 'task'])
def tool_run_skill(skill_name: str, task: str = '') -> str:
    """加载启用 Skill 的完整正文，供 LLM 依据该技能做深度分析"""
    try:
        from skill_manager import get_skill, get_skills_index
        enabled_names = {s['name'] for s in get_skills_index()}
        if skill_name not in enabled_names:
            return (f"Error: Skill '{skill_name}' 不存在或未启用。可用 Skills："
                    + (json.dumps(sorted(enabled_names), ensure_ascii=False) if enabled_names else "（无）"))
        skill = get_skill(skill_name)
        if not skill:
            return f"Error: Skill '{skill_name}' 加载失败。"
        body = skill.get('body') or skill.get('description') or ''
        head = f"# 技能已加载：{skill['name']}\n描述：{skill.get('description', '')}\n\n请严格遵循以下技能工作流执行深度分析：\n\n{body}"
        if task:
            head += f"\n\n待分析任务：{task}"
        return head[:6000]
    except Exception as e:
        return f"Error loading skill: {e}"


# ==================== 通用分析 API 桥接 ====================
# 让 Agent 自由调用网站后端全部分析端点（DID/时序/假设检验/ML/评分卡/固收/金融/
# 透视/图表/清洗/NLQ/analytics 等）。端点需命中以下前缀白名单，并实际注册在 app 中。

# 功能类端点白名单前缀：凡命中且真实注册在 app 的端点，Agent 均可经 run_analysis_api 调用。
# 覆盖网站主要「功能类」能力族；管理/破坏性/内部基建端点**不放行**（见 _ANALYSIS_API_HINT 注释内的安全边界）。
_FUNCTIONAL_PREFIXES = (
    # 数据说明/清洗/表格/图表
    '/api/describe', '/api/correlation', '/api/group', '/api/analyze', '/api/scrape',
    '/api/clean', '/api/table/', '/api/chart/', '/api/store',
    # 时序 / 假设检验 / DID / 机器学习 / 特征工程
    '/api/did/', '/api/ht', '/api/ts/', '/api/ml/', '/api/feature/',
    # 评分卡 / 固收 / 金融 / 自然语言查询 / 高级分析
    '/api/scorecard/', '/api/fixedincome/', '/api/finance/', '/api/nlq/', '/api/analytics/',
    # 本次新增覆盖：财务报表 / 股票筛选 / NLP / 报告预览
    '/api/fs/', '/api/screener/', '/api/nlp/', '/api/report/preview', '/api/company-analysis',
    '/api/company-deep/',
    '/api/rag/query',
)

# ---- P1 工具执行边界（已迁至 tool_boundary.py；此处 re-export 保外部调用点零改动） ----
# 背景：`_FUNCTIONAL_PREFIXES` 已是前缀白名单，但端点一旦注册即可被模型触达，
# **没有类别粒度**，也没有对"写盘类工具能写到哪"的约束。此处补两层：
#
#   ① 端点类别划分：区分「纯读数」与「会落盘/改库」。写盘类端点受
#      `ALLOW_TOOL_SIDE_EFFECTS` 与 `AGENT_ALLOW_WRITE_ENDPOINTS` 双重约束。
#   ② 写盘路径约束：所有 `*_write` 类工具的产出必须落在 `OUT_DIR` 内
#      （`safe_output_path()` 拦路径穿越），防止模型构造 `../../` 越界写。
#
# 为什么不上容器/子进程沙箱：本仓库工具均为**数据分析**语义（非任意 shell/代码执行），
# 上容器的成本（部署复杂度、便携 Python 适配）远超收益。路径与类别约束已覆盖主要风险面。
# ⚠️ 迁出原因：`ai_agent.py` 已触 R1 行数基线，本块为纯函数 + 表，零依赖可独立测试。
from tool_boundary import (WRITE_ENDPOINT_PREFIXES as _WRITE_ENDPOINT_PREFIXES,
                           classify_endpoint as _classify_endpoint,
                           write_endpoints_allowed as _write_endpoints_allowed,
                           safe_output_path as safe_output_path,
                           guarded_load as guarded_load)
# 只读前缀 = 总前缀白名单去掉写盘项（在调用方算，避免 tool_boundary 反向 import 本模块）
_READONLY_ENDPOINT_PREFIXES = tuple(
    p for p in _FUNCTIONAL_PREFIXES if not p.startswith(_WRITE_ENDPOINT_PREFIXES)
)


def endpoint_risk(endpoint: str) -> str:
    """该分析端点的副作用类别：'readonly' / 'write'（未命中白名单返回 'unknown'）。"""
    return _classify_endpoint(endpoint, _READONLY_ENDPOINT_PREFIXES)

# 端点 → 参数提示（供模型组装 params）。命中前缀但未列出的端点也可调用，模型需自行推断参数。
_ANALYSIS_API_HINT = {
    # 清洗
    '/api/clean': {'file'},
    '/api/clean/init': {'file'},
    '/api/clean/text': {'file', 'column', 'rule'},
    '/api/clean/encode': {'file', 'column'},
    '/api/clean/date': {'file', 'column', 'format'},
    '/api/clean/replace': {'file', 'column', 'old', 'new'},
    '/api/clean/filter': {'file', 'expression'},
    '/api/clean/save': {'file'},
    '/api/clean/missing-heatmap': {'file'},
    '/api/clean/log': {'file'},
    # 表格 / 透视
    '/api/table/pivot': {'file', 'index_cols', 'columns_col', 'values_col', 'agg_func'},
    '/api/table/crosstab': {'file', 'index', 'columns', 'values'},
    '/api/table/join': {'file', 'file2', 'how', 'left_on', 'right_on'},
    '/api/table/formula-apply': {'file', 'expr', 'new_col'},
    '/api/table/sample': {'file', 'n'},
    '/api/table/columns': {'file'},
    # 图表
    '/api/chart/custom': {'file', 'x', 'y', 'type'},
    '/api/chart/plotly': {'file', 'plot_type', 'x', 'y'},
    '/api/chart/echarts-data': {'file', 'chart_type', 'x', 'y'},
    '/api/chart/analyze': {'file'},
    '/api/chart/scatter': {'file', 'x', 'y'},
    # 时序
    '/api/ts/date-features': {'file', 'column'},
    '/api/ts/resample': {'file', 'column', 'rule', 'agg'},
    '/api/ts/prophet': {'file', 'column', 'periods'},
    '/api/ts/rolling': {'file', 'column', 'window'},
    '/api/ts/decompose': {'file', 'column', 'model'},
    '/api/ts/adf': {'file', 'column'},
    '/api/ts/arima': {'file', 'column', 'periods'},
    # 假设检验
    '/api/ht/run': {'file', 'test', 'var1', 'var2', 'group_col'},
    '/api/ht/viz': {'file', 'test', 'var1', 'var2', 'group_col'},
    # DID
    '/api/did/regression': {'file', 'dep_var', 'treat_col', 'time_col', 'unit_col', 'control_vars', 'consume_cols'},
    '/api/did/parallel-trends': {'file', 'dep_var', 'treat_col', 'time_col', 'unit_col', 'pre_periods', 'post_periods', 'consume_cols'},
    '/api/did/placebo': {'file', 'dep_var', 'treat_col', 'time_col', 'unit_col', 'n_placebo', 'consume_cols'},
    '/api/did/subsample': {'file', 'dep_var', 'treat_col', 'time_col', 'unit_col', 'split_col', 'consume_cols'},
    '/api/did/heterogeneity': {'file', 'dep_var', 'treat_col', 'time_col', 'unit_col', 'hetero_var', 'consume_cols'},
    '/api/did/mediation': {'file', 'dep_var', 'treat_col', 'mediator', 'consume_cols'},
    '/api/did/interaction': {'file', 'dep_var', 'treat_col', 'interact_var', 'consume_cols'},
    '/api/did/time-trend': {'file', 'dep_var', 'treat_col', 'time_col', 'consume_cols'},
    '/api/did/full-analysis': {'file', 'dep_var', 'treat_col', 'time_col', 'unit_col', 'control_vars', 'consume_cols'},
    '/api/did/columns': {'file'},
    # 机器学习
    '/api/ml/columns': {'file'},
    '/api/ml/train': {'file', 'target', 'task', 'model', 'test_size'},
    '/api/ml/predict': {'file', 'model_id'},
    '/api/ml/feature-engineering': {'file', 'target'},
    '/api/feature/auto': {'file', 'target'},
    '/api/feature/ts': {'file', 'date_col', 'value_col'},
    '/api/feature/cross': {'file', 'cols', 'topk'},
    '/api/ml/spearman': {'file', 'target'},
    '/api/ml/evaluation-panel': {'file', 'target'},
    # 评分卡
    '/api/scorecard/columns': {'file'},
    '/api/scorecard/binning': {'file', 'col', 'target', 'bins'},
    '/api/scorecard/train': {'file', 'target', 'features'},
    '/api/scorecard/psi': {'file', 'col', 'base_col'},
    '/api/scorecard/report': {'file', 'model_id'},
    '/api/scorecard/predict': {'file', 'model_id'},
    # 固收
    '/api/fixedincome/bond': {'bond_id', 'ytm'},
    '/api/fixedincome/portfolio': {'file'},
    '/api/fixedincome/curve': {'file'},
    '/api/fixedincome/spread': {'file'},
    '/api/fixedincome/stress': {'file', 'shock'},
    # 金融分析
    '/api/finance/fund-metrics': {'file', 'return_col', 'benchmark_col', 'risk_free'},
    '/api/finance/capm': {'file', 'return_col', 'market_col'},
    '/api/finance/fama-french': {'file', 'return_col'},
    '/api/finance/backtest': {'file', 'strategy', 'capital'},
    '/api/finance/predict-eval': {'file'},
    '/api/finance/kline': {'symbol', 'period', 'start_date', 'end_date', 'adjust'},
    '/api/finance/akshare': {'func', 'symbol', 'period', 'start_date', 'end_date', 'indicator'},
    '/api/finance/yfinance': {'func', 'symbol', 'period', 'start_date', 'end_date', 'filename'},
    '/api/finance/fred': {'func', 'series_id', 'series_ids', 'query', 'start_date', 'end_date', 'filename', 'symbol'},
    # NLQ / 自然语言查询
    '/api/nlq/execute': {'file', 'question'},
    '/api/nlq/query': {'file', 'question'},
    # Analytics
    '/api/analytics/query': {'file', 'sql'},
    '/api/analytics/describe': {'file'},
    # 财务报表 / 相对估值
    '/api/fs/fetch': {'symbol'},
    '/api/fs/ratios': {'symbol'},
    '/api/fs/charts': {'ratios', 'symbol'},
    '/api/fs/dcf': {'symbol', 'params'},
    '/api/fs/compare': {'symbol', 'peers'},
    # 股票筛选
    '/api/screener/fetch': {},
    '/api/screener/screen': {'data', 'filters'},
    '/api/screener/export': {'data', 'filters'},
    # NLP 文本分析
    '/api/nlp/analyze': {'file', 'col', 'text'},
    # 报告预览
    '/api/report/preview': {'charts', 'conclusions', 'template', 'file'},
    '/api/company-analysis': {'company'},
    '/api/company-deep/auto': {'company', 'use_ai'},
    '/api/company-deep/export': {'state', 'format', 'template'},
    '/api/company-deep/ppt-templates': {},
    '/api/rag/query': {'question', 'top_k'},
}


def _discover_functional_endpoints() -> List[Dict]:
    """运行时从 Flask 路由表动态收集所有命中 _FUNCTIONAL_PREFIXES 的真实功能端点。

    返回 [{endpoint, methods}]，按 endpoint 排序；endpoint 为规范路径（去掉末尾 '/'）。
    （路由为懒注册，此处每次调用都实时读 app.url_map，故目录与真实注册保持同步。）
    """
    found = {}  # endpoint -> set(methods)
    try:
        from app import app
        for rule in app.url_map.iter_rules():
            path = rule.rule
            if path.endswith('/') and path != '/':
                path = path.rstrip('/')
            if not path.startswith('/api/'):
                continue
            if not path.startswith(_FUNCTIONAL_PREFIXES):
                continue
            # 只暴露可读/分析类方法；POST(或有 body) 与 GET 均纳入，排除 OPTIONS/HEAD 噪音
            methods = {m for m in getattr(rule, 'methods', set()) if m in ('POST', 'GET')}
            if not methods:
                continue
            found.setdefault(path, set()).update(methods)
    except Exception as _e:
        logger.debug("api endpoint scan swallowed: %s", _e)
    return [{'endpoint': e, 'methods': sorted(m)} for e, m in sorted(found.items())]


def _analysis_api_registered(endpoint: str) -> bool:
    """端点是否已注册（命中功能白名单前缀 + 真实存在于 Flask 路由表）"""
    if not endpoint or not endpoint.startswith(_FUNCTIONAL_PREFIXES):
        return False
    ep = endpoint.rstrip('/')
    try:
        for item in _discover_functional_endpoints():
            if item['endpoint'] == ep:
                return True
    except Exception:
        pass
    return False


@register_tool('list_analysis_api', 'List backend analysis API endpoints that can be invoked via run_analysis_api, grouped by capability (clean/table/chart/ts/ht/did/ml/feature/scorecard/fixedincome/finance/nlq/analytics/fs/screener/nlp/report). Use this to discover available analysis endpoints and their parameters.', [])
def tool_list_analysis_api() -> str:
    """列出真实注册的后端功能端点目录（基于运行时路由表，而非硬编码）"""
    groups = [
        ('数据清洗', ('/api/clean', '/api/describe', '/api/correlation', '/api/group', '/api/analyze', '/api/scrape')),
        ('表格/透视', ('/api/table/',)),
        ('图表', ('/api/chart/',)),
        ('时序分析', ('/api/ts/',)),
        ('假设检验', ('/api/ht',)),
        ('DID 因果推断', ('/api/did/',)),
        ('机器学习', ('/api/ml/', '/api/feature/')),
        ('评分卡', ('/api/scorecard/',)),
        ('固收分析', ('/api/fixedincome/',)),
        ('金融组分析', ('/api/finance/',)),
        ('自然语言查询', ('/api/nlq/',)),
        ('高级分析', ('/api/analytics/', '/api/rag/query')),
        ('财务报表/估值', ('/api/fs/', '/api/company-analysis', '/api/company-deep/')),
        ('股票筛选', ('/api/screener/',)),
        ('NLP 文本分析', ('/api/nlp/',)),
        ('报告预览', ('/api/report/preview',)),
    ]
    discovered = {d['endpoint']: sorted(d['methods']) for d in _discover_functional_endpoints()}
    if not discovered:
        return "（当前未发现有可调用的功能端点 / 路由未就绪）"

    def _match(e: str, prefixes) -> bool:
        return any(e == p.rstrip('/') or e.startswith(p) for p in prefixes if p)

    lines = ["可用后端功能 API（通过 run_analysis_api 调用；参数取下方 params 键或自行推断，file 自动注入）："]
    shown = set()
    for label, prefixes in groups:
        eps = sorted(e for e in discovered if _match(e, prefixes) and e not in shown)
        if not eps:
            continue
        lines.append(f"\n## {label}")
        for e in eps:
            methods = '/'.join(discovered[e])
            hint = _ANALYSIS_API_HINT.get(e)
            htxt = ', '.join(sorted(hint)) if hint else '-（模型自行推断，POST json / GET query 传参）'
            lines.append(f"- [{methods}] `{e}`  (params: {htxt})")
            shown.add(e)
    # 命中白名单但未分组命中的端点（新增能力族等）
    rest = sorted(e for e in discovered if e not in shown)
    if rest:
        lines.append("\n## 其他功能端点")
        for e in rest:
            methods = '/'.join(discovered[e])
            hint = _ANALYSIS_API_HINT.get(e)
            htxt = ', '.join(sorted(hint)) if hint else '-'
            lines.append(f"- [{methods}] `{e}`  (params: {htxt})")
    return "\n".join(lines)


@register_tool('run_analysis_api', 'Invoke any backend analysis endpoint (e.g. DID, time-series, hypothesis test, ML, scorecard, fixed-income, finance, pivot, chart, clean) directly. endpoint must be one of the analysis APIs (see list_analysis_api) and will auto-receive the current file when accepted. Returns the structured JSON result.', ['endpoint', 'params', 'file'])
def tool_run_analysis_api(endpoint: str, params=None, file: str = '') -> str:
    """通过 Flask test_client 本地调用任一白名单分析端点，返回 JSON

    调用前过两道边界（P1）：
      1. 端点类别：写盘类端点额外受 `AGENT_ALLOW_WRITE_ENDPOINTS` 约束；
      2. 参数中的 `file` / `out` / `filename` 等落盘键经 `safe_output_path()` 校验。
    """
    if not _analysis_api_registered(endpoint):
        return (f"Error: 端点 '{endpoint}' 不在白名单或未注册。可用端点见 list_analysis_api。"
                f"\n白名单前缀：{', '.join(_FUNCTIONAL_PREFIXES)}")
    _risk = endpoint_risk(endpoint)
    if _risk == 'write' and not _write_endpoints_allowed():
        return (f"Error: 端点 '{endpoint}' 属写盘/改库类（risk=write），当前被 "
                f"AGENT_ALLOW_WRITE_ENDPOINTS=0 拦截，本次未产生任何副作用。"
                f"如需只读分析，请改用 describe / correlation / ht / ts / did / fixedincome 等读数端点。")
    if params is None:
        params = {}
    elif isinstance(params, str):
        try:
            params = json.loads(params)
        except Exception:
            return f"Error: params 不是合法 JSON：{params[:200]}"
    if not isinstance(params, dict):
        return "Error: params 必须是 JSON 对象（dict）。"
    # 落盘类键一律过写入边界（防模型构造 `../../` 越界写）
    for _k in ('out', 'output', 'save_as', 'out_file'):
        if _k in params and isinstance(params[_k], str):
            _p, _err = safe_output_path(params[_k])
            if _err:
                return f"Error: 参数 {_k} 被写入边界拒绝 —— {_err}"
            params[_k] = _p.name
    # 端点声明接收 file 且未给定时，注入当前文件
    if file and 'file' not in params and endpoint in _ANALYSIS_API_HINT and 'file' in _ANALYSIS_API_HINT[endpoint]:
        params['file'] = file

    try:
        from app import app
        client = app.test_client()
        # 部分端点为 GET（目录/模型列表等），读到 params list 中不含 file 的场景可回退
        resp = client.post(endpoint, json=params)
        if resp.status_code in (405, 404) and (endpoint not in _ANALYSIS_API_HINT or _ANALYSIS_API_HINT[endpoint] == {'file'} or 'file' not in _ANALYSIS_API_HINT[endpoint]):
            resp = client.get(endpoint, query_string=params)
        try:
            data = resp.get_json()
        except Exception:
            data = None
        if data is None:
            return f"{endpoint} -> HTTP {resp.status_code}\n{resp.get_data(as_text=True)[:1500]}"
        return json.dumps(data, ensure_ascii=False)[:9000]
    except Exception as e:
        return f"Error calling {endpoint}: {str(e)}"


@register_tool('describe', 'Get descriptive statistics (mean, std, min, max)', ['file'])
def tool_describe(file: str) -> str:
    """Get descriptive statistics"""
    from core_config import load_dataframe, OUT_DIR
    
    try:
        df = load_dataframe(str(OUT_DIR / file))
        if df is None or df.empty:
            return "Error: Could not load file"
        
        num_cols = df.select_dtypes(include='number').columns.tolist()
        if not num_cols:
            return "No numeric columns found"
        
        stats = df[num_cols].describe().T[['mean', 'std', 'min', 'max']].round(4)
        return f"Shape: {df.shape}\n\nStatistics:\n{stats.to_string()}"
    except Exception as e:
        return f"Error: {str(e)}"


@register_tool('overview', 'Get data overview: panel structure, treatment evolution, key variables', ['file'])
def tool_overview(file: str) -> str:
    """Get comprehensive data overview: panel structure, treatment variable evolution, key variables"""
    from core_config import load_dataframe, OUT_DIR
    
    try:
        df = load_dataframe(str(OUT_DIR / file))
        if df is None or df.empty:
            return "Error: Could not load file"
        
        lines = [f"Panel: {df.shape[0]} rows x {df.shape[1]} cols"]
        
        # 省份/个体维度
        unit_col = None
        for col in df.columns:
            if col.lower() in ('province', 'city', '省份', '城市', 'id'):
                unit_col = col
                break
        if unit_col:
            lines.append(f"Units: {df[unit_col].nunique()} ({unit_col})")
        
        # 时间维度
        time_col = None
        for col in df.columns:
            if col in ('year', 'Year', '年份', '时间'):
                time_col = col
                break
        if time_col:
            years = sorted(df[time_col].unique())
            lines.append(f"Time: {years[0]}-{years[-1]} ({len(years)} periods, {time_col})")
        
        # 处理变量演变（试点覆盖率）
        treat_col = None
        for col in df.columns:
            if any(k in col for k in ['treatrate', '试点', 'treat', '政策', '处理']):
                treat_col = col
                break
        
        if treat_col:
            lines.append(f"\nTreatment variable: {treat_col}")
            if time_col and treat_col in df.columns:
                # 按年份统计试点覆盖率
                treat_by_year = df.groupby(time_col)[treat_col].agg(['mean', 'min', 'max', lambda x: (x > 0).sum()]).round(3)
                treat_by_year.columns = ['mean', 'min', 'max', 'n_treated']
                lines.append(f"Treatment evolution by year:\n{treat_by_year.to_string()}")
                
                # 找出从未试点的省份
                max_by_unit = df.groupby(unit_col)[treat_col].max() if unit_col else None
                if max_by_unit is not None:
                    never = max_by_unit[max_by_unit == 0].index.tolist()
                    if never:
                        lines.append(f"\nNever-treated provinces ({len(never)}): {', '.join(map(str, never[:10]))}")
                    started = max_by_unit[max_by_unit > 0].index.tolist()
                    lines.append(f"Ever-treated provinces ({len(started)}): {', '.join(map(str, started[:15]))}")
        
        # 结果变量
        dep_vars = []
        for col in df.columns:
            if any(k in col.lower() for k in ['surv', 'dev', 'enjoy', 'upgr', '生存', '发展', '享受', '升级']):
                dep_vars.append(col)
        if dep_vars:
            lines.append(f"\nOutcome variables: {', '.join(dep_vars)}")
        
        # 控制变量
        control_vars = [c for c in df.select_dtypes(include='number').columns 
                       if c not in dep_vars and c != treat_col and c != time_col and c != unit_col]
        if control_vars:
            lines.append(f"Control variables ({len(control_vars)}): {', '.join(control_vars)}")
        
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {str(e)}"


@register_tool('group_compare', 'Compare outcome means between treated and control groups', ['file', 'outcome'])
def tool_group_compare(file: str, outcome: str = None) -> str:
    """Compare outcome means between treated (>0) and control (=0) groups"""
    from core_config import load_dataframe, OUT_DIR
    
    try:
        df = load_dataframe(str(OUT_DIR / file))
        if df is None or df.empty:
            return "Error: Could not load file"
        
        # 找处理变量
        treat_col = None
        for col in df.columns:
            if any(k in col for k in ['treatrate', '试点', 'treat', '政策', '处理']):
                treat_col = col
                break
        if not treat_col:
            return "No treatment variable found"
        
        # 找时间列
        time_col = None
        for col in df.columns:
            if col in ('year', 'Year', '年份', '时间'):
                time_col = col
                break
        
        # 找结果变量
        if not outcome:
            for col in df.columns:
                if any(k in col.lower() for k in ['surv', '生存']):
                    outcome = col
                    break
            if not outcome:
                outcome = df.select_dtypes(include='number').columns[0]
        
        if outcome not in df.columns:
            return f"Outcome '{outcome}' not found"
        
        df = df.copy()
        df['_treated'] = df[treat_col] > 0
        
        lines = [f"Group comparison: {outcome} (treated = {treat_col} > 0)"]
        lines.append("")
        
        # 全样本对比
        groups = df.groupby('_treated')[outcome].agg(['mean', 'std', 'count']).round(4)
        lines.append("Full sample:")
        lines.append(groups.to_string())
        
        # 如果有多期，对比试点后（时间中位数以后）
        if time_col:
            mid_year = df[time_col].median()
            post = df[df[time_col] > mid_year]
            lines.append(f"\nAfter {int(mid_year)} (policy period):")
            lines.append(post.groupby('_treated')[outcome].agg(['mean', 'std', 'count']).round(4).to_string())
        
        # 差值
        means = df.groupby('_treated')[outcome].mean()
        if 0 in means.index and 1 in means.index:
            diff = means[1] - means[0]
            lines.append(f"\nMean difference (treated - control): {diff:.4f}")
        
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {str(e)}"


@register_tool('correlation', 'Get correlation matrix of numeric columns', ['file'])
def tool_correlation(file: str) -> str:
    """Get correlation matrix"""
    from core_config import load_dataframe, OUT_DIR
    
    try:
        df = load_dataframe(str(OUT_DIR / file))
        if df is None or df.empty:
            return "Error: Could not load file"
        
        num_cols = df.select_dtypes(include='number').columns.tolist()
        if len(num_cols) < 2:
            return "Need at least 2 numeric columns"
        
        corr = df[num_cols].corr().round(4)
        
        strong = []
        for i in range(len(num_cols)):
            for j in range(i+1, len(num_cols)):
                c = corr.iloc[i, j]
                if abs(c) > 0.7:
                    strong.append(f"{num_cols[i]} <-> {num_cols[j]}: {c}")
        
        result = f"Correlation matrix:\n{corr.to_string()}"
        if strong:
            result += f"\n\nStrong correlations (|r|>0.7):\n" + "\n".join(strong)
        return result
    except Exception as e:
        return f"Error: {str(e)}"


@register_tool('did_regression', 'Run DID regression analysis', ['file', 'dep_var'])
def tool_did_regression(file: str, dep_var: str = None) -> str:
    """Run DID regression"""
    from core_config import load_dataframe, OUT_DIR
    from did import did_regression
    
    try:
        df = load_dataframe(str(OUT_DIR / file))
        if df is None or df.empty:
            return "Error: Could not load file"
        
        treat_col = None
        for col in df.columns:
            if any(k in col for k in ['试点', 'treat', 'policy', '处理']):
                treat_col = col
                break
        
        if not treat_col:
            return "No treatment variable found"
        
        time_col = None
        for col in df.columns:
            if col in ('year', 'Year', '年份', '时间', 'date', 'Date'):
                time_col = col
                break
        
        if not time_col:
            return "No time variable found"
        
        unit_col = None
        for col in df.columns:
            if col.lower() in ('province', 'city', 'id', '个体', '省份', '城市'):
                unit_col = col
                break
        
        if not unit_col:
            unit_col = df.select_dtypes(include='object').columns[0] if df.select_dtypes(include='object').any() else None
        
        if not unit_col:
            return "No unit/entity variable found"
        
        if not dep_var:
            for col in df.columns:
                if any(k in col.lower() for k in ['upgr', '升级', '消费结构']):
                    dep_var = col
                    break
            if not dep_var:
                dep_var = df.select_dtypes(include='number').columns[0]
        
        if dep_var not in df.columns:
            return f"Variable '{dep_var}' not found"
        
        control_vars = [c for c in df.select_dtypes(include='number').columns 
                       if c != dep_var and c != treat_col and c != time_col and c != unit_col]
        
        # 过滤高共线控制变量（与处理变量相关性 > 0.9 的剔除）
        filtered = []
        for c in control_vars:
            if c in df.columns and treat_col in df.columns:
                try:
                    cor = abs(df[c].corr(df[treat_col]))
                    if cor < 0.9:
                        filtered.append(c)
                except Exception:
                    filtered.append(c)
            else:
                filtered.append(c)
        control_vars = filtered[:15]
        
        df_reg = df.copy()
        if df_reg[treat_col].nunique() > 2:
            treat_dummy = f'{treat_col}_treated'
            df_reg[treat_dummy] = (df_reg[treat_col] > 0).astype(int)
            actual_treat = treat_dummy
        else:
            actual_treat = treat_col
        
        result = did_regression(df_reg, dep_var, actual_treat, time_col, unit_col, control_vars[:10])
        
        if 'error' in result:
            return f"Regression error: {result['error']}"
        
        lines = [f"DID Regression: {dep_var} ~ {actual_treat}"]
        lines.append(f"Observations: {result.get('n_obs', '-')}")
        lines.append(f"R-squared: {result.get('r2', '-'):.4f}")
        lines.append("")
        lines.append("Coefficients:")
        for var, coef in result.get('params', {}).items():
            pval = result.get('pvalues', {}).get(var, 1)
            sig = '***' if pval < 0.01 else '**' if pval < 0.05 else '*' if pval < 0.1 else ''
            lines.append(f"  {var}: {coef:.4f}{sig} (p={pval:.4f})")
        
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {str(e)}"


@register_tool('head', 'Show first rows of data', ['file', 'n'])
def tool_head(file: str, n: str = "5") -> str:
    """Show first n rows"""
    from core_config import load_dataframe, OUT_DIR
    
    try:
        df = load_dataframe(str(OUT_DIR / file))
        if df is None:
            return "Error: Could not load file"
        n = int(n)
        return f"First {n} rows:\n{df.head(n).to_string()}"
    except Exception as e:
        return f"Error: {str(e)}"


@register_tool('columns', 'List all columns with types', ['file'])
def tool_columns(file: str) -> str:
    """List columns"""
    from core_config import load_dataframe, OUT_DIR
    
    try:
        df = load_dataframe(str(OUT_DIR / file))
        if df is None:
            return "Error: Could not load file"
        
        lines = [f"Shape: {df.shape}"]
        for col in df.columns:
            dtype = df[col].dtype
            nunique = df[col].nunique()
            lines.append(f"  {col} ({dtype}, {nunique} unique)")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {str(e)}"


@register_tool('trend', 'Analyze time trends', ['file', 'column'])
def tool_trend(file: str, column: str = None) -> str:
    """Analyze trends"""
    from core_config import load_dataframe, OUT_DIR
    
    try:
        df = load_dataframe(str(OUT_DIR / file))
        if df is None:
            return "Error: Could not load file"
        
        time_col = None
        for col in df.columns:
            if col in ('year', 'Year', '年份', '时间'):
                time_col = col
                break
        
        if not time_col:
            return "No time column found"
        
        if not column:
            for col in df.columns:
                if any(k in col.lower() for k in ['upgr', '升级', '消费']):
                    column = col
                    break
            if not column:
                column = df.select_dtypes(include='number').columns[0]
        
        if column not in df.columns:
            return f"Column '{column}' not found"
        
        trend = df.groupby(time_col)[column].mean()
        pct_change = trend.pct_change().mean() * 100
        
        lines = [f"Trend of {column} by {time_col}:"]
        lines.append(f"Average annual change: {pct_change:.2f}%")
        lines.append(f"Latest value: {trend.iloc[-1]:.4f}")
        lines.append(f"First value: {trend.iloc[0]:.4f}")
        lines.append(f"\nYearly values:")
        for year, val in trend.items():
            lines.append(f"  {year}: {val:.4f}")
        
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {str(e)}"


@register_tool('did_full', 'Run complete DID analysis (parallel trends, placebo, heterogeneity)', ['file'])
def tool_did_full(file: str) -> str:
    """Run DID analysis using did.py"""
    from core_config import load_dataframe, OUT_DIR
    from did import did_regression
    
    try:
        df = load_dataframe(str(OUT_DIR / file))
        if df is None:
            return "Error: Could not load file"
        
        dep_var = None
        for col in df.columns:
            if any(k in col.lower() for k in ['upgr', '升级', '消费结构']):
                dep_var = col
                break
        if not dep_var:
            dep_var = df.select_dtypes(include='number').columns[0]
        
        treat_col = None
        for col in df.columns:
            if any(k in col for k in ['treatrate', '试点', 'treat', '政策', '处理']):
                treat_col = col
                break
        if not treat_col:
            return "No treatment variable found"
        
        time_col = None
        for col in df.columns:
            if col in ('year', 'Year', '年份', '时间'):
                time_col = col
                break
        if not time_col:
            return "No time variable found"
        
        unit_col = None
        for col in df.columns:
            if col.lower() in ('province', 'city', '省份', '城市', 'id'):
                unit_col = col
                break
        if not unit_col:
            return "No unit variable found"
        
        control_vars = [c for c in df.select_dtypes(include='number').columns 
                       if c != dep_var and c != treat_col and c != time_col and c != unit_col]
        
        # 过滤高共线控制变量
        filtered = []
        for c in control_vars:
            try:
                cor = abs(df[c].corr(df[treat_col]))
                if cor < 0.9:
                    filtered.append(c)
            except Exception:
                filtered.append(c)
        control_vars = filtered[:10]
        
        lines = [f"DID Analysis: {dep_var} ~ {treat_col}"]
        lines.append(f"Controls: {len(control_vars)}")
        
        # Baseline
        result = did_regression(df, dep_var, treat_col, time_col, unit_col, control_vars)
        if 'params' in result:
            lines.append("\nBaseline DID:")
            for var, coef in result['params'].items():
                pval = result.get('pvalues', {}).get(var, 1)
                sig = '***' if pval < 0.01 else '**' if pval < 0.05 else '*' if pval < 0.1 else ''
                lines.append(f"  {var}: {coef:.4f}{sig} (p={pval:.4f})")
            lines.append(f"  R²: {result.get('r2', '-'):.4f}")
        elif 'error' in result:
            lines.append(f"\nBaseline error: {result['error']}")
        
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {str(e)}"


@register_tool('hypothesis_test', 'Run hypothesis test (t-test, ANOVA, chi-square, etc.)', ['file', 'test', 'col1', 'col2'])
def tool_hypothesis_test(file: str, test: str = 't-test', col1: str = '', col2: str = '') -> str:
    """Run hypothesis test via scipy directly"""
    import scipy.stats as stats
    import pandas as pd
    from core_config import load_dataframe, OUT_DIR
    
    try:
        df = load_dataframe(str(OUT_DIR / file))
        if df is None:
            return "Error: Could not load file"
        
        # Auto-detect columns
        if not col1:
            num_cols = df.select_dtypes(include='number').columns.tolist()
            if len(num_cols) >= 2:
                col1, col2 = num_cols[0], num_cols[1]
            elif len(num_cols) == 1:
                col1 = num_cols[0]
        
        if col1 not in df.columns:
            return f"Column '{col1}' not found"
        
        df = df.copy()
        df[col1] = pd.to_numeric(df[col1], errors='coerce')
        
        result = {}
        
        if test == 't-test':
            # 如果有 col2，做两样本 t 检验
            if col2 and col2 in df.columns:
                df[col2] = pd.to_numeric(df[col2], errors='coerce')
                a = df[col1].dropna()
                b = df[col2].dropna()
                r = stats.ttest_ind(a, b)
                result = {"statistic": round(float(r.statistic), 4), "p_value": round(float(r.pvalue), 6)}
            else:
                # 单样本 t 检验（与均值比较）
                r = stats.ttest_1samp(df[col1].dropna(), df[col1].mean())
                result = {"statistic": round(float(r.statistic), 4), "p_value": round(float(r.pvalue), 6)}
        
        elif test == 'anova':
            # 单因素 ANOVA
            groups = [g[col1].dropna().values for _, g in df.groupby(col2)] if col2 else [df[col1].dropna().values]
            r = stats.f_oneway(*groups)
            result = {"statistic": round(float(r.statistic), 4), "p_value": round(float(r.pvalue), 6)}
        
        elif test == 'chi-square':
            # 卡方检验
            contingency = pd.crosstab(df[col1], df[col2]) if col2 else pd.crosstab(df[col1], df[col1])
            r = stats.chi2_contingency(contingency)
            result = {"statistic": round(float(r[0]), 4), "p_value": round(float(r[1]), 6)}
        
        elif test == 'mannwhitney':
            # Mann-Whitney U 检验
            if col2 and col2 in df.columns:
                df[col2] = pd.to_numeric(df[col2], errors='coerce')
                a = df[col1].dropna()
                b = df[col2].dropna()
                r = stats.mannwhitneyu(a, b, alternative='two-sided')
                result = {"statistic": round(float(r.statistic), 4), "p_value": round(float(r.pvalue), 6)}
        
        elif test == 'shapiro':
            # Shapiro-Wilk 正态性检验
            sample = df[col1].dropna()
            if len(sample) > 5000:
                sample = sample.sample(5000)
            r = stats.shapiro(sample)
            result = {"statistic": round(float(r.statistic), 4), "p_value": round(float(r.pvalue), 6)}
        
        elif test == 'pearson':
            # Pearson 相关
            if col2 and col2 in df.columns:
                df[col2] = pd.to_numeric(df[col2], errors='coerce')
                r = stats.pearsonr(df[col1].dropna(), df[col2].dropna())
                result = {"correlation": round(float(r[0]), 4), "p_value": round(float(r[1]), 6)}
        
        elif test == 'spearman':
            # Spearman 相关
            if col2 and col2 in df.columns:
                df[col2] = pd.to_numeric(df[col2], errors='coerce')
                r = stats.spearmanr(df[col1].dropna(), df[col2].dropna())
                result = {"correlation": round(float(r[0]), 4), "p_value": round(float(r[1]), 6)}
        
        else:
            return f"Unknown test: {test}"
        
        lines = [f"Hypothesis Test: {test}"]
        lines.append(f"Columns: {col1}" + (f" vs {col2}" if col2 else ""))
        lines.append("")
        for key, val in result.items():
            lines.append(f"  {key}: {val}")
        
        # Conclusion
        pval = result.get('p_value', 1)
        if pval < 0.05:
            lines.append(f"\nConclusion: Statistically significant (p={pval:.4f} < 0.05)")
        else:
            lines.append(f"\nConclusion: Not statistically significant (p={pval:.4f} >= 0.05)")
        
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {str(e)}"


@register_tool('adf_test', 'ADF stationarity test for time series', ['file', 'column'])
def tool_adf_test(file: str, column: str = '') -> str:
    """ADF stationarity test"""
    from statsmodels.tsa.stattools import adfuller
    from core_config import load_dataframe, OUT_DIR
    
    try:
        df = load_dataframe(str(OUT_DIR / file))
        if df is None:
            return "Error: Could not load file"
        
        if not column:
            column = df.select_dtypes(include='number').columns[0]
        
        if column not in df.columns:
            return f"Column '{column}' not found"
        
        series = df[column].dropna()
        result = adfuller(series)
        
        lines = [f"ADF Stationarity Test: {column}"]
        lines.append(f"  Test statistic: {result[0]:.4f}")
        lines.append(f"  p-value: {result[1]:.4f}")
        lines.append(f"  Lags used: {result[2]}")
        lines.append(f"  Observations: {result[3]}")
        lines.append(f"  Critical values:")
        for key, val in result[4].items():
            lines.append(f"    {key}: {val:.4f}")
        
        if result[1] < 0.05:
            lines.append(f"\nConclusion: Stationary (p={result[1]:.4f} < 0.05)")
        else:
            lines.append(f"\nConclusion: Non-stationary (p={result[1]:.4f} >= 0.05)")
        
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {str(e)}"


@register_tool('arima', 'ARIMA/SARIMA time series forecasting', ['file', 'column', 'periods'])
def tool_arima(file: str, column: str = '', periods: str = '12') -> str:
    """ARIMA forecasting using statsmodels"""
    from core_config import load_dataframe, OUT_DIR
    
    try:
        if not column:
            df = load_dataframe(str(OUT_DIR / file))
            column = df.select_dtypes(include='number').columns[0]
        
        from statsmodels.tsa.arima.model import ARIMA
        import pandas as pd
        
        df = load_dataframe(str(OUT_DIR / file))
        series = df[column].dropna()
        
        # Fit ARIMA(1,1,1)
        model = ARIMA(series, order=(1, 1, 1))
        fitted = model.fit()
        
        # Forecast
        forecast = fitted.forecast(steps=int(periods))
        
        lines = [f"ARIMA Forecast: {column}"]
        lines.append(f"\nForecast ({periods} periods):")
        for i, val in enumerate(forecast, 1):
            lines.append(f"  Period {i}: {val:.4f}")
        
        lines.append(f"\nModel: ARIMA(1,1,1)")
        lines.append(f"AIC: {fitted.aic:.2f}")
        lines.append(f"BIC: {fitted.bic:.2f}")
        
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {str(e)}"


@register_tool('ml_train', 'Train a ML model (regression/classification)', ['file', 'target', 'task'])
def tool_ml_train(file: str, target: str = '', task: str = 'regression') -> str:
    """Train ML model using sklearn"""
    from core_config import OUT_DIR

    try:
        df, _e = _load_guarded(file)
        if _e:
            return _e
        if df is None:
            return "Error: Could not load file"
        
        # Auto-detect target
        if not target:
            for col in df.columns:
                if any(k in col.lower() for k in ['upgr', '升级', '消费']):
                    target = col
                    break
            if not target:
                target = df.select_dtypes(include='number').columns[-1]
        
        if target not in df.columns:
            return f"Target '{target}' not found"
        
        from sklearn.model_selection import train_test_split
        from sklearn.ensemble import RandomForestRegressor, RandomForestClassifier
        from sklearn.metrics import mean_squared_error, r2_score, accuracy_score
        
        features = [c for c in df.select_dtypes(include='number').columns if c != target]
        X = df[features].fillna(df[features].median())
        y = df[target].fillna(df[target].median())
        
        X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
        
        if task == 'regression':
            model = RandomForestRegressor(n_estimators=100, random_state=42)
            model.fit(X_train, y_train)
            y_pred = model.predict(X_test)
            mse = mean_squared_error(y_test, y_pred)
            r2 = r2_score(y_test, y_pred)
            
            lines = [f"ML Model: Random Forest Regression"]
            lines.append(f"Target: {target}")
            lines.append(f"Features: {len(features)}")
            lines.append(f"Samples: {len(X)}")
            lines.append(f"")
            lines.append(f"MSE: {mse:.4f}")
            lines.append(f"R²: {r2:.4f}")
        else:
            model = RandomForestClassifier(n_estimators=100, random_state=42)
            model.fit(X_train, y_train)
            y_pred = model.predict(X_test)
            acc = accuracy_score(y_test, y_pred)
            
            lines = [f"ML Model: Random Forest Classification"]
            lines.append(f"Target: {target}")
            lines.append(f"Features: {len(features)}")
            lines.append(f"Samples: {len(X)}")
            lines.append(f"")
            lines.append(f"Accuracy: {acc:.4f}")
        
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {str(e)}"


@register_tool('feature_eng', 'Auto feature engineering (create new features)', ['file'])
def tool_feature_eng(file: str) -> str:
    """Auto feature engineering"""
    from core_config import OUT_DIR

    try:
        df, _e = _load_guarded(file)
        if _e:
            return _e
        if df is None:
            return "Error: Could not load file"
        
        num_cols = df.select_dtypes(include='number').columns.tolist()
        
        lines = [f"Feature Engineering Suggestions:"]
        lines.append(f"Original features: {len(num_cols)}")
        lines.append("")
        
        # Suggest interactions for highly correlated pairs
        if len(num_cols) >= 2:
            corr = df[num_cols].corr()
            high_corr_pairs = []
            for i in range(len(num_cols)):
                for j in range(i+1, len(num_cols)):
                    c = corr.iloc[i, j]
                    if abs(c) > 0.7 and abs(c) < 0.99:
                        high_corr_pairs.append((num_cols[i], num_cols[j], c))
            
            if high_corr_pairs:
                lines.append("Highly correlated pairs (interaction candidates):")
                for col1, col2, c in high_corr_pairs[:5]:
                    lines.append(f"  {col1} × {col2}: r={c:.4f}")
                lines.append("")
        
        # Suggest polynomial features for key variables
        lines.append("Suggested polynomial features:")
        for col in num_cols[:3]:
            lines.append(f"  {col}² (squared)")
        
        # Suggest lag features if time series
        time_col = None
        for col in df.columns:
            if col in ('year', 'Year', '年份', '时间'):
                time_col = col
                break
        
        if time_col:
            unit_col = next((c for c in df.columns if c.lower() in ('province', '省份')), None)
            if unit_col:
                lines.append(f"\nLag features (for panel data):")
                lines.append(f"  L1_lag: 1-period lag of target variable")
                lines.append(f"  L2_lag: 2-period lag of target variable")
        
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {str(e)}"


def _load_guarded(file: str):
    """带写入/读取边界校验的 load_dataframe（P1 工具执行边界，内部助手函数）。

    统一入口的原因：`clean_data` / `feature_eng` / `ml_train` 三个工具原先是
    `load_dataframe(str(OUT_DIR / file))` —— 若模型给出含 `..` 或绝对路径的 `file`，
    `Path.__truediv__` 会直接越出 OUT_DIR（`Path('/a') / '/etc/x'` == `/etc/x`）。
    这三个工具都会**产出并落盘**，越界读写后果同源，故共用本守卫。

    返回 `(df, None)` 或 `(None, 错误说明)`。
    """
    from core_config import load_dataframe
    return guarded_load(file, load_dataframe)


# P0 修复：此前装饰器错把 `_load_guarded` 注册为 clean_data 工具（返回 (df, err)
# 元组直接灌给 LLM，真实清洗逻辑 tool_clean_data 反而注册不上）
@register_tool('clean_data', 'Clean data (handle missing values, outliers)', ['file'])
def tool_clean_data(file: str) -> str:
    """Clean data: handle missing values and outliers"""
    from core_config import OUT_DIR

    try:
        df, _e = _load_guarded(file)
        if _e:
            return _e
        if df is None:
            return "Error: Could not load file"
        
        lines = [f"Data Quality Report: {file}"]
        lines.append(f"Shape: {df.shape}")
        lines.append("")
        
        # Missing values
        missing = df.isnull().sum()
        missing_cols = missing[missing > 0]
        if len(missing_cols) > 0:
            lines.append("Missing values:")
            for col, n in missing_cols.items():
                pct = n / len(df) * 100
                lines.append(f"  {col}: {n} ({pct:.1f}%)")
        else:
            lines.append("No missing values found")
        
        # Outliers (Z-score > 3)
        num_cols = df.select_dtypes(include='number').columns
        outlier_count = 0
        for col in num_cols:
            series = df[col].dropna()
            if len(series) > 0:
                z = ((series - series.mean()) / series.std()).abs()
                n_outliers = (z > 3).sum()
                if n_outliers > 0:
                    outlier_count += n_outliers
        
        lines.append(f"\nOutliers (|z|>3): {outlier_count}")
        
        # Duplicate rows
        n_dup = df.duplicated().sum()
        lines.append(f"Duplicate rows: {n_dup}")
        
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {str(e)}"


@register_tool('generate_chart', 'Generate chart (scatter, line, bar, etc.)', ['file', 'chart_type', 'col1', 'col2'])
def tool_generate_chart(file: str, chart_type: str = 'scatter', col1: str = '', col2: str = '') -> str:
    """Generate chart directly using matplotlib"""
    from core_config import load_dataframe, OUT_DIR
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import pandas as pd
    import io
    import base64
    
    try:
        df = load_dataframe(str(OUT_DIR / file))
        if df is None:
            return "Error: Could not load file"
        
        # Auto-detect columns
        num_cols = df.select_dtypes(include='number').columns.tolist()
        if not col1 and len(num_cols) >= 1:
            col1 = num_cols[0]
        if not col2 and len(num_cols) >= 2:
            col2 = num_cols[1]
        
        if not col1:
            return "Need at least 1 numeric column"
        
        # Create chart
        fig, ax = plt.subplots(figsize=(10, 6))
        
        if chart_type == 'scatter' and col2:
            ax.scatter(df[col1], df[col2], alpha=0.6)
        elif chart_type == 'line':
            ax.plot(df[col1].values[:100], label=col1)
            if col2:
                ax.plot(df[col2].values[:100], label=col2)
            ax.legend()
        elif chart_type == 'bar':
            ax.bar(range(min(20, len(df))), df[col1].values[:20])
        elif chart_type == 'hist':
            ax.hist(df[col1].dropna(), bins=30)
        elif chart_type == 'box':
            ax.boxplot([df[col1].dropna(), df[col2].dropna()] if col2 else [df[col1].dropna()])
        else:
            ax.plot(df[col1].values[:100])
        
        ax.set_title(f'{chart_type}: {col1}' + (f' vs {col2}' if col2 else ''))
        ax.set_xlabel(col1)
        ax.set_ylabel(col2 or col1)
        
        # Save to base64
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
        buf.seek(0)
        img_base64 = base64.b64encode(buf.read()).decode('utf-8')
        plt.close()
        
        return f"Chart generated: data:image/png;base64,{img_base64[:50]}... (truncated, {len(img_base64)} chars total)"
    except Exception as e:
        return f"Error: {str(e)}"


@register_tool('company_analysis',
               'Company comprehensive analysis (A-share/HK): profile, financials, market, valuation (DCF+relative), news, competitors, SWOT and risks. Input company name (e.g. 贵州茅台, 腾讯) or code (600519, HK0700). Returns a structured full report',
               ['company'])
def tool_company_analysis(company: str = '', company_name: str = '') -> str:
    """公司全方位分析：概况/财务/市场/估值/新闻/竞争对手/SWOT/风险（调用后端真实数据）"""
    company = company or company_name  # 兼容模型输出 company_name 参数名
    try:
        from company_analysis import analyze_company
        r = analyze_company(company)
        if not r.get('ok'):
            return f"Error: {r.get('error', '分析失败')}"
        m = r['market']; f = r['financial']; pr = r['profile']
        lines = [f"# {r['name']}（{r['code']}）全方位分析",
                 f"- 行业: {pr.get('industry') or '暂缺'} | 市值: {m.get('market_cap_yi')}亿 | 现价: {m.get('price')}元",
                 f"- 财务: 营收{f.get('rev_yi')}亿(同比{f.get('rev_growth')})%, 净利{f.get('net_profit_yi')}亿(同比{f.get('profit_growth')})%, ROE={f.get('roe')}%",
                 f"- 估值: PE={m.get('pe')}, PB={m.get('pb')}, 毛利率={m.get('gross_margin')}%, 资产负债率={m.get('debt_ratio')}%, 52周[{m.get('low_52')}-{m.get('high_52')}]"]
        dcf = (r.get('valuation') or {}).get('dcf')
        if dcf:
            lines.append(f"- DCF: 内在价值{dcf.get('per_share')} vs 现价{dcf.get('price')}，结论{dcf.get('verdict_cn')}")
        peer = (r.get('valuation') or {}).get('peer')
        if peer and peer.get('verdict'):
            lines.append(f"- 相对估值: {peer['verdict']['label']}")
        if r.get('news'):
            lines.append("- 近期新闻: " + "; ".join(n['title'][:40] for n in r['news'][:3]))
        lines.append("- 竞争对手: " + ", ".join(c['name'] for c in r['competitors']))
        lines.append("- SWOT优势: " + "; ".join(r['swot']['strengths'][:3]))
        lines.append("- SWOT劣势: " + "; ".join(r['swot']['weaknesses'][:3]))
        lines.append("- 风险: " + "; ".join(f"{x['name']}({x['level']})" for x in r['risks'][:3]))
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {str(e)}"


# 企业深析：Agent 可复用的最近一次流水线状态（进程内缓存）
# 会话级状态容器（原为模块级裸全局，waitress 多线程下会把 A 的分析结果串给 B）
from company_deep_session import (get_state as _cd_state, set_state as _cd_store,
                                  get_name as _cd_name, set_name as _cd_rename)
from company_deep_rules import moat_avg as _cd_moat_avg


@register_tool('industry_stats',
               'Industry aggregates for a company (auto industry detection): industry scale (revenue, 亿元), '
               'revenue YoY, company market share, CR3/CR5 (A-share, eastmoney); falls back to profit-basis aggregates (cninfo) for HK/missing',
               ['company'])
def tool_industry_stats(company: str = '') -> str:
    """行业聚合数据：行业规模/增速/市占率/CR3/CR5（A股东财营收口径，港股/缺失回退巨潮利润口径）"""
    company = (company or '').strip()
    if not company:
        return "Error: 请提供公司名或代码"
    try:
        from company_analysis import (resolve_ticker, get_company_profile,
                                      get_industry_stats_em, get_industry_aggregates,
                                      get_financial_analysis)
        code, mkt = resolve_ticker(company)
        if not code:
            return f"Error: 无法识别公司：{company}"
        em = get_industry_stats_em(code) or {}
        if em.get('industry'):
            lines = [f"# {em['industry']} 行业聚合（东财业绩报表 {em.get('report_date', '')}，营收口径）",
                     f"- 行业规模：{em.get('industry_rev_yi')} 亿元（{em.get('company_count')} 家上市公司）"]
            if em.get('rev_yoy') is not None:
                lines.append(f"- 行业增速（营收同比）：{em['rev_yoy']}%")
            if em.get('share_rev') is not None:
                lines.append(f"- 公司市占率：{em['share_rev']}%（营收 {em.get('company_rev_yi')} 亿元）")
            if em.get('cr3') is not None:
                lines.append(f"- CR3：{em['cr3']}%    CR5：{em.get('cr5')}%")
            if em.get('peers_top'):
                lines.append("- 营收前五：" + "、".join(em['peers_top'][:5]))
            return "\n".join(lines)
        # 回退：巨潮利润口径（需行业名匹配）
        prof = get_company_profile(code, mkt)
        fa = get_financial_analysis(code, mkt)
        np_yi = ((fa.get('ratios') or {}).get('profitability') or {}).get('net_profit')
        agg = get_industry_aggregates(prof.get('industry') or '', np_yi) or {}
        if not agg:
            return f"Error: {prof.get('industry') or '该公司'} 暂无可用的行业聚合数据（免费渠道覆盖 A 股为主）"
        lines = [f"# {agg.get('industry')} 行业聚合（巨潮 {agg.get('report_date', '')}，利润口径）",
                 f"- 上市公司家数：{agg.get('company_count')}    总市值：{agg.get('total_mcap_yi')} 亿元"]
        if agg.get('net_profit_yi') is not None:
            lines.append(f"- 行业净利润合计：{agg['net_profit_yi']} 亿元")
        if agg.get('net_profit_yoy') is not None:
            lines.append(f"- 行业净利润同比：{agg['net_profit_yoy']}%（可作行业增速参考）")
        if agg.get('share_profit') is not None:
            lines.append(f"- 公司利润占比（市占率近似）：{agg['share_profit']}%")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {str(e)}"


@register_tool('company_source_fetch',
               'Fetch public materials for a company from a named source (IPO prospectus, annual report, CNINFO announcements, research reports, court records, patents, etc.). Returns titles/links/snippets',
               ['company'])
def tool_company_source_fetch(company: str = '', source_id: str = '') -> str:
    """企业深析信源检索：按信源抓取该公司公开资料（标题/链接/摘要）"""
    company = (company or '').strip()
    if not company:
        return "Error: 请提供公司名或代码"
    try:
        from company_analysis import resolve_ticker
        from company_deep_sources import fetch_source
        code, mkt = resolve_ticker(company)
        if not code:
            return f"Error: 无法识别公司：{company}"
        sid = (source_id or 'report').strip().lower()
        name = company if not code.startswith('HK') else company
        r = fetch_source(sid, name=company, code=code, market=('港股' if code.startswith('HK') else 'A股'), top_k=5)
        if not r.get('ok'):
            return f"Error: {r.get('error', '检索失败')}（信源 {sid}，检索词 {r.get('query', '')}）"
        lines = [f"# {r.get('title')}（{company}）检索词：{r.get('query', '')}"]
        for i, it in enumerate((r.get('items') or [])[:5], 1):
            lines.append(f"{i}. {it.get('title', '')}\n   {it.get('url', '')}")
        lines.append("提示：链接可交由用户点击查看原文；摘要仅供研究参考。")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {str(e)}"


def _cd_format_verdict_markdown(result: dict) -> str:
    """把流水线结果整理成 Agent 可直接引用的 Markdown 结论。"""
    st = result.get("state") or {}
    meta = result.get("meta") or {}
    c = st.get("company") or {}
    fin = st.get("finance") or {}
    model = st.get("model") or {}
    val = st.get("val") or {}
    concl = st.get("conclusions") or {}
    risks = st.get("risks") or []
    stages = result.get("stages") or []

    lines = [
        f"# 企业深度分析 · {c.get('name') or '未知'}（{c.get('code') or '—'}）",
        f"- 行业: {c.get('industry') or '—'} | 市场: {c.get('market') or '—'} | 分析日期: {c.get('date') or '—'}",
        f"- 营收 {fin.get('rev') or '—'} 亿 | 净利 {fin.get('np') or '—'} 亿 | 毛利率 {model.get('gm') or '—'}% | "
        f"净利率 {model.get('nm') or '—'}% | ROE {fin.get('roe') or '—'}%",
        f"- PE {val.get('pe') or '—'} | PB {val.get('pb') or '—'} | 价格结论: {val.get('verdict') or '待定'}",
        f"- 流水线: {result.get('elapsed_sec')}s | 草稿来源: {meta.get('draft_source') or 'rules'} | "
        f"阶段 {'/'.join(s.get('id','') + ':' + s.get('status','') for s in stages)}",
        "",
        "## 一页纸判断卡（五问）",
    ]
    for i, key in enumerate(("a1", "a2", "a3", "a4", "a5"), 1):
        q = ["这门生意好不好", "这家公司强不强", "账靠不靠谱", "谁在管、有什么风险", "当前价格值不值"][i - 1]
        lines.append(f"{i}. **{q}**：{concl.get(key) or '（待补充）'}")

    if risks:
        lines.append("")
        lines.append("## 主要风险")
        for r in risks[:6]:
            lines.append(f"- [{r.get('level')}] {r.get('type')}：{r.get('text')}")

    # 护城河（P0-4：六维全列 + 打分公式 + 均分算式，单一实现在 company_deep_rules）
    m = st.get("moat") or {}
    if m:
        from company_deep_rules import moat_breakdown as _moat_bd
        lines.append("")
        lines.append(f"## 护城河六维均分 {_cd_moat_avg(m):.1f}/10")
        lines.append(_moat_bd(m))

    lines.append("")
    lines.append("说明：财务与行情来自公开数据接口；定性判断为规则/AI 初稿，关键结论需人工复核招股书与年报。")
    lines.append("本结论为客观数据分析，仅供参考，不构成任何投资建议。")
    return "\n".join(lines)


@register_tool('company_deep_analyze',
               'Run the full company deep-analysis pipeline (8 stages: resolve→profile→finance→market→news→risk→draft→assemble). Pulls real market/financial data, red-flag rules, optional AI draft, then returns a structured one-page judgment (5 questions) plus key metrics. Prefer this over company_analysis when the user wants a complete deep-dive research card.',
               ['company', 'use_ai'])
def tool_company_deep_analyze(company: str = '', company_name: str = '', use_ai: bool = True) -> str:
    """企业深析自动化流水线：真实数据 + 规则排雷 + AI/规则草稿 → 一页纸判断卡"""
    company = company or company_name
    if not company:
        return "Error: 请提供公司名称或股票代码"
    try:
        if isinstance(use_ai, str):
            use_ai = str(use_ai).lower() not in ("0", "false", "no", "否")
        from company_deep_pipeline import run_pipeline
        result = run_pipeline(company, use_ai=bool(use_ai))
        if not result.get("ok"):
            return f"Error: 流水线失败 - {result.get('error')}"
        _cd_store(result.get("state") or {})
        _cd_rename(((result.get("state") or {}).get("company") or {}).get("name") or company)
        return _cd_format_verdict_markdown(result)
    except Exception as e:
        return f"Error: {str(e)}"


@register_tool('company_deep_export_report',
               'Export the company deep-analysis result to a Word (.docx) or PowerPoint (.pptx) report file saved under the workspace output folder. Reuses the last company_deep_analyze result unless company is given (then re-runs the pipeline first).',
               ['company', 'format'])
def tool_company_deep_export_report(company: str = '', company_name: str = '',
                                    format: str = 'docx', template: str = '') -> str:
    """导出企业深析 Word/PPT 报告到 output 目录，返回可下载路径"""
    company = company or company_name
    fmt = (format or 'docx').lower()
    if fmt not in ('docx', 'pptx'):
        return "Error: format 仅支持 docx 或 pptx"
    tpl = (template or 'navy_business').strip() or 'navy_business'

    state = _cd_state()
    need_run = False
    if not state:
        if not company:
            return "Error: 尚无分析结果。请先调用 company_deep_analyze，或在 company 参数中提供公司名/代码。"
        need_run = True
    elif company:
        last_id = f"{((state.get('company') or {}).get('name') or '')} {((state.get('company') or {}).get('code') or '')}".strip()
        if company not in last_id and last_id not in company:
            need_run = True

    try:
        if need_run:
            from company_deep_pipeline import run_pipeline
            result = run_pipeline(company, use_ai=True)
            if not result.get("ok"):
                return f"Error: 流水线失败 - {result.get('error')}"
            state = result.get("state") or {}
            _cd_store(state)
            _cd_rename(((state.get("company") or {}).get("name")) or company)

        from company_deep_report import generate_docx, generate_pptx, safe_filename, resolve_theme
        from core_config import OUT_DIR
        name = (state.get("company") or {}).get("name") or company or "企业深度分析"
        th = resolve_theme(tpl)
        payload = generate_pptx(state, template=th["id"]) if fmt == "pptx" else generate_docx(state)

        fname = safe_filename(name, fmt)
        # 路径约束到 OUT_DIR 内
        out_path, path_err = safe_output_path(fname)
        if path_err or not out_path:
            out_path = Path(OUT_DIR) / fname
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(payload)

        return (
            f"已生成{fmt.upper()}报告：{out_path}\n"
            f"公司：{name}\n"
            f"模板：{th['name']}（{th['id']}）\n"
            f"文件大小：{len(payload)} 字节\n"
            f"提示：文件在服务端 output 目录，可在网站「报告」或文件管理中查看/下载。"
        )
    except Exception as e:
        return f"Error: {str(e)}"


@register_tool('company_deep_list_templates',
               'List built-in PPT report templates (id, name, use-case, cover style) for company deep-analysis slides.',
               [])
def tool_company_deep_list_templates() -> str:
    """列出内置 PPT 汇报模板"""
    try:
        from company_deep_report import list_ppt_templates
        rows = list_ppt_templates()
        lines = ["内置 PPT 汇报模板（export 时 template 填 id 或中文名）："]
        for t in rows:
            lines.append(f"- **{t['id']}**｜{t['name']}｜{t['desc']}｜封面:{t['cover']}")
        lines.append("中文别名：商务深蓝/深蓝、科技深色/科技、增长青绿/绿色、简约汇报/简约、投研备忘录/投研")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {str(e)}"


@register_tool('company_deep_dcf',
               'Quick Gordon-growth DCF intrinsic value: given FCF (100M CNY), perpetual growth g%, WACC%, shares (100M), optional net cash. Returns enterprise value, equity value, per-share value and a rough fair-price note.',
               ['fcf', 'g', 'wacc', 'shares', 'net_cash'])
def tool_company_deep_dcf(fcf: float = None, g: float = 2.0, wacc: float = 9.0,
                          shares: float = None, net_cash: float = 0.0,
                          fcf_yi: float = None, g_pct: float = None,
                          wacc_pct: float = None, shares_yi: float = None) -> str:
    """简易 DCF（Gordon）内在价值测算"""
    try:
        fcf_v = fcf if fcf is not None else fcf_yi
        g_v = g if g_pct is None else g_pct
        w_v = wacc if wacc_pct is None else wacc_pct
        sh_v = shares if shares is not None else shares_yi
        if fcf_v is None:
            return "Error: 请提供 fcf（未来 5 年 FCF 均值，亿元）"
        fcf_v, g_v, w_v = float(fcf_v), float(g_v), float(w_v)
        sh = float(sh_v) if sh_v not in (None, "") else None
        nc = float(net_cash or 0)
        w, gg = w_v / 100.0, g_v / 100.0
        if w <= gg:
            return "Error: WACC 必须大于永续增长率 g"
        ev = fcf_v * (1 + gg) / (w - gg)
        eq = ev + nc
        lines = [
            f"【DCF 简算】FCF={fcf_v}亿, g={g_v}%, WACC={w_v}%",
            f"- 企业价值 EV ≈ {ev:.1f} 亿元",
            f"- 股权价值 ≈ {eq:.1f} 亿元（净现金 {nc} 亿）",
        ]
        if sh and sh > 0:
            ps = eq / sh
            lines.append(f"- 每股内在价值 ≈ **¥{ps:.2f}**（股本 {sh} 亿股）")
            lines.append(f"- 提示：市价低于该值约有安全边际；高于则偏贵（粗算，非精确定价）。")
        else:
            lines.append("- 提供 shares（亿股）可换算每股价值")
        lines.append("本结论为客观数据分析，仅供参考，不构成任何投资建议。")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {str(e)}"


@register_tool('company_deep_redflags',
               'Run the cash-flow / accounting red-flag checklist on financial numbers (OCF vs NP, receivables growth vs revenue, goodwill, dual cash-debt, leverage). Uses last analyze state if numbers omitted.',
               ['ocf', 'net_profit', 'ar_growth', 'revenue_growth', 'goodwill', 'equity', 'cash', 'interest_debt', 'assets'])
def tool_company_deep_redflags(ocf=None, net_profit=None, ar_growth=None, revenue_growth=None,
                               goodwill=None, equity=None, cash=None, interest_debt=None,
                               assets=None, inventory_growth=None) -> str:
    """现金流照妖镜 / 排雷清单扫描（口径委托 company_deep_rules，**唯一实现**）。

    原先此处把 6 条规则与阈值**逐条复制**自 company_deep_report.derive_metrics，
    且缺少网页实时区的第 7 条「利润增长与毛利率背离」→ AI 工具与网页/导出三处口径不一。
    """
    try:
        from company_deep_rules import redflag_alerts, norm_fin
        st = (_cd_state() or {}).get("finance") or {}
        given = {"ocf": ocf, "np": net_profit, "arG": ar_growth, "revG": revenue_growth,
                 "gw": goodwill, "equity": equity, "cash": cash, "idebt": interest_debt,
                 "assets": assets, "invG": inventory_growth}
        # 显式入参优先，缺省回落"上次分析状态"（会话级）
        merged = {k: (v if v not in (None, "") else st.get(k)) for k, v in given.items()}
        alerts = redflag_alerts(norm_fin(merged))
        if not alerts:
            return ("排雷扫描：未触发主要信号（或数字不足）。"
                    "请补充 ocf/net_profit/revenue_growth 等，或先运行 company_deep_analyze。")
        lines = ["【现金流照妖镜 · 排雷结果】（单一实现，与网页/导出文档同口径）"]
        for a in alerts:
            mark = "⚠" if a["level"] == "danger" else "!"
            lines.append(f"{mark} **{a['label']}**：{a['note']}")
        lines.append("需人工核对年报附注与多年趋势，单点数字不足以定性。")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {str(e)}"


def tool_company_deep_moat_score(tech=None, brand=None, channel=None, cost=None,
                                 network=None, license=None) -> str:
    """护城河六维评分"""
    try:
        m = (_cd_state() or {}).get("moat") or {}

        def pick(val, key, default=5):
            if val not in (None, ""):
                return max(0.0, min(10.0, float(val)))
            try:
                return max(0.0, min(10.0, float(m.get(key, default))))
            except (TypeError, ValueError):
                return float(default)

        dims = {
            "技术/专利": pick(tech, "tech"),
            "品牌心智": pick(brand, "brand"),
            "渠道网络": pick(channel, "channel"),
            "成本/规模": pick(cost, "cost"),
            "网络效应": pick(network, "network"),
            "牌照/资质": pick(license, "license"),
        }
        avg = sum(dims.values()) / 6.0
        grade = "护城河较深" if avg >= 7 else ("有一定壁垒" if avg >= 5 else ("壁垒偏薄" if avg >= 3 else "几乎无壁垒"))
        lines = [f"【护城河评分】均分 **{avg:.1f}/10**（{grade}）"]
        for k, v in dims.items():
            bar = "█" * int(round(v)) + "·" * (10 - int(round(v)))
            lines.append(f"- {k}: {v:.0f} {bar}")
        lines.append("解读：≥7 守得住高毛利；5–6 需持续投入；≤4 高利润易被侵蚀。")
        # P0-4：均分算式透明化（Σ/6 写出过程）
        expr = '+'.join(f'{v:.0f}' for v in dims.values())
        lines.append(f"均分 = ({expr}) ÷ 6 = {avg:.1f}（各维打分公式见 company_deep_rules.MOAT_LABELS）")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {str(e)}"


@register_tool('company_deep_playbook',
               'Competitor war-game / playbook: given our company (from last analyze or company=) and rival, produce attack options, our defense, impact level, and investment implication. Use when user asks how competitors will fight or whether we can defend.',
               ['rival', 'impact'])
def tool_company_deep_playbook(rival: str = '', impact: str = '', company: str = '',
                               our_weakness: str = '', rival_move: str = '') -> str:
    """竞争推演：站在对手立场推演打法与守不守得住"""
    try:
        st = _cd_state() or {}
        c = st.get("company") or {}
        name = company or c.get("name") or "目标公司"
        m = st.get("moat") or {}
        f = st.get("finance") or {}
        model = st.get("model") or {}
        avg = 5.0
        try:
            avg = _cd_moat_avg(m)
        except Exception:
            pass
        gm = model.get("gm") or f.get("gm") or "—"
        weak = our_weakness or model.get("weakest") or "客户/供应商集中度或回款质量（待核实）"
        rival_name = rival or (st.get("pb") or {}).get("rival") or "行业主要对手"
        move = rival_move or (st.get("pb") or {}).get("move") or "价格战 / 渠道下沉 / 产品换代 / 挖核心团队"

        defense = "护城河可挡" if avg >= 7 else ("需资源应对" if avg >= 5 else "难防守")
        level = impact or ("重大" if avg < 5 else "中等")

        lines = [
            f"【竞争推演】{name} vs {rival_name}",
            f"- 我方软肋：{weak}",
            f"- 对手最可能打法：{move}",
            f"- 护城河 {avg:.1f}/10 → 防守能力：**{defense}**；冲击评估：**{level}**",
            "",
            "## 对手组合拳（推演）",
            f"1. 短期：以性价比/渠道返利冲击核心客户（毛利率 {gm}% 的定价空间是攻防点）",
            "2. 中期：对标产品迭代或绑定渠道独家，压缩我方份额",
            "3. 长期：技术换代或资本开支扩产，改变行业成本曲线",
            "",
            "## 我方应对",
            f"1. 守住最脆弱一环：{weak}",
            "2. 用服务/转换成本/品牌溢价稳住核心客户",
            "3. 在对手薄弱细分市场反打，避免全面价格战",
            "",
            f"## 投资含义",
            f"- 若 {level} 冲击落地：关注毛利率与市占率是否同步下滑",
            f"- 当前判断：{defense}，估值需预留安全边际",
            "本结论为客观数据分析，仅供参考，不构成任何投资建议。",
        ]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {str(e)}"


@register_tool('company_deep_fill',
               'Update / fill company deep-analysis card fields (conclusions a1-a5, risks, moat dims, valuation verdict, etc.) on the last analyze state. Use to inject human notes or your refined judgment before export.',
               ['fields_json'])
def tool_company_deep_fill(fields_json: str = '', conclusions: str = '',
                           a1: str = '', a2: str = '', a3: str = '', a4: str = '', a5: str = '',
                           risk: str = '', risk_level: str = '中', risk_type: str = '经营',
                           valuation_verdict: str = '') -> str:
    """回填分析卡字段（结论五问 / 风险 / 估值结论），便于导出前润色"""
    try:
        if not _cd_state():
            return "Error: 尚无分析状态。请先 company_deep_analyze。"
        import json as _json
        st = _cd_state()

        if fields_json:
            patch = _json.loads(fields_json) if isinstance(fields_json, str) else fields_json
            if isinstance(patch, dict):
                for k, v in patch.items():
                    if isinstance(v, dict) and isinstance(st.get(k), dict):
                        st[k].update(v)
                    else:
                        st[k] = v

        for key, val in (("a1", a1), ("a2", a2), ("a3", a3), ("a4", a4), ("a5", a5)):
            if val:
                st.setdefault("conclusions", {})[key] = val
        if conclusions:
            # 允许整段 JSON
            try:
                obj = _json.loads(conclusions)
                if isinstance(obj, dict):
                    st.setdefault("conclusions", {}).update(obj)
            except Exception:
                st.setdefault("conclusions", {})["a1"] = conclusions

        if risk:
            st.setdefault("risks", []).append({
                "type": risk_type or "经营",
                "level": risk_level or "中",
                "text": str(risk)[:300],
            })
        if valuation_verdict:
            st.setdefault("val", {})["verdict"] = valuation_verdict

        _cd_store(st)
        concl = st.get("conclusions") or {}
        return (
            "分析卡已更新。\n"
            + "；".join(f"{k}={bool(concl.get(k))}" for k in ("a1", "a2", "a3", "a4", "a5"))
            + f"；风险数={len(st.get('risks') or [])}；估值={ (st.get('val') or {}).get('verdict') }"
            + "\n可继续 company_deep_export_report 生成文档。"
        )
    except Exception as e:
        return f"Error: {str(e)}"


@register_tool('company_deep_scorecard',
               'One-shot scorecard from last analyze (or numbers): financial health score, DuPont breakdown, moat average, risk density, and valuation snapshot. Best for mid-conversation status checks.',
               [])
def tool_company_deep_scorecard() -> str:
    """综合记分卡：财务健康 / 杜邦 / 护城河 / 风险 / 估值一屏汇总"""
    try:
        from company_deep_report import derive_metrics
        if not _cd_state():
            return "Error: 尚无分析状态。请先 company_deep_analyze。"
        st = _cd_state()
        met = derive_metrics(st)
        f = st.get("finance") or {}
        v = st.get("val") or {}
        c = st.get("company") or {}
        risks = st.get("risks") or []
        high = sum(1 for r in risks if r.get("level") == "高")
        lines = [
            f"【记分卡】{c.get('name') or '—'}（{c.get('code') or '—'}）",
            f"- 财务健康分 **{met['health']}/100**｜毛利率 {met['gm'] and round(met['gm'],1)}%｜净利率 {met['nm'] and round(met['nm'],1)}%｜ROE {f.get('roe') or '—'}%",
            f"- 杜邦：ROE≈{met['du_roe'] and round(met['du_roe'],1)}% = 净利率×周转{met['tat'] and round(met['tat'],2)}×权益乘数{met['em'] and round(met['em'],2)}",
            f"- 现金含量 OCF/NP **{met['ocf_np'] and round(met['ocf_np'],2)}**｜资产负债率 {met['debt_ratio'] and round(met['debt_ratio'],1)}%",
            f"- 护城河 **{met['moat_avg']:.1f}/10**｜风险 {len(risks)} 条（高风险 {high}）",
            f"- 估值：PE {v.get('pe') or '—'}｜PB {v.get('pb') or '—'}｜结论 {v.get('verdict') or '待定'}",
        ]
        if met["alerts"]:
            lines.append("排雷信号：" + "；".join(f"{t}" for t, _d in met["alerts"][:4]))
        lines.append("本结论为客观数据分析，仅供参考，不构成任何投资建议。")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {str(e)}"


@register_tool('finance_data', 'Get stock/index/fund market data (K-line, quotes) for financial analysis', ['symbol', 'period'])
def tool_finance_data(symbol: str, period: str = 'daily') -> str:
    """获取股票/指数/基金行情数据（复用网站「金融数据」模块）"""
    try:
        from finance_data import get_kline
        df = get_kline(symbol=symbol, period=period)
        if df is None or getattr(df, 'empty', True):
            return f"Error: 未获取到 {symbol} 的行情数据（可能代码有误或网络问题）"
        return f"{symbol} 最近行情（共{len(df)}条）：\n" + df.tail(15).to_string()
    except Exception as e:
        return f"Error: {str(e)}"


@register_tool('stock_valuation',
                'Estimate whether a stock price is reasonable: DCF intrinsic value vs current price (absolute) plus comparable-company PE/PB vs industry median (relative). Supports A-share 6-digit (600519) or HK+4 code (HK0700). Optional peers: comma-separated comparable codes, e.g. peers="000858,000568,600809"',
                ['symbol', 'peers'])
def tool_stock_valuation(symbol: str, peers: str = '') -> str:
    """估值分析：DCF 绝对估值 + 可比公司相对估值，判断股价是否合理"""
    try:
        from valuation import dcf_valuation, peer_comparison
        out = []
        r = dcf_valuation(symbol, {})
        if r.get('ok'):
            out.append(
                f"【DCF 现金流折现】{symbol} {r.get('name','')}: 现价={r.get('price')}, 每股内在价值={r.get('per_share')}, "
                f"偏差={r.get('spread_pct')}%, 结论={r.get('verdict_cn')}（盈利基数{r.get('base_fcf_yi')}亿, "
                f"高增长率{r.get('g1')}%, 折现率{r.get('r')}%, 永续增长{r.get('g2')}%, 净负债{r.get('net_debt_yi')}亿, 股权价值{r.get('equity_yi')}亿）")
        else:
            out.append(f"DCF 估值失败: {r.get('error')}")
        peer_list = [p.strip() for p in peers.replace('，', ',').split(',') if p.strip()] if peers else []
        if peer_list:
            c = peer_comparison(symbol, peer_list)
            if c.get('ok'):
                v = c.get('verdict') or {}
                t = c.get('target') or {}
                med = f"行业中位数 PE={c.get('med_pe')}, PB={c.get('med_pb')}"
                out.append(f"【可比公司】{symbol} PE={t.get('pe')}, PB={t.get('pb')} vs {med} → {v.get('label','')}")
                detail = "; ".join(
                    f"{p.get('code')}: PE={p.get('pe')}, PB={p.get('pb')}, ROE={p.get('roe')}%, 毛利率={p.get('gross_margin')}%"
                    for p in (c.get('peers') or [])[:4])
                out.append(f"可比明细: {detail}")
            else:
                out.append(f"可比公司分析失败: {c.get('error')}")
        else:
            out.append("提示: 传入 peers 参数（逗号分隔可比公司代码）可进行相对估值对比")
        out.append("免责声明: 本结论为客观数据分析，仅供参考，不构成任何投资建议。")
        return "\n".join(out)
    except Exception as e:
        return f"Error: {str(e)}"


@register_tool('financial_statements',
                'Get the latest financial statements (income statement / balance sheet / cash flow statement) of a stock. Supports A-share 6-digit (600519) or HK+4 code (HK0700). Amounts auto-converted to 亿/万.',
                ['symbol'])
def tool_financial_statements(symbol: str) -> str:
    """获取三大财务报表（最新报告期摘要）"""
    try:
        from financial_statement import fetch_financial_data
        r = fetch_financial_data(symbol)
        if not r.get('ok'):
            return f"Error: {r.get('error')}"
        data = r.get('data', {})
        out = [f"{symbol} 三大财务报表（最新报告期，金额已换算单位）:"]
        for key, name in [('income', '利润表'), ('balance', '资产负债表'), ('cashflow', '现金流量表')]:
            rows = data.get(key, [])
            if rows:
                items = list(rows[0].items())[:12]
                out.append(f"【{name}】" + "、".join(f"{k}={v}" for k, v in items))
            else:
                out.append(f"【{name}】无数据")
        return "\n".join(out)
    except Exception as e:
        return f"Error: {str(e)}"


@register_tool('financial_ratios',
                'Calculate financial ratios (profitability / solvency / growth / dupont) of a stock. Supports A-share 6-digit (600519) or HK+4 code (HK0700). Returns ratio values for analysis.',
                ['symbol'])
def tool_financial_ratios(symbol: str) -> str:
    """计算财务比率（盈利能力/偿债能力/成长能力/杜邦）"""
    try:
        from financial_statement import fetch_financial_data, calculate_ratios
        fd = fetch_financial_data(symbol)
        if not fd.get('ok'):
            return f"Error: {fd.get('error')}"
        r = calculate_ratios(fd)
        if not r.get('ok'):
            return f"Error: {r.get('error')}"
        ratios = r.get('ratios', {})
        out = [f"{symbol} 财务比率:"]
        for cat, name in [('profitability', '盈利能力'), ('solvency', '偿债能力'),
                          ('growth', '成长能力'), ('operational', '营运能力')]:
            items = ratios.get(cat, {})
            if items:
                out.append(f"{name}: " + ", ".join(f"{k}={v}" for k, v in items.items()))
        du = ratios.get('dupont', {})
        if du:
            out.append(f"杜邦分解: ROE={du.get('roe')}% = 净利率{du.get('net_margin')}% × 周转率{du.get('asset_turnover')} × 权益乘数{du.get('equity_multiplier')}")
        return "\n".join(out)
    except Exception as e:
        return f"Error: {str(e)}"


@register_tool('db_query', 'Query external database with readonly SQL (MySQL/PostgreSQL/ClickHouse/SQLite), auto LIMIT and DDL/DML blocked. Credentials (host/port/user/password) are injected by the server defaults; only provide sql and optional db_type/database.', ['sql', 'db_type', 'database'])
def tool_db_query(sql: str, db_type: str = 'sqlite', database: str = '') -> str:
    """只读查询外部数据库（复用网站「数据库直连」模块的安全护栏）

    P2-8：凭据参数（host/port/user/password）已从签名移除——连接信息只允许
    来自服务端配置，禁止由模型注入（此前虽被 _normalize_tool_params 丢弃，
    但签名存在即误用入口）。"""
    try:
        from db_connector import execute_readonly_sql, safe_sqlite_path
        # P0 修复：SQLite 路径必须过白名单（此前仅 routes 层校验，Agent 工具路径
        # 可传 database=data/app.db 读到站点主库的用户表）。app.db 本身在黑名单。
        if (db_type or 'sqlite').lower() == 'sqlite':
            rp = safe_sqlite_path(database or '')
            if rp is None:
                return "Error: 数据库路径未授权（仅允许 output/uploads/data 白名单目录内的库，且禁止站点主库 app.db）"
        cfg = {'db_type': db_type, 'host': '127.0.0.1', 'port': None,
               'database': database, 'username': '', 'password': ''}
        result = execute_readonly_sql(cfg, sql)
        cols = result['columns']
        rows = result['rows']
        head = "\n".join(str(r) for r in rows[:20])
        return f"columns={cols}\nrows({len(rows)}条{'，已截断' if result.get('truncated') else ''}):\n{head}"
    except Exception as e:
        return f"Error: {str(e)}"


# ==================== 通用数值工具（safe_calc / percent_change） ====================
# 供 Agent 在需要精确数字（涨跌幅 / 比率 / 金融基准运算）时调用，避免模型心算出错。
# 遵循业界 Agent 工具规范：snake_case 命名 + 清晰描述 + 类型化参数 schema
# （见 _TOOL_PARAM_SCHEMA），兼容原生 function calling 与正则解析双路径。

# 安全求值函数白名单与常量（拒绝任意代码/属性访问）
_SAFE_COMMON_CONSTANTS = {'pi': 3.141592653589793, 'e': 2.718281828459045}


def _safe_eval(expr: str) -> float:
    """基于 AST 节点白名单的安全表达式求值（禁止 getattr/属性/任意函数）。"""
    import ast as _ast
    import math as _math
    import operator as _op

    _BIN = {
        _ast.Add: _op.add, _ast.Sub: _op.sub, _ast.Mult: _op.mul,
        _ast.Pow: _op.pow, _ast.Mod: _op.mod, _ast.FloorDiv: _op.floordiv,
    }

    def _node(node):
        if isinstance(node, _ast.Expression):
            return _node(node.body)
        if isinstance(node, _ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ValueError('仅支持数值常量')
            return node.value
        if isinstance(node, _ast.BinOp):
            op = type(node.op)
            if op is _ast.Div:
                r = _node(node.right)
                if r == 0:
                    raise ZeroDivisionError('除数不能为 0')
                return _node(node.left) / r
            if op not in _BIN:
                raise ValueError('不支持的运算符')
            return _BIN[op](_node(node.left), _node(node.right))
        if isinstance(node, _ast.UnaryOp):
            v = _node(node.operand)
            if isinstance(node.op, _ast.USub):
                return -v
            if isinstance(node.op, _ast.UAdd):
                return v
            raise ValueError('不支持的运算符')
        if isinstance(node, _ast.Name):
            if node.id in _SAFE_COMMON_CONSTANTS:
                return _SAFE_COMMON_CONSTANTS[node.id]
            raise ValueError(f'不支持的标识符: {node.id}')
        if isinstance(node, _ast.Call):
            if not isinstance(node.func, _ast.Name) or not hasattr(_math, node.func.id) \
                    or node.func.id.startswith('_'):
                raise ValueError('不支持的函数调用')
            args = [_node(a) for a in node.args]
            if node.keywords:
                raise ValueError('不支持关键字参数')
            return getattr(_math, node.func.id)(*args)
        raise ValueError('表达式仅支持数字、四则运算、括号及白名单函数')

    tree = _ast.parse(expr, mode='eval')
    return _node(tree)


@register_tool('safe_calc',
               'Evaluate a safe arithmetic expression precisely (e.g. "12.5*1.08-3", "sqrt(144)", '
               '"10000*(1+5/100)"). Use for financial ratios, percentage/benchmark math and any numeric '
               'calculation to avoid arithmetic errors. Supports + - * / % // ** and parentheses plus '
               'math functions (sqrt/abs/round/min/max/log/log10/exp/floor/ceil) and constants pi/e.',
               ['expression'])
def tool_safe_calc(expression: str) -> str:
    """本地安全表达式求值（AST 白名单，无 eval、无网络）。"""
    try:
        expr = (expression or '').strip()
        if not expr:
            return 'Error: expression 不能为空。例："10000*1.08" 或 "sqrt(144)"。'
        expr = expr.replace('×', '*').replace('÷', '/').replace('％', '%')
        value = float(_safe_eval(expr))
        result = round(value, 10)
        return json.dumps({'expression': expression, 'result': result}, ensure_ascii=False)
    except ZeroDivisionError as e:
        return f'Error: 无法计算（{e}）。'
    except Exception as e:
        return (f'Error: 表达式无效或不受支持（{e}）。仅支持数字、+ - * / % // **、括号，'
                f'函数 abs/round/sqrt/min/max/log/log10/exp/floor/ceil，常量 pi/e。')


@register_tool('percent_change',
               'Compute percentage change (涨跌幅) and absolute change between two values, OR the value '
               'after applying a percentage to a base (目标价/涨幅/贴现). Use instead of doing percentage '
               'math mentally. mode=rate: from_value→to_value; mode=apply: base*(1+pct/100).',
               ['mode', 'from_value', 'to_value', 'base', 'pct'])
def tool_percent_change(mode: str = 'rate', from_value=None, to_value=None,
                        base=None, pct=None) -> str:
    """涨跌幅 / 按幅度求目标值（本地精确计算）。"""
    try:
        _mode = (mode or 'rate').strip().lower()
        if _mode == 'rate':
            fv, tv = float(from_value), float(to_value)
            if fv == 0:
                return 'Error: from_value 不能为 0（无法作为涨跌幅基准）。'
            chg = tv - fv
            pct_val = chg / fv * 100.0
            return json.dumps({
                'mode': 'rate', 'from_value': fv, 'to_value': tv,
                'absolute_change': round(chg, 6),
                'percent_change': round(pct_val, 6),
                'display': f'{tv:g} 相对 {fv:g}：变化 {chg:g}（{pct_val:g}%）',
            }, ensure_ascii=False)
        if _mode == 'apply':
            b, p = float(base), float(pct)
            target = b * (1 + p / 100.0)
            return json.dumps({
                'mode': 'apply', 'base': b, 'pct': p,
                'target': round(target, 6), 'delta': round(target - b, 6),
                'display': f'基准 {b:g} 按 {p:g}% 增减 → {target:g}（幅度 {target - b:g}）',
            }, ensure_ascii=False)
        return 'Error: mode 仅支持 rate（涨跌幅）或 apply（按幅度求目标值）。'
    except (TypeError, ValueError):
        return 'Error: 参数类型错误。rate 模式需 from_value+to_value；apply 模式需 base+pct。'
    except Exception as e:
        return f'Error: {e}'


@register_tool('list_files', 'List the currently available uploaded data files (name, size, last-modified) that can be analyzed. Call this to discover what dataset(s) are available before choosing a file-based tool.', [])
def tool_list_files() -> str:
    """列出 OUT_DIR 下可用的数据文件（名称+大小+修改时间），帮 agent 了解可用数据集"""
    try:
        from core_config import OUT_DIR, pd
        out = Path(OUT_DIR)
        rows = []
        if out.exists():
            for p in sorted(out.iterdir()):
                if p.is_file() and p.suffix.lower() in ('.csv', '.xlsx', '.xls', '.parquet', '.json', '.txt'):
                    size_mb = p.stat().st_size / (1024 * 1024)
                    mtime = time.strftime('%Y-%m-%d %H:%M', time.localtime(p.stat().st_mtime))
                    rows.append(f"- `{p.name}`  ({size_mb:.2f} MB, {mtime})")
        if not rows:
            return "（当前没有可用的数据文件）"
        return "可用数据文件（供数据分析工具引用置 file）：\n" + "\n".join(rows)
    except Exception as e:
        return f'Error: {e}'


@register_tool('data_quality', 'Run a data-quality health-check on the current/uploaded file: rows/columns, per-column missing counts and rates, duplicate rows, zero/constant share for numeric columns, and dtype overview. Call before heavy analysis to spot dirty data.', ['file'])
def tool_data_quality(file: str = '') -> str:
    """数据质量体检：缺失/重复/常量/零值/类型概览。失败返回错误，绝不静默。"""
    try:
        from core_config import load_dataframe, OUT_DIR, pd, np
        fname = file or getattr(tool_file_rag, '_active_file', None)
        if not fname:
            return 'Error: 未提供数据文件（file）。可用文件见 list_files。'
        path = Path(OUT_DIR) / fname
        if not path.exists():
            return f'Error: 文件不存在：{path}'
        df = load_dataframe(str(path))
        if df is None or df.empty:
            return 'Error: 文件加载为空或解析失败。'
        report = {"file": fname, "shape": [int(df.shape[0]), int(df.shape[1])]}
        missing = df.isna().sum()
        missing_rates = (missing / len(df) * 100)
        report['missing'] = {c: {'count': int(missing[c]), 'rate_pct': round(float(missing_rates[c]), 2)}
                             for c in df.columns if int(missing[c]) > 0}
        report['duplicate_rows'] = int(df.duplicated().sum())
        num_cols = df.select_dtypes(include=[np.number]).columns.tolist()
        bad_zero = {}
        const = {}
        for c in num_cols:
            s = df[c]
            nonzero = s[s != 0].dropna()
            bad_zero[c] = round(float((s == 0).astype(int).sum()) / len(df) * 100, 2)
            if len(nonzero) > 0:
                const[c] = round(float((s == nonzero.iloc[0]).sum()) / len(df) * 100, 2) if len(nonzero) else 0.0
        report['dtypes'] = {c: str(t) for c, t in df.dtypes.items()}
        report['numeric_cols'] = num_cols
        report['zero_rate_pct'] = bad_zero
        report['verbatim'] = {c: df[c].nunique() for c in df.columns if df[c].nunique() <= 20}
        return json.dumps(report, ensure_ascii=False)[:9000]
    except Exception as e:
        return f'Error: {e}'



# 分部（主营构成）数据工具：模块内不反向 import 本文件，靠参数注入注册表
from segment_tool import register_segment_tools  # noqa: E402
register_segment_tools(registry)