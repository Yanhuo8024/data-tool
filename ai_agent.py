#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ai_agent.py - ReAct Agent Loop for data analysis
Optimized for DeepSeek V3
Features: progress streaming, tool caching, adaptive iterations
"""

import os
import re
import json
import logging
import time
import hashlib
import threading
import uuid
from pathlib import Path
from typing import Dict, Any, List, Optional, Callable
from functools import lru_cache

logger = logging.getLogger(__name__)
from egress_policy import tool_allowed as _tool_allowed, tool_disabled_message as _egress_off  # noqa: E402
from egress_policy import (tool_side_effect_allowed as _side_effect_allowed,
                           tool_side_effect_message as _side_effect_off)  # noqa: E402

# LLM 文本协议（工具调用解析 / 思考正文提取 / 工具 JSON 净化）已拆到 agent_text_protocol，
# 此处 re-export 保持既有 `from ai_agent import parse_calls / _clean_final_output` 调用点不变。
from agent_text_protocol import (  # noqa: E402
    CALL_PATTERN_BRACKET, CALL_PATTERN_MARKDOWN, _clean_final_output, _extract_json_objects,
    _extract_reasoning, _strip_tool_json, parse_calls,
)


# ==================== Tool Result Cache ====================

class ToolCache:
    """LRU cache for tool results to avoid redundant calls（带 TTL：实时类短缓存、静态类长缓存）"""

    def __init__(self, maxsize=50, default_ttl=1200):
        self._cache = {}        # key -> (result, ts)
        self._access_order = []
        self._maxsize = maxsize
        self._default_ttl = default_ttl
        # key -> 该条目的原始 params（供 invalidate_file 定向失效；随 LRU 一并回收）
        self._key_params: Dict[str, dict] = {}
        # 全局共享实例会被并发的多个 Agent/会话访问，用锁保证 LRU 操作原子防止竞态
        self._lock = threading.RLock()

    def _make_key(self, tool_name: str, params: tuple, fingerprint: str = '') -> str:
        """Generate cache key from tool name + sorted params（+ 可选文件指纹）。

        P1-1 治本：带 `file` 参数的工具，键额外混入文件指纹（mtime_ns + size），
        使"同名文件被覆盖"后旧缓存立即失效，避免用上一版列结构分析新数据。
        """
        raw = f"{tool_name}:{json.dumps(params, sort_keys=True, default=str)}"
        if fingerprint:
            raw += f"|fp={fingerprint}"
        return hashlib.md5(raw.encode()).hexdigest()

    def get(self, tool_name: str, params: dict, ttl: Optional[int] = None,
            fingerprint: str = '') -> Optional[str]:
        with self._lock:
            key = self._make_key(tool_name, tuple(sorted(params.items())), fingerprint)
            item = self._cache.get(key)
            if item is None:
                return None
            value, ts = item
            _ttl = self._default_ttl if ttl is None else ttl
            if _ttl > 0 and (time.time() - ts) > _ttl:
                # 已过期：移除并视为未命中（动态环境用最新结果）
                try:
                    self._access_order.remove(key)
                except ValueError:
                    pass
                try:
                    del self._cache[key]
                except KeyError:
                    pass
                self._key_params.pop(key, None)
                return None
            # Move to end (most recently used)
            if key in self._access_order:
                try:
                    self._access_order.remove(key)
                except ValueError:
                    pass
            if key not in self._access_order and key in self._cache:
                self._access_order.append(key)
            return value

    def put(self, tool_name: str, params: dict, result: str, fingerprint: str = ''):
        with self._lock:
            key = self._make_key(tool_name, tuple(sorted(params.items())), fingerprint)
            if key in self._cache:
                try:
                    self._access_order.remove(key)
                except ValueError:
                    pass
            elif len(self._cache) >= self._maxsize:
                # Evict least recently used
                if self._access_order:
                    oldest = self._access_order.pop(0)
                    if oldest in self._cache:
                        del self._cache[oldest]
                    self._key_params.pop(oldest, None)
            self._cache[key] = (result, time.time())
            # 记录 key → 原参数，供 invalidate_file 做定向失效（仅 maxsize 条，内存可控）
            try:
                self._key_params[key] = dict(params)
            except Exception:
                pass
            if key not in self._access_order:
                self._access_order.append(key)

    def invalidate_file(self, file_name: str) -> int:
        """定向失效：删除所有 params 中 `file` 命中该文件名的缓存条目。

        P1-1：上传/清洗/建模产出后调用，避免"同名文件覆盖"或"派生新文件"
        场景下命中旧结构缓存。返回删除条目数。
        """
        if not file_name:
            return 0
        base = os.path.basename(str(file_name))
        removed = 0
        with self._lock:
            for key in [k for k, p in self._key_params.items()
                        if isinstance(p, dict) and p.get('file') and
                        os.path.basename(str(p.get('file'))) == base]:
                self._cache.pop(key, None)
                self._key_params.pop(key, None)
                try:
                    self._access_order.remove(key)
                except ValueError:
                    pass
                removed += 1
        return removed

    def clear(self):
        with self._lock:
            self._cache.clear()
            self._access_order.clear()
            self._key_params.clear()


# Global cache instance
tool_cache = ToolCache(maxsize=50)


# 按工具类型分级 TTL：实时/联网类短缓存，避免"最新情/走势"命中陈旧结果；静态口径类更长。
def _tool_ttl(tool_name: str) -> int:
    """返回某工具的缓存有效期（秒）。"""
    t = (tool_name or '').lower()
    if t.startswith('web_search') or any(k in t for k in ('news', 'price', 'quote', 'latest',
                                                          'kline', 'point', 'realt')):
        return 60                      # 实时/联网类：60s
    if t in ('run_skill', 'run_analysis', 'run_analysis_api', 'describe', 'file_rag',
             'list_analysis_api', 'list_skills'):
        return 900                     # 口径/静态类：15min
    return 1200                        # 通用：20min


# ---- P1-1 文件指纹：带 file 参数的工具缓存键混入 (mtime_ns, size)，同覆盖即失效 ----
def _file_fingerprint(file_name) -> str:
    """返回数据文件的指纹 `mtime_ns:size`；无法取到时返回 ''（退化为不混指纹）。

    仅用于缓存键（不参与工具调用），对不存在的文件/非文件名为空安全。
    """
    try:
        if not file_name:
            return ''
        from pathlib import Path as _P
        p = _P(str(file_name))
        if not p.is_absolute():
            try:
                from core_config import OUT_DIR as _OUT
                cand = _P(_OUT) / p
                if cand.exists():
                    p = cand
            except Exception:
                pass
        st = p.stat()
        return f"{int(st.st_mtime_ns)}:{int(st.st_size)}"
    except Exception:
        return ''


# ---- P1-4 工具执行超时：所有工具调用统一包一层墙钟超时，防单工具阻塞耗尽线程 ----
TOOL_TIMEOUT_S = float(os.environ.get('AGENT_TOOL_TIMEOUT', '90'))
# 超时后子线程无法被强杀（Python 限制），以 daemon 池承接：主流程立即拿到超时错误
# 继续推进，避免 waitress 线程被永久占用。
_TOOL_POOL = None
_TOOL_POOL_GUARD = threading.Lock()


def _tool_executor():
    global _TOOL_POOL
    if _TOOL_POOL is None:
        with _TOOL_POOL_GUARD:
            if _TOOL_POOL is None:
                import concurrent.futures as _cf
                _TOOL_POOL = _cf.ThreadPoolExecutor(
                    max_workers=int(os.environ.get('AGENT_TOOL_WORKERS', '4')),
                    thread_name_prefix='tool-exec')
    return _TOOL_POOL


class ToolTimeoutError(Exception):
    """工具执行超时（墙钟预算内未返回）。"""


def _call_tool_with_timeout(func, params: dict, timeout: float = None):
    """在受控线程池中执行工具，超时抛 ToolTimeoutError。

    timeout<=0 表示不启用超时（直接同步调用，保留旧行为）。
    """
    _to = TOOL_TIMEOUT_S if timeout is None else float(timeout)
    if _to <= 0:
        return func(**params)
    import concurrent.futures as _cf
    try:
        fut = _tool_executor().submit(func, **params)
    except Exception:
        # 线程池不可用（如解释器关闭）时退化为同步调用
        return func(**params)
    try:
        return fut.result(timeout=_to)
    except _cf.TimeoutError:
        try:
            fut.cancel()
        except Exception:
            pass
        raise ToolTimeoutError(f'工具执行超过 {int(_to)}s 未返回')


# 全局会话记忆管理器（按 session_id 维护跨轮次状态）
from agent_memory import SessionMemoryManager
_memory_manager = SessionMemoryManager()

# 长期记忆（按 user_id 跨会话持久化：偏好、角色、关注列表、近期结论）
from agent_long_memory import get_long_term, LongTermMemory  # noqa: E402

# LangGraph 节点（模块级函数）通过模块属性调用 LLM，便于测试 monkeypatch
import llm_client  # noqa: E402

# 结构化追踪（P0）：轨迹 JSONL 落盘 + OTel 槽位
from trace_store import begin_run as _begin_run, event as _trace_event, end_run as _end_run
from trace_store import trace_file as _trace_file


# ==================== Progress Tracker ====================

class ProgressTracker:
    """Track and report agent execution progress"""
    
    def __init__(self):
        self.steps = []
        self.current_step = 0
        self.total_steps = 0
        self.status = "idle"  # idle, running, done, error
        self.result = None
    
    def start(self, total_steps: int):
        self.total_steps = total_steps
        self.current_step = 0
        self.status = "running"
        self.steps = []
    
    def add_step(self, tool_name: str, params: dict, result_preview: str = ""):
        self.current_step += 1
        step = {
            'step': self.current_step,
            'tool': tool_name,
            'params': {k: v for k, v in params.items()},
            'result_preview': result_preview[:100],
            'timestamp': time.time()
        }
        self.steps.append(step)
        return step
    
    def finish(self, result: str, ok: bool = True):
        self.status = "done" if ok else "error"
        self.result = result
    
    def to_dict(self) -> Dict:
        return {
            'status': self.status,
            'current_step': self.current_step,
            'total_steps': self.total_steps,
            'progress_pct': round((self.current_step / max(self.total_steps, 1)) * 100),
            'steps': self.steps,
            'result': self.result[:500] if self.result else None
        }


# Active progress trackers (keyed by session id)
_active_trackers: Dict[str, ProgressTracker] = {}

# Cancel flags (keyed by session id)
_cancel_flags: Dict[str, bool] = {}


def get_tracker(session_id: str) -> ProgressTracker:
    if session_id not in _active_trackers:
        _active_trackers[session_id] = ProgressTracker()
    return _active_trackers[session_id]


def remove_tracker(session_id: str):
    _active_trackers.pop(session_id, None)
    _cancel_flags.pop(session_id, None)


def cancel_session(session_id: str):
    """Cancel a running agent session"""
    _cancel_flags[session_id] = True


def clear_cancel(session_id: str):
    """清除取消标记（恢复分析时调用，否则恢复后立即再次被取消）"""
    _cancel_flags.pop(session_id, None)


def is_cancelled(session_id: str) -> bool:
    return _cancel_flags.get(session_id, False)


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
            return {'func': lambda **_kw: _side_effect_off(name), 'description': '', 'params': []}
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
            except (Exception, SystemExit):
                pass
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


def _format_fact_verification_note(verified: List[Dict], unverified: List[Dict],
                                   stats: Dict[str, Any]) -> str:
    """生成报告末尾的【核验数据】区块：数据必须真实，未核验不得当真实依据。"""
    total = int(stats.get('total') or 0)
    if total <= 0:
        return ''
    sources = [s for s in (stats.get('sources') or []) if s]
    src_label = '、'.join(dict.fromkeys(sources)) if sources else '本轮工具返回'
    lines = [
        '',
        '',
        '## 核验数据（数据必须真实）',
        f'系统已将报告中的数字与本轮**成功工具的真实返回**逐条比对（来源：{src_label}）。',
        f'共识别 **{total}** 处数字：**已核验 {stats.get("verified", 0)}** · '
        f'**未核验 {stats.get("unverified", 0)}**（未核验率 {stats.get("ratio", 0):.0%}）。',
    ]
    if verified:
        lines.append('')
        lines.append('**已核验（可作为结论依据）**')
        for item in verified[:12]:
            tool = item.get('source') or '工具返回'
            lines.append(f'- `{item.get("value")}` ← {tool}')
        if len(verified) > 12:
            lines.append(f'- …另有 {len(verified) - 12} 处已核验数字')
    if unverified:
        lines.append('')
        lines.append('**未核验（不得视为真实数据）**')
        for item in unverified[:12]:
            lines.append(f'- `{item.get("value")}` — {item.get("reason") or "未在本轮工具真实返回中找到"}')
        if len(unverified) > 12:
            lines.append(f'- …另有 {len(unverified) - 12} 处未核验数字')
        lines.append('')
        lines.append('> ⚠️ **真实性约束**：未核验数字**不得**作为投资/研究结论依据；'
                     '请改为标注「待补充」，或重新调用工具取数后再引用。系统只认可工具返回中的真实数值。')
    else:
        lines.append('')
        lines.append('> ✓ 报告数字均已对上本轮工具真实返回，可作为分析依据。')
    return '\n'.join(lines)


def _audit_fact_sources(final_text, calls) -> tuple:
    """事实核查：报告数字必须能溯源到**成功工具的真实返回**。

    返回 (报告末尾核验说明文本, 统计 dict)。

    stats 关键字段：
      - ratio = 未核验率（missing/total）——下游 agent_flow 按此语义报警
      - verified_data / unverified_data：核验清单（value + source + real）
      - policy='data_must_be_real'：强制「数据必须真实」
      - sources：本轮作为证据的工具名列表

    匹配策略（降低“其实有源却被判未核验”的误报）：
      原文 / 去千分位 / 去单位 / 四舍五入变体；证据源仅计成功工具结果。
    """
    evidence = _real_tool_evidence(calls)
    parts = []
    for e in evidence:
        parts.append(e.get('tool') or '')
        parts.append(str(e.get('params') or ''))
        parts.append(e.get('text') or '')
    blob = ' '.join(parts)
    blob_nc = blob.replace(',', '')
    blob_nums = _source_blob_nums(blob_nc)

    # 逐源匹配，记录数字来自哪个工具（核验数据需要出处）
    def _find_source(tok: str) -> Optional[str]:
        for e in evidence:
            t = e.get('text') or ''
            t_nc = t.replace(',', '')
            if _match_in_text(tok, t, t_nc, _source_blob_nums(t_nc)):
                return e.get('tool') or '工具返回'
        if _match_in_text(tok, blob, blob_nc, blob_nums):
            return '工具返回'
        return None

    used = _extract_numeric_tokens(final_text)
    verified_data: List[Dict[str, Any]] = []
    unverified_data: List[Dict[str, Any]] = []
    missing: List[str] = []
    for tok in sorted(used):
        src = _find_source(tok)
        if src:
            verified_data.append({'value': tok, 'source': src, 'real': True})
        else:
            missing.append(tok)
            unverified_data.append({
                'value': tok,
                'real': False,
                'reason': '未在本轮成功工具的真实返回中找到',
            })

    total = len(used)
    stats = {
        'total': total,
        'verified': len(verified_data),
        'unverified': len(missing),
        'ratio': round(len(missing) / total, 2) if total else 0.0,
        'verified_data': verified_data[:40],
        'unverified_data': unverified_data[:20],
        'sources': [e.get('tool') for e in evidence if e.get('tool')],
        'policy': 'data_must_be_real',
        'real_data_only': True,
        'evidence_tools': len(evidence),
    }
    if total == 0:
        return '', stats
    note = _format_fact_verification_note(verified_data, unverified_data, stats)
    return note, stats



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
    except Exception:
        pass
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
        ('财务报表/估值', ('/api/fs/', '/api/company-analysis')),
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


@register_tool('clean_data', 'Clean data (handle missing values, outliers)', ['file'])
def _load_guarded(file: str):
    """带写入/读取边界校验的 load_dataframe（P1 工具执行边界）。

    统一入口的原因：`clean_data` / `feature_eng` / `ml_train` 三个工具原先是
    `load_dataframe(str(OUT_DIR / file))` —— 若模型给出含 `..` 或绝对路径的 `file`，
    `Path.__truediv__` 会直接越出 OUT_DIR（`Path('/a') / '/etc/x'` == `/etc/x`）。
    这三个工具都会**产出并落盘**，越界读写后果同源，故共用本守卫。

    返回 `(df, None)` 或 `(None, 错误说明)`。
    """
    from core_config import load_dataframe
    return guarded_load(file, load_dataframe)


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
        from db_connector import execute_readonly_sql
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


# ==================== Agent Loop ====================

# ---- Checkpoint（参考 LangGraph checkpointing：中断后可恢复）----
_CHECKPOINT_DIR = None


def _checkpoint_dir():
    global _CHECKPOINT_DIR
    if _CHECKPOINT_DIR is None:
        try:
            from core_config import OUT_DIR
            _CHECKPOINT_DIR = Path(OUT_DIR) / 'agent_checkpoints'
        except Exception:
            _CHECKPOINT_DIR = Path('output') / 'agent_checkpoints'
        _CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    return _CHECKPOINT_DIR


def _save_checkpoint(session_id: str, state: dict):
    """每轮迭代后保存轻量状态（消息/调用记录/计划），供取消或中断后恢复"""
    if not session_id:
        return
    try:
        cp_file = _checkpoint_dir() / f"{session_id}.json"
        cp_file.write_text(json.dumps({
            'session_id': session_id,
            'saved_at': time.time(),
            'messages': state.get('messages', []),
            'calls_made': state.get('calls_made', []),
            'plan': state.get('plan'),
            'failed_tools': state.get('failed_tools', {}),
        }, ensure_ascii=False), encoding='utf-8')
    except (Exception, SystemExit) as e:
        # 显式捕获 SystemExit：运行环境（沙箱/OS）的删除或写入守卫会以
        # SystemExit 形式中断文件操作。checkpoint 只是派生缓存，
        # 任何失败都不得中断 agent 主流程（KeyboardInterrupt 仍正常传播）。
        logger.debug("checkpoint 保存失败: %s", e)


def _load_checkpoint(session_id: str):
    if not session_id:
        return None
    try:
        cp_file = _checkpoint_dir() / f"{session_id}.json"
        if cp_file.exists():
            data = json.loads(cp_file.read_text(encoding='utf-8'))
            if data.get('session_id') == session_id:
                return data
    except (Exception, SystemExit) as e:
        logger.debug("checkpoint 读取失败: %s", e)
    return None


def _remove_checkpoint(session_id: str):
    if not session_id:
        return
    try:
        cp_file = _checkpoint_dir() / f"{session_id}.json"
        if cp_file.exists():
            cp_file.unlink()
    except (Exception, SystemExit) as e:
        # 关键：清理失败绝不能吞掉已完成的运行结果。
        # 实测坑位：环境删除守卫（SystemExit）在此处抛出，
        # 因 SystemExit 不是 Exception 而穿透旧 except，导致终稿被丢弃。
        logger.debug("checkpoint 清理失败（已忽略）: %s", e)


def _trace_summary(calls_made: list, usage: dict = None, active_skill: str = None) -> dict:
    """LangSmith 式调用轨迹摘要：工具数/错误数/缓存命中/总耗时/Token 消耗"""
    total = len(calls_made)
    errors = sum(1 for c in calls_made if c.get('status') == 'error')
    cached = sum(1 for c in calls_made if c.get('cached'))
    duration_ms = sum(int(c.get('duration_ms') or 0) for c in calls_made)
    usage = usage or {}
    return {
        'total_tools': total,
        'errors': errors,
        'cache_hits': cached,
        'tool_duration_ms': duration_ms,
        'active_skill': active_skill,
        # Token 消耗（成本可观测；免费额度敏感，供前端展示）
        'prompt_tokens': int(usage.get('prompt_tokens') or 0),
        'completion_tokens': int(usage.get('completion_tokens') or 0),
        'total_tokens': int(usage.get('total_tokens') or 0),
    }


def _parse_plan(content: str, max_steps: int = 6):
    """从 planner 输出解析 {"steps": [...]}，失败返回 None"""
    if not content:
        return None
    try:
        m = re.search(r'\{[\s\S]*\}', content)
        if m:
            obj = json.loads(m.group(0))
            steps = obj.get('steps')
            if isinstance(steps, list) and steps:
                return [str(s).strip() for s in steps if str(s).strip()][:max_steps]
    except (json.JSONDecodeError, AttributeError):
        pass
    return None


# ============================================================================
# LangGraph 编排核心循环（StateGraph）
#   图结构：START → agent(LLM思考/解析调用)
#                 ├─ 有工具调用 → tools(执行工具) → 轮次满 → force_final，否则 → agent
#                 ├─ 无调用且需「首轮重试 / 分步守卫」→ agent(自环)
#                 ├─ 无调用且收敛 → finalize(生成终稿)
#                 └─ 取消 / 连续失败降级 / 单次重试失败 → END
#             finalize / force_final → END
#   节点仅依赖 state 字段，复用现有 llm_client / ToolRegistry / 追踪 / checkpoint，
#   agent_loop 对外返回契约不变。
# ============================================================================
_LG_STATE_G = None          # 模块级缓存编译后的图（降低重复 build 开销）
_LG_CHECKPOINTER = None     # LangGraph 原生 checkpointer（MemorySaver，按 thread_id 存状态）

# 运行期上下文注册表（**刻意不入图状态**）。
# MemorySaver 会对每轮图状态做 msgpack 序列化，而 progress_callback 是函数、
# memory_state 是自定义对象，均不可序列化（会抛 "Type is not msgpack serializable"）。
# 故以一次调用为粒度生成 runtime_token 存进 cfg（纯字符串，可序列化），
# 真实对象挂在本表；节点运行时按 token 取回，invoke 结束后立即回收。
_LG_RUNTIME: Dict[str, Dict[str, Any]] = {}


def _lg_ctx(cfg: Any) -> Dict[str, Any]:
    """取回本次运行的运行期上下文（回调 / 记忆对象），不参与 checkpoint 序列化。

    2026-09-22：cfg 可能因 MemorySaver 恢复/脏状态为 None，原先直接 `cfg.get`
    会抛 `AttributeError: 'NoneType' object has no attribute 'get'`（graph invoke 0 轮失败）。
    """
    if not isinstance(cfg, dict):
        return {'progress_callback': None, 'memory_state': None}
    token = cfg.get('runtime_token')
    if token:
        ctx = _LG_RUNTIME.get(token)
        if ctx is not None:
            return ctx
    # 兜底：单元测试直接构造 state（无 registry）时读状态内字段
    return {
        'progress_callback': cfg.get('progress_callback'),
        'memory_state': cfg.get('memory_state'),
    }


def _lg_safe_cfg(state: Any) -> Dict[str, Any]:
    """从图状态取 cfg，并补齐节点必需默认值（防 None / 缺键导致 AttributeError）。"""
    raw = state.get('cfg') if isinstance(state, dict) else None
    cfg = dict(raw) if isinstance(raw, dict) else {}
    cfg.setdefault('llm_config', {})
    cfg.setdefault('file_name', None)
    cfg.setdefault('session_id', 'default')
    cfg.setdefault('max_iterations', 5)
    cfg.setdefault('use_native', False)
    cfg.setdefault('openai_tools', None)
    cfg.setdefault('trace_id', None)
    cfg.setdefault('plan', [])
    cfg.setdefault('fail_limit', 3)
    cfg.setdefault('min_success_calls', MIN_SUCCESS_CALLS)
    cfg.setdefault('step_guard_max', 2)
    cfg.setdefault('user_message', '')
    cfg.setdefault('no_plan', False)
    cfg.setdefault('tool_names', None)
    cfg.setdefault('deadline_ts', None)
    cfg.setdefault('fast', False)
    return cfg


def _lg_cb(cfg: Any) -> Optional[Callable]:
    """节点用进度回调（外挂，非图状态字段）。"""
    return _lg_ctx(cfg).get('progress_callback')


def _lg_memory_state(cfg: Dict[str, Any]):
    """节点用会话记忆对象（外挂，非图状态字段）。"""
    return _lg_ctx(cfg).get('memory_state')


# ---- 观察压缩（Observation wrapper）----
# 工具原始输出常为冗长的表格/JSON。观察压缩把它折叠成结构化、低 token 的
# 自然语言观察，既保留关键数字/要点，又压低上下文噪音，减少 token 浪费。
def _compress_observation(tool_name: str, result: str, status: str = 'ok', cached: bool = False) -> str:
    """把单次工具输出压缩成一条 Observation。"""
    original_len = len(result or '')
    _tag = '[缓存]' if cached else ''
    if status != 'ok':
        return f"Tool: {tool_name}{_tag} · Observation (FAILED): {str(result)[:300]}"
    lines = [ln.strip() for ln in str(result).splitlines() if ln.strip()]
    key_lines, n = [], 0
    for ln in lines:
        # 去重连续空行/纯分隔线噪声后保留前若干条要点
        if ln in ('---', '...', '…', '-') or ln.startswith('_-_'):
            continue
        key_lines.append(ln[:200] + '…' if len(ln) > 200 else ln)
        n += 1
        if n >= 14:
            key_lines.append('…（其余条目略）')
            break
    body = '\n'.join(key_lines) or ('(无内容)' if not str(result).strip() else str(result)[:300])
    obs_budget = min(1600, _tool_return_budget(tool_name))
    if len(body) > obs_budget:
        body = body[:obs_budget] + '\n…（观察已截断）'
    header = f"Tool: {tool_name}{_tag} · Observation"
    if len(str(result)) > len(body):
        header += f"（原输出{original_len}字符已压缩）"
    return f"{header}:\n{body}"


def _trim_history(messages: list, keep_recent_rounds: int = 2) -> list:
    """历史轮次裁剪：系统消息 + 最近 keep_recent_rounds 轮完整保留；
    更早的“工具观察(user 消息)”被折叠为一行占位，避免多轮取数后上下文无限膨胀。
    只压缩、不删除消息，保证 assistant/user 交替不被破坏。"""
    if not messages:
        return messages
    sys_head = messages[:2] if messages and messages[0].get('role') == 'system' else messages[:1]
    body = messages[len(sys_head):]
    upto = keep_recent_rounds * 2  # 每轮 = assistant + user 两条
    tail = body[-upto:] if upto > 0 else body
    head = body[:-upto] if upto < len(body) else []
    compact = []
    for m in head:
        content = m.get('content') or ''
        if m.get('role') == 'user' and 'Observation' in content and '[历史' not in content:
            compact.append({**m, 'content': '[历史工具观察已折叠，最新数据见后文]'})
        else:
            compact.append(m)
    return sys_head + compact + tail


# ---- 意图 → 工具分组（降低 system prompt 注意力稀释）----
# 依据用户问题意图，标注本轮最相关的工具组，优先引导模型聚焦；其余工具仍在
# 工具清单中（原生 function calling 仍可用），仅降低其在文本提示中的注意权重。
def _intent_tool_groups(user_message: str, file_name: Optional[str]) -> str:
    """根据意图返回工具分组引导文案（插入 AGENT_SYSTEM_PROMPT 的工具清单之前）。"""
    _live = ('为什么', '原因', '最新', '上涨', '下跌', '趋势', '走势', '行情', '新闻',
             '供需', '涨价', '降价', '展望', '影响', '景气', '反弹', '回落')
    _file = ('描述', '分组', '相关', 'corr', 'describe', '分布', '统计', '报表', '汇总', '同比')
    _ml = ('预测', '模型', '回归', '分类', '聚类', '机器学习', '训练', '评估', '特征', 'score')
    _fin = ('基金', '净值', '回撤', '夏普', '年化', '固收', '债券', '持仓', '评分卡', '风控')
    _t = ('DID', 'did', '双重差分', '政策评估', '因果', '试点的政策效应')

    picks = []
    if any(k in user_message for k in _live):
        picks.append('实时/联网信息（web_search、行情、指标）')
    if any(k in user_message for k in _file):
        picks.append('文件数据分析（describe、分组统计、相关性）')
    if any(k in user_message for k in _ml):
        picks.append('机器学习与建模（回归、分类、特征、评分）')
    if any(k in user_message for k in _fin):
        picks.append('金融深度分析（固收、基金、风控、评分卡）')
    if any(k in user_message for k in _t):
        picks.append('因果推断/政策评估（DID、双重差分）')
    if not file_name and any(k in user_message for k in _live):
        picks.append('联网检索（web_search 多源爬取）')
    if picks:
        return (f"\n\n# 本轮高优先级工具\n"
                "根据用户问题意图，优先使用以下工具组：\n- " + "\n- ".join(picks) +
                "\n其余工具仍可用于补充，但请把注意力集中在上述工具组上。")
    return ''


def _fact_line(tool_name: str, params: dict) -> str:
    """把一次成功工具调用转成一行“已获取资料”摘要（用于 facts_so_far 定向补数）。"""
    key = ''
    for k in ('query', 'file', 'api_path', 'skill_name', 'table', 'columns', 'target', 'symbol'):
        if params.get(k):
            key = str(params.get(k))[:60]
            break
    suffix = f'（{key}）' if key else ''
    short = tool_name.rsplit('.', 1)[-1]
    return f"{short}{suffix}：已获取"


def _dir_addition_context(facts: list, missing: list) -> str:
    """facts_so_far + missing_dimensions 定向补数提示（追加到当轮工具观察之后）。"""
    parts = []
    if facts:
        # 去重并按工具合并
        seen, grouped = set(), {}
        for f in facts:
            tool = f.split('：', 1)[0]
            if tool in seen:
                continue
            seen.add(tool)
            grouped[tool] = f
        parts.append("已获取资料维度：" + "、".join(sorted(grouped.values())))
    if missing:
        parts.append("待补充/补数维度：" + "；".join(missing[-4:]))
    if parts:
        base = "\n\n[定向补数] " + "。".join(parts)
        if missing:
            base += "。——请在下一轮优先补足缺失维度。"
        return base
    return ''


def _lg_state_schema():
    from typing import TypedDict
    class _LGState(TypedDict):
        cfg: Dict[str, Any]                 # 不可变运行配置（llm/file/session/cb/plan/skill…）
        messages: List[Dict[str, Any]]
        calls_made: List[Dict[str, Any]]
        failed_tools: Dict[str, int]
        usage_total: Dict[str, int]
        iteration: int                      # 1-based 当前轮
        pending_calls: List[Dict[str, Any]]
        last_content: str
        consec_fail: int
        step_guards: int
        next: str                           # tools|finalize|force_final|retry|end
        ok: bool
        output: str
        end_status: str
        error: Optional[str]
        degraded: bool
        cancelled: bool
        resumable: bool
        truncated: bool
        facts: List[str]                # 已获取的资料摘要（定向补数参考）
        missing: List[str]              # 缺失/待补充维度
        reflections: int                # 已触发的反思-修订次数
        plan: List[str]                 # Planner 输出的结构化步骤列表（agent 节点参考）
        plan_tools_hint: Dict[str, Any] # 步骤→推荐工具提示（弱约束）
    return _LGState


# ==================== Planner 节点（图示「规划」层，P0 架构补齐）====================
# 把 User Goal + 长期记忆 + 短期上下文 拆解为结构化执行计划，写入 state['plan']，
# 供后续 _lg_agent 参考。简单/单工具任务自动跳过以省一轮 LLM 开销。

_PLANNER_PROMPT = """你是「数据分析任务的 Planner」。把用户目标拆解成 2~{max_steps} 步的执行计划。

【用户目标】
{user_goal}

【长期记忆（跨会话）】
{long_term}

【短期记忆（本次会话上下文）】
{short_term}

【可用工具】
{tools_list}

【输出格式】（仅输出 JSON，不要 Markdown 代码块、不要任何解释文字）
{{
  "steps": [
    {{"id": 1, "action": "步骤描述（中文，动宾结构，如「搜索某行业市场份额」「读取某股票近30日K线」）"}},
    {{"id": 2, "action": "..."}}
  ],
  "expected_output": "最终交付物描述（如「竞品对比表 + 总结报告」）"
}}
"""


def _extract_user_goal(messages: List[Dict[str, Any]]) -> str:
    """从消息列表中提取最近一条 user 消息作为目标"""
    for m in reversed(messages or []):
        if isinstance(m, dict) and m.get('role') == 'user':
            return str(m.get('content', ''))
    return ''


def _should_plan(user_message: str, max_iterations: int) -> bool:
    """是否启动 Planner：短问题/单工具任务跳过以省一轮 LLM"""
    if max_iterations <= 3:
        return False
    if len(user_message or '') < 12:
        return False
    multi_keywords = ("全面", "完整", "详细", "所有", "深度", "报告", "对比",
                      "然后", "接着", "并发", "再", "并", "分析", "评估")
    return any(k in user_message for k in multi_keywords)


def _parse_plan_v2(content: str, max_steps: int = 6) -> Dict[str, Any]:
    """解析 Planner LLM 输出的结构化 plan；失败返回空 plan 但不阻断主流程"""
    empty = {"steps": [], "expected_output": "", "_raw": content or ""}
    if not content:
        return empty
    # 快速通道：纯数组形式 ["步骤1", "步骤2", ...]（_extract_json_objects 只扫 {，需单独处理）
    s = content.strip()
    if s.startswith('['):
        try:
            data = json.loads(s)
            if isinstance(data, list):
                steps = [str(x).strip() for x in data if str(x).strip()]
                if steps:
                    return {
                        "steps": steps[:max_steps],
                        "expected_output": "",
                        "_raw": content,
                    }
        except json.JSONDecodeError:
            pass
    try:
        for obj_str in _extract_json_objects(content):
            try:
                data = json.loads(obj_str)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and isinstance(data.get("steps"), list) and data["steps"]:
                steps: List[str] = []
                for s in data["steps"]:
                    if isinstance(s, dict):
                        a = str(s.get("action", "")).strip()
                        if a:
                            steps.append(a)
                    elif isinstance(s, str):
                        a = s.strip()
                        if a:
                            steps.append(a)
                if steps:
                    return {
                        "steps": steps[:max_steps],
                        "expected_output": str(data.get("expected_output", "")),
                        "_raw": content,
                    }
    except Exception:
        pass
    # 兜底：兼容旧的 "纯列表" 输出
    fallback = _parse_plan(content, max_steps)
    if fallback:
        return {"steps": fallback, "expected_output": "", "_raw": content}
    return empty


def _lg_planner(state: Dict[str, Any]) -> Dict[str, Any]:
    """Planner 节点：把 User Goal 拆解为结构化步骤并注入 messages。

    失败/不适用场景静默返回空 plan，主流程不受影响（保留旧行为）。
    """
    cfg = _lg_safe_cfg(state)
    messages = list(state.get('messages') or []) if isinstance(state, dict) else []
    user_goal = (cfg.get('user_message') or _extract_user_goal(messages) or '').strip()
    max_iter = cfg.get('max_iterations', 5)
    session_id = cfg.get('session_id') or 'default'
    user_id = cfg.get('user_id') or session_id
    trace_id = cfg.get('trace_id')

    # no_plan（多Agent管线阶段）：管线角色已由 STAGE_DEFS.focus 给定，规划节点直接透传，
    # 省掉每阶段一轮 LLM 往返（该 prompt 含全量工具清单，开销不小）。
    if cfg.get('no_plan'):
        return {'plan': [], 'plan_tools_hint': {}}

    if not _should_plan(user_goal, max_iter):
        return {'plan': [], 'plan_tools_hint': {}}

    long_term_text = get_long_term(user_id).to_natural_language(user_goal)
    short_term_text = (cfg.get('memory_context') or '').strip()
    try:
        tools_list = '\n'.join(
            [f"- {t['name']}: {t['description']}" for t in registry.list_tools()]
        )
    except Exception:
        tools_list = ''

    max_steps = min(6, max(2, int(max_iter) - 1))
    prompt = _PLANNER_PROMPT.format(
        max_steps=max_steps,
        user_goal=user_goal[:1000],
        long_term=long_term_text or '（无）',
        short_term=short_term_text or '（无）',
        tools_list=tools_list[:1500] or '（无）',
    )

    if trace_id:
        try:
            _trace_event(trace_id, 'planner.start', {'user_goal': user_goal[:200]})
        except Exception:
            pass

    content = ''
    try:
        llm_config = cfg['llm_config']
        # P0-1 修复：仅传 system 轮会被智谱等 provider 以 `400 code 1214 messages 参数非法` 拒绝
        # （已用真实 provider 在线复现）。必须补一条 user 轮，否则规划阶段必然失败并静默降级为
        # plan=[]，使"复杂任务的 Plan-and-Execute"完全失效。
        response = llm_client.chat(
            [{'role': 'system', 'content': prompt},
             {'role': 'user', 'content': (user_goal or '请为上述任务制定执行计划')[:2000]}],
            llm_config, max_tokens=600,
        )
        content = response.get('content', '') if isinstance(response, dict) else str(response)
        # 防御：usage_total 缺失时不得让 KeyError 被下面的兜底吞掉 —— 那会把"规划成功、
        # 但计数结构异常"静默降级成"无计划"（与 _safe_int_usage 修的静默失效同类）。
        _u = state.get('usage_total')
        if isinstance(_u, dict):
            _lg_emit_usage(_u, response)
    except Exception as e:
        if trace_id:
            try:
                _trace_event(trace_id, 'planner.error', {'error': str(e)[:200]})
            except Exception:
                pass
        return {'plan': [], 'plan_tools_hint': {}}

    parsed = _parse_plan_v2(content, max_steps=max_steps)
    steps = parsed['steps']
    if not steps:
        return {'plan': [], 'plan_tools_hint': {}}

    plan_lines = [f"{i+1}. {s}" for i, s in enumerate(steps)]
    plan_msg = "【本任务执行计划】\n" + "\n".join(plan_lines)
    if parsed.get('expected_output'):
        plan_msg += f"\n最终输出: {parsed['expected_output']}"
    new_messages = list(messages) + [{'role': 'system', 'content': plan_msg}]

    if trace_id:
        try:
            _trace_event(trace_id, 'planner.done', {'steps': steps})
        except Exception:
            pass

    return {'plan': steps, 'plan_tools_hint': {}, 'messages': new_messages}


def _safe_int_usage(usage) -> dict:
    """把 usage 中的数值字段取整，**跳过嵌套对象**。

    修复的历史缺陷（2026-09-14 定位）：
        原实现 `{k: int(v) for k, v in usage.items() if k != 'usage'}` 对 usage 的
        每个值直接 int()，而智谱 GLM-4.x 的响应形如
            {"prompt_tokens": 18, "completion_tokens": 111,
             "total_tokens": 129,
             "prompt_tokens_details": {"cached_tokens": 3},
             "completion_tokens_details": {"reasoning_tokens": 107}}
        其中 *_details 是 dict → int(dict) 抛 TypeError。该异常发生在「LLM 调用已成功
        返回之后」，却被外层的 `except Exception` 捕获并当作 LLM 调用失败处理
        （consec_fail += 1），于是**一次成功的调用被记成失败**，达到 fail_limit 后
        agent 直接降级——表现为「模型明明通了，agent 却一直失败」。

    同时兼容字符串形式的数字，忽略 bool（bool 是 int 子类，误转会污染统计）。
    """
    if not isinstance(usage, dict):
        return {}
    out = {}
    for k, v in usage.items():
        if k == 'usage' or isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            out[k] = int(v)
        elif isinstance(v, str):
            s = v.strip()
            if s.lstrip('+-').isdigit():
                out[k] = int(s)
    return out


def _lg_emit_usage(usage: Any, response) -> dict:
    """累计单次 LLM 调用的 token 用量（原位累计并返回）。

    P2-4：兼容 input/output_tokens 等别名，缺失 total 时按 p+c 兜底。
    2026-09-22：usage 为 None/非 dict 时不得抛 AttributeError（checkpoint 恢复脏状态）。
    """
    if not isinstance(usage, dict):
        usage = {}
    if isinstance(response, dict):
        u = _safe_int_usage(response.get('usage'))
        p = u.get('prompt_tokens', u.get('input_tokens'))
        c = u.get('completion_tokens', u.get('output_tokens'))
        if p is None:
            p = u.get('prompt')
        if c is None:
            c = u.get('completion')
        usage['prompt_tokens'] = usage.get('prompt_tokens', 0) + int(p or 0)
        usage['completion_tokens'] = usage.get('completion_tokens', 0) + int(c or 0)
        _tot = u.get('total_tokens')
        if _tot is None:
            _tot = int(p or 0) + int(c or 0)
        usage['total_tokens'] = usage.get('total_tokens', 0) + int(_tot)
    return usage


def _lg_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    """agent 节点：LLM 思考 + 解析工具调用 + 进度/思考事件转发。据此决定 next。"""
    cfg = _lg_safe_cfg(state)
    llm_config = cfg['llm_config']
    file_name = cfg['file_name']
    session_id = cfg['session_id']
    cb = _lg_cb(cfg)
    max_iter = cfg['max_iterations']
    use_native = cfg['use_native']
    openai_tools = cfg['openai_tools']
    trace_id = cfg['trace_id']
    round_no = int(state.get('iteration') or 0) + 1
    messages = list(state.get('messages') or [])
    calls_made = list(state.get('calls_made') or [])

    _usage = state.get('usage_total')
    feed = dict(
        iteration=round_no, messages=messages,
        usage_total=_usage if isinstance(_usage, dict)
        else {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0})

    # 取消检查
    if is_cancelled(session_id):
        _save_checkpoint(session_id, {
            'messages': messages, 'calls_made': calls_made,
            'plan': cfg['plan'], 'failed_tools': state['failed_tools'],
        })
        tracker = get_tracker(session_id) if session_id else None
        if tracker:
            tracker.finish('', ok=False)
        feed.update(next='end', cancelled=True, resumable=True, ok=False,
                    end_status='cancelled',
                    output='分析已被用户取消（已保存断点，可恢复继续）')
        return feed

    if cb:
        cb({'status': 'running', 'iteration': round_no, 'max_iterations': max_iter,
            'message': f'思考中... (第 {round_no}/{max_iter} 轮)'})

    _t_llm = time.time()   # LLM 往返耗时：agent 里最大的时间消耗项，必须可观测
    try:
        response = llm_client.chat(messages, llm_config, max_tokens=900,
                        tools=openai_tools, tool_choice='auto' if openai_tools else None)
        content = response.get('content', '') if isinstance(response, dict) else str(response)
        _lg_emit_usage(state['usage_total'], response)
        consec = 0
        _reason = _extract_reasoning(content)
        if cb and _reason and len(_reason) >= 5:
            cb({'type': 'thinking', 'text': _reason, 'iteration': round_no,
                'max_iterations': max_iter, 'status': 'running',
                'message': f'推理中... (第 {round_no}/{max_iter} 轮)'})
        if trace_id:
            _trace_event(trace_id, 'llm_call', {
                'iteration': round_no,
                'native': bool(openai_tools),
                'has_tool_calls': bool(response.get('tool_calls')) if isinstance(response, dict) else False,
                'content_len': len(content),
                'duration_ms': int((time.time() - _t_llm) * 1000),
                # 注意：必须走 _safe_int_usage —— 智谱 GLM-4.x 的 usage 含嵌套
                # *_details 对象，直接 int(v) 会抛 TypeError 并让本轮被误判为失败。
                'usage': _safe_int_usage(response.get('usage'))
                if isinstance(response, dict) else None,
            })
    except Exception as e:
        logger.error('LangGraph agent LLM 调用失败: %s', e)
        consec = state['consec_fail'] + 1
        if consec >= cfg['fail_limit']:
            tracker = get_tracker(session_id) if session_id else None
            if tracker:
                tracker.finish('', ok=False)
            feed.update(next='end', ok=False, degraded=True, error=str(e),
                        consec_fail=consec, end_status='degraded',
                        output=(
                            "⚠️ 当前模型连续多次调用失败（可能已耗尽免费配额或触发限流）。\n\n"
                            "建议：请在「AI 助手」设置中切换其他模型，或稍等片刻配额重置后重试。\n"
                            f"最近错误：{str(e)[:200]}"
                        ))
            return feed
        try:
            response = llm_client.chat(messages, llm_config, max_tokens=400,
                            tools=openai_tools, tool_choice='auto' if openai_tools else None)
            content = response.get('content', '') if isinstance(response, dict) else str(response)
            _lg_emit_usage(state['usage_total'], response)
            consec = 0
            _reason = _extract_reasoning(content)
            if cb and _reason and len(_reason) >= 5:
                cb({'type': 'thinking', 'text': _reason, 'iteration': round_no,
                    'max_iterations': max_iter, 'status': 'running',
                    'message': f'推理中... (第 {round_no}/{max_iter} 轮)'})
        except Exception as e2:
            tracker = get_tracker(session_id) if session_id else None
            if tracker:
                tracker.finish('', ok=False)
            feed.update(next='end', ok=False, error=str(e2),
                        consec_fail=consec, end_status='error',
                        output=f'LLM error: {str(e2)}（已重试一次仍失败）')
            return feed

    # 解析工具调用（原生 function calling 优先，正则兜底）
    tcr = response.get('tool_calls') if isinstance(response, dict) else None
    if tcr:
        pending = []
        for tc in tcr:
            func = tc.get('function', {})
            try:
                params = json.loads(func.get('arguments', '{}'))
            except json.JSONDecodeError:
                params = {}
            pending.append({'tool': func.get('name', ''), 'params': params})
    else:
        pending = parse_calls(content)

    feed.update(last_content=content, pending_calls=pending, consec_fail=consec)

    if not pending:
        _ok = sum(1 for c in calls_made if c.get('status') == 'ok')
        # 首轮无工具调用 → 专属"重试一次"提示（原生/正则分别适配）
        if not calls_made and round_no == 1:
            if use_native and trace_id:
                _trace_event(trace_id, 'fallback_retry', {'mode': 'native_no_calls'})
            # P1-6：累计未采纳次数，达标后对该 provider/model 自动降级（后续不再下发 tools）
            if use_native:
                _note_native_miss(llm_config.get('provider', ''), llm_config.get('model', ''), trace_id)
            _msgs = list(messages) + [
                {'role': 'assistant', 'content': content},
                {'role': 'user', 'content': _first_round_retry_prompt(
                    use_native=use_native, has_file=bool(file_name))},
            ]
            feed.update(messages=_msgs, next='retry')
            return feed
        # 反思-修订（Reflect-Revise）：多次工具调用失败后，不让模型直接放弃，
        # 而是先反思执行计划、改用可用工具/更合理关键词再取一次数。每个任务最多触发一次。
        _total_fail = sum(v for v in state['failed_tools'].values() if v >= 2)
        if (calls_made and _total_fail > 0 and round_no < max_iter
                and state.get('reflections', 0) < 1):
            _msgs = list(messages) + [
                {'role': 'assistant', 'content': content},
                {'role': 'user', 'content': (
                    "\n\n[反思-修订] 前几轮的工具调用多次失败。请先停下反思当前执行计划："
                    "哪些工具不可用或参数填错了？能否改用其他工具或更合理的关键词？是否遗漏关键维度？\n"
                    "然后给出修订后的下一步：在仍可用的工具中选最合适的，重新发起一次工具调用"
                    "（不要直接跳过取数下结论）。若确已拥有足够资料，请直接输出最终报告并标注数据来源。")},
            ]
            if cb:
                cb({'status': 'running', 'iteration': round_no, 'max_iterations': max_iter,
                    'message': '反思-修订：多次工具失败，重新规划取数方向...'})
            feed.update(messages=_msgs, next='retry', reflections=state.get('reflections', 0) + 1)
            return feed
        # 分步守卫：资料不足强制回到取数步骤
        if (_ok < cfg['min_success_calls'] and round_no < max_iter
                and state['step_guards'] < cfg['step_guard_max']):
            _msgs = list(messages) + [
                {'role': 'assistant', 'content': content},
                {'role': 'user', 'content': _step_guard_message(_ok, cfg['min_success_calls'], cfg['plan'])},
            ]
            if cb:
                cb({'status': 'running', 'iteration': round_no, 'max_iterations': max_iter,
                    'message': f'分步守卫：资料不足（{_ok}/{cfg["min_success_calls"]}），继续获取...'})
            feed.update(messages=_msgs, next='retry', step_guards=state['step_guards'] + 1)
            return feed
        feed['next'] = 'finalize'
        return feed

    feed['next'] = 'tools'
    return feed


def _prepare_tool_call(call: Dict[str, Any], file_name: str) -> Dict[str, Any]:
    """规整单个待执行的工具调用（解析工具名 / 技能归一 / file 注入 / 参数 schema 清洗）。

    抽出来供 tools 节点与「并行预取」共用 —— 两条路径必须做完全一致的准备，
    否则预取算出的参数与循环内规整的参数会不一致（缓存键、trace 都会漂移）。
    返回 dict：{tool, params, info, dropped}
    """
    tool_name = call.get('tool')
    params = dict(call.get('params') or {})
    tool_info = registry.get_tool(tool_name)
    # 技能简写调用归一为 run_skill
    if tool_info is None and tool_name and re.match(r'^[a-z][a-z0-9_-]*$', tool_name):
        try:
            from skill_manager import get_skills_index
            _skill_names = {s.get('name') for s in (get_skills_index() or [])}
            if tool_name in _skill_names:
                _task = ', '.join(str(v) for v in params.values()) if params else ''
                params = {'skill_name': tool_name, 'task': _task or ''}
                tool_name = 'run_skill'
                tool_info = registry.get_tool('run_skill')
        except Exception:
            pass
    # 仅对声明接受 file 的工具注入/校正 file
    _accepts_file = bool(tool_info and tool_info['params'] and 'file' in tool_info['params'])
    if _accepts_file and 'file' not in params and file_name:
        params['file'] = file_name
    elif _accepts_file and 'file' in params and file_name:
        from core_config import OUT_DIR
        from pathlib import Path
        if not (Path(OUT_DIR) / params['file']).exists():
            params['file'] = file_name

    # 参数 schema 规整：丢弃未知键（防越权注入），并对数字参数做类型转换。
    _cleaned, _dropped = _normalize_tool_params(tool_name, params, tool_info)
    return {'tool': tool_name, 'params': _cleaned, 'info': tool_info, 'dropped': _dropped}


# 并行执行同轮工具调用（2026-09-18 提速）：
#   agent 一轮常同时下发 2~3 个互相独立的取数调用（如多个 web_search / 估值+财务），
#   旧实现逐个串行执行 —— 每个联网工具 2~10s，一轮就被串成 6~30s。
#   改为一次性投递工具池并发执行，结果仍按原顺序回收，保证 messages / calls_made 顺序稳定。
#   有状态或重型的工具保持串行（技能执行/分析 API 内部可能自持全局状态）。
_TOOLS_PARALLEL = os.environ.get('AGENT_TOOLS_PARALLEL', '1') not in ('0', '', 'false', 'False')
_NON_PARALLEL_TOOLS = frozenset({'run_skill', 'run_analysis_api', 'clean_data', 'ml_train'})
_PARALLEL_MAX = max(1, int(os.environ.get('AGENT_TOOLS_PARALLEL_MAX', '3')))


def _submit_tool(tool_info: Dict, params: Dict):
    """把工具调用投递到工具池（复用 _tool_executor，与超时路径同一池）。"""
    func = tool_info['func']
    try:
        return _tool_executor().submit(func, **params)
    except Exception:
        return None


def _lg_tools(state: Dict[str, Any]) -> Dict[str, Any]:
    """tools 节点：执行全部 pending 工具调用，累积结果并入消息，保存 checkpoint。"""
    cfg = _lg_safe_cfg(state)
    llm_config = cfg['llm_config']
    file_name = cfg['file_name']
    session_id = cfg['session_id']
    cb = _lg_cb(cfg)
    plan = cfg['plan']
    max_iter = cfg['max_iterations']
    round_no = int(state.get('iteration') or 0)
    messages = list(state.get('messages') or [])
    calls_made = list(state.get('calls_made') or [])
    failed_tools = dict(state.get('failed_tools') or {})
    last_content = state.get('last_content') or ''

    tool_results = []
    # 这一轮新增的已获取/缺失维度（定向补数：agent 下轮据此决定是否继续取数）
    facts = list(state.get('facts') or [])
    missing = list(state.get('missing') or [])

    # ---- 一次准备（共用 helper），随后并行预取本轮所有「可并行且未命中缓存」的工具 ----
    # 顺序：先并发跑耗时的取数，再按原顺序做记账与消息拼装 —— 顺序与串行版逐字节一致。
    prepared = [_prepare_tool_call(c, file_name) for c in (state.get('pending_calls') or [])]
    prefetch: Dict[int, Any] = {}
    if _TOOLS_PARALLEL and len(prepared) > 1:
        _slots = 0
        for _i, _p in enumerate(prepared):
            if _slots >= _PARALLEL_MAX:
                break
            if _p['info'] is None or _p['tool'] in _NON_PARALLEL_TOOLS:
                continue
            _t = _p['tool']
            _prm = _p['params']
            _force = (round_no >= max_iter) or (failed_tools.get(_t, 0) > 0)
            _ttl = 0 if _force else _tool_ttl(_t)
            _fp0 = _file_fingerprint(_prm.get('file')) if _prm.get('file') else ''
            if tool_cache.get(_t, _prm, ttl=_ttl, fingerprint=_fp0) is not None:
                continue                      # 命中缓存 → 交给主循环走 cached 分支
            _fut = _submit_tool(_p['info'], _prm)
            if _fut is not None:
                prefetch[_i] = _fut
                _slots += 1
        if prefetch:
            # 并行任务同一时刻投递 → 每个 future 的等待时长即"投递至该工具返回"的墙钟，
            # 供 tool_end 展示（并发下各工具耗时互相重叠，这里是各自的墙钟而非净占用）。
            _t_submit = time.time()
            for _i, _fut in list(prefetch.items()):
                try:
                    _v = _fut.result(timeout=TOOL_TIMEOUT_S)
                    prefetch[_i] = ('ok', _v, time.time() - _t_submit)
                except Exception as _e:       # 含 TimeoutError / 工具自身异常
                    prefetch[_i] = ('err', _e, time.time() - _t_submit)

    for _ci, call in enumerate(state['pending_calls']):
        _p = prepared[_ci]
        tool_name = _p['tool']
        params = _p['params']
        tool_info = _p['info']
        if _p['dropped']:
            _trace_event(cfg['trace_id'], 'tool_param_drop', {
                'iteration': round_no, 'tool': tool_name, 'dropped_keys': _p['dropped'],
            })

        if cb:
            cb({'type': 'tool_start', 'tool': tool_name,
                'params': {k: str(v)[:80] for k, v in (params or {}).items()},
                'iteration': round_no, 'status': 'running', 'message': f'调用 {tool_name}...'})

        # 分级 TTL + 强制刷新（计划末轮或已连续失败过的不缓存，强制取最新）
        _force_refresh = (round_no >= max_iter) or (failed_tools.get(tool_name, 0) > 0)
        _ttl = 0 if _force_refresh else _tool_ttl(tool_name)
        # P1-1：带 file 参数的工具，缓存键混入文件指纹（同名覆盖后旧缓存立即失效）
        _fp = _file_fingerprint(params.get('file')) if params.get('file') else ''
        cached = tool_cache.get(tool_name, params, ttl=_ttl, fingerprint=_fp)
        if cached is not None:
            result, status, elapsed, is_cached = cached, 'ok', 0.0, True
            calls_made.append({'tool': tool_name, 'params': params,
                               'result_preview': str(result)[:200], 'cached': True,
                               'result_brief': _compress_observation(
                                   tool_name, result, status='ok', cached=True)[:1500],
                               'status': 'ok', 'duration_ms': 0})
            _trace_event(cfg['trace_id'], 'tool_result', {
                'iteration': round_no, 'tool': tool_name,
                'params': {k: str(v)[:120] for k, v in (params or {}).items()},
                'status': 'ok', 'duration_ms': 0, 'cached': True,
                'preview': str(result)[:200],
            })
            if cb:
                cb({'type': 'tool_end', 'tool': tool_name,
                    'params': {k: str(v)[:80] for k, v in (params or {}).items()},
                    'tool_status': 'ok', 'cached': True, 'elapsed_ms': 0,
                    'preview': str(result)[:160], 'iteration': round_no,
                    'calls_count': len(calls_made), 'status': 'running',
                    'message': f'{tool_name} 命中缓存'})
            tool_results.append(_compress_observation(tool_name, result, status='ok', cached=True))
            facts.append(f"{tool_name}（缓存）：已获取关键数据")
            continue

        status, elapsed = 'ok', 0.0
        if tool_info is None:
            result = f"Error: Unknown tool '{tool_name}'"
            status = 'error'
        else:
            _pf = prefetch.get(_ci)
            if _pf is not None:
                # 并行预取已完成：不等超时，直接取结果/异常（异常在此重放，走统一错误处理）
                _pf_kind, _pf_val, elapsed = _pf
                if _pf_kind != 'ok':
                    status = 'error'
                    _emsg = str(_pf_val).replace('\n', ' ')[:200]
                    if isinstance(_pf_val, ToolTimeoutError):
                        _emsg = f"{_emsg}（请改用其他工具或缩减数据规模后重试）"
                    result = f"Error executing {tool_name}: {_emsg}"
                else:
                    result = _clip_tool_result(tool_name, _pf_val)
                    tool_cache.put(tool_name, params, result, fingerprint=_fp)
            else:
                try:
                    start_time = time.time()
                    # P1-4：统一墙钟超时，防止单个工具（联网/akshare）无限阻塞占用 waitress 线程
                    result = _call_tool_with_timeout(tool_info['func'], params)
                    elapsed = time.time() - start_time
                    result = _clip_tool_result(tool_name, result)
                    tool_cache.put(tool_name, params, result, fingerprint=_fp)
                except Exception as e:
                    # P2-8：异常原文可能含路径/凭据片段，截断后再回灌 LLM 与 trace
                    _emsg = str(e).replace('\n', ' ')[:200]
                    if isinstance(e, ToolTimeoutError):
                        _emsg = f"{_emsg}（请改用其他工具或缩减数据规模后重试）"
                    result = f"Error executing {tool_name}: {_emsg}"
                    status = 'error'

        if status == 'error':
            _fails = failed_tools.get(tool_name, 0) + 1
            failed_tools[tool_name] = _fails
            if _fails <= 2:
                result += "\n（该工具调用失败。请检查参数是否正确后修正重试，或改用其他工具达成目标。）"
            else:
                result += f"\n（{tool_name} 已连续失败 {_fails} 次，请勿再调用该工具，改用其他方式完成分析。）"
            missing.append(f"{tool_name}：获取失败（{str(result)[:80]}），需改用其他工具/方式补数")
        else:
            facts.append(_fact_line(tool_name, params))

        calls_made.append({'tool': tool_name, 'params': params,
                           'result_preview': str(result)[:200], 'cached': False,
                           # 审计用更长摘要：preview 200 常截断数字，导致 fact_audit 误报
                           'result_brief': _compress_observation(
                               tool_name, result, status=status)[:1500],
                           'status': status, 'duration_ms': int(elapsed * 1000)})
        _trace_event(cfg['trace_id'], 'tool_result', {
            'iteration': round_no, 'tool': tool_name,
            'params': {k: str(v)[:120] for k, v in (params or {}).items()},
            'status': status, 'duration_ms': int(elapsed * 1000), 'cached': False,
            'preview': str(result)[:200],
        })
        tool_results.append(_compress_observation(tool_name, result, status=status))
        if cb:
            cb({'type': 'tool_end', 'tool': tool_name,
                'params': {k: str(v)[:80] for k, v in (params or {}).items()},
                'tool_status': status, 'cached': False, 'elapsed_ms': int(elapsed * 1000),
                'preview': str(result)[:160], 'iteration': round_no,
                'max_iterations': max_iter, 'calls_count': len(calls_made),
                'status': 'running',
                'message': f'{tool_name} {"失败" if status == "error" else "完成"}（{elapsed:.1f}s）'})

    _ok = sum(1 for c in calls_made if c.get('status') == 'ok')
    _remaining = max(1, max_iter - round_no)
    _step_note = (f"\n\n[分步进度] 已成功执行 {_ok}/{cfg['min_success_calls']} 次工具调用，"
                  f"剩余轮次 {_remaining}；请按步骤继续推进（先资料获取、后结论），未完成的步骤严禁跳过。"
                  "\n若执行计划的核心步骤已基本覆盖（关键数据已拿到），请立即停止调用工具，"
                  "基于已有真实数据输出最终报告——个别缺失数据在报告中标注「待补充」即可，"
                  "不要为单个细节数据继续消耗轮次。")
    # facts_so_far + missing_dimensions 定向补数：指引 agent 下一轮聚焦未取到的维度
    _facts_ctx = _dir_addition_context(facts, missing)
    messages = messages + [{'role': 'assistant', 'content': last_content},
                           {'role': 'user', 'content': "\n\n".join(tool_results) + _step_note + _facts_ctx}]
    messages = _trim_history(messages)
    _save_checkpoint(session_id, {
        'messages': messages, 'calls_made': calls_made,
        'plan': plan, 'failed_tools': failed_tools,
    })
    return {'messages': messages, 'calls_made': calls_made, 'failed_tools': failed_tools,
            'facts': facts, 'missing': missing}


_STREAM_GUARD_CHARS = 24   # 终稿流式首块守卫的比较窗口（判定是否像工具调用 JSON）


def _looks_like_tool_call(head: str) -> bool:
    """判断已累积内容的开头是否像「工具调用 JSON」而非正文。

    文本工具协议是 `{"tool": ...}` JSON（parse_calls 用 _extract_json_objects 扫描 `{`），
    而 Markdown 正文不会以 `{` / `[` 开头。命中即放弃流式上报（仍继续收流），
    避免把 JSON 噪声渲染给用户。
    """
    s = (head or '').lstrip()
    if not s:
        return False
    if s[0] in '{[':
        return True
    return '"tool"' in s[:_STREAM_GUARD_CHARS]


def _final_answer_call(messages: List[Dict[str, Any]], llm_config: Dict[str, Any],
                       cb: Callable = None, max_tokens: int = 3200):
    """终稿生成：有进度回调时走**流式**并逐段上报 delta，让长回答"边生成边显示"。

    动机：终稿是本轮最长的一次生成（max_tokens 可达 3200），此前用非流式一次性等待，
    用户在"生成最终报告..."之后全程看不到任何输出 —— 总时长不变，但**感知等待极长**。

    安全性（三层）：
      1. 首块过 `_looks_like_tool_call` 守卫：疑似工具调用 JSON 则不上报，只收流；
      2. 流式产出为空 / 抛异常 → **回退非流式**调用，成功率不下降；
      3. 前端 done 阶段比对文案，若与流式内容**分叉**则整体替换（见 core.js）。

    返回 (content, response)；response 为 None 表示走了流式（无 usage 可累计）。
    """
    if cb:
        try:
            chunks = []
            streaming = False
            for piece in llm_client.chat_stream(messages, llm_config, max_tokens=max_tokens):
                if not piece:
                    continue
                chunks.append(piece)
                if not streaming:
                    head = ''.join(chunks)
                    if _looks_like_tool_call(head) or not head.strip():
                        continue          # 疑似工具调用 / 尚无正文：先不上报
                    streaming = True
                    # 判定通过：把已缓存前缀一次性补发，避免丢字
                    try:
                        cb({'type': 'delta', 'text': head})
                    except (Exception, SystemExit):
                        pass
                    continue
                try:
                    cb({'type': 'delta', 'text': piece})
                except (Exception, SystemExit):
                    pass
            text = ''.join(chunks)
            if text.strip():
                return text, None
            logger.warning('终稿流式产出为空，回退非流式调用')
        except Exception as e:
            logger.warning('终稿流式调用失败，回退非流式: %s', e)
    resp = llm_client.chat(messages, llm_config, max_tokens=max_tokens)
    content = (resp.get('content', '') if isinstance(resp, dict) else str(resp))
    # P0（2026-09-18）：max_tokens 触顶续写。此前 finish_reason 不透出，终稿被
    # max_tokens 截断时静默产出半截报告（诊断页可见 content_len 2442/5402 的截断案例）。
    # 非流式路径检测到 finish_reason == 'length' 且有正文时，追加一次续写调用补全。
    if isinstance(resp, dict) and resp.get('finish_reason') == 'length' and str(content).strip():
        try:
            _cont_msgs = (list(messages)
                          + [{'role': 'assistant', 'content': content},
                             {'role': 'user',
                              'content': ('继续：从上文中断处直接衔接输出剩余内容，'
                                          '不要重复已有内容，尽快收尾。')}])
            resp2 = llm_client.chat(_cont_msgs, llm_config, max_tokens=1600)
            _cont = (resp2.get('content') or '').strip() if isinstance(resp2, dict) else ''
            if _cont:
                content = str(content).rstrip() + ('\n' if not str(content).rstrip().endswith(('\n', ' ', '　')) else '') + _cont
                # 合并 usage，避免续写部分的 token 统计丢失
                _u1 = dict(resp.get('usage') or {})
                _u2 = dict(resp2.get('usage') or {})
                for _k in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
                    if _k in _u1 or _k in _u2:
                        _u1[_k] = int(_u1.get(_k) or 0) + int(_u2.get(_k) or 0)
                resp = dict(resp, content=content, usage=_u1,
                            finish_reason=resp2.get('finish_reason') or 'stop',
                            continued=True)
        except Exception as _cexc:
            logger.warning('终稿续写失败（保留已生成部分）: %s', _cexc)
    return content, resp


# 终稿 token 预算（2026-09-20 拆分出快速档）：
#   深度模式 4800 —— 终稿是本轮最长生成，且 reasoning 系的思考与正文共享该预算，
#     3200 下长报告频繁触顶截断；
#   ⚡快速档 2000 —— 只要核心结论。实测终稿占 LLM 累计耗时一半以上，是"快速模式
#     仍显得慢"的最大单点，故快速档同时收紧预算并要求简洁输出。
_FINAL_MAX_TOKENS = 4800
_FAST_FINAL_MAX_TOKENS = 2000


def _final_token_budget(cfg: Dict[str, Any]) -> int:
    """按是否快速档返回终稿 token 预算（独立函数便于单测，避免直接驱动 LangGraph）。"""
    return _FAST_FINAL_MAX_TOKENS if bool((cfg or {}).get('fast')) else _FINAL_MAX_TOKENS


def _lg_finish(state: Dict[str, Any], truncated: bool = False) -> Dict[str, Any]:
    """finalize / force_final 共用收尾：追加收敛提示 → LLM 生成终稿 → 记忆/checkpoint 收尾。"""
    cfg = _lg_safe_cfg(state)
    llm_config = cfg['llm_config']
    file_name = cfg['file_name']
    session_id = cfg['session_id']
    cb = _lg_cb(cfg)
    messages = list(state.get('messages') or [])
    calls_made = list(state.get('calls_made') or [])
    content = state.get('last_content') or ''
    max_iter = cfg['max_iterations']
    round_no = int(state.get('iteration') or 0)
    _fast = bool(cfg.get('fast'))   # ⚡快速档：终稿收紧预算 + 要求核心结论

    messages = messages + [{'role': 'assistant', 'content': content}]
    if truncated:
        prompt = ((FORCE_FINAL_PROMPT if file_name else FORCE_FINAL_NO_FILE_PROMPT)
                  + "\n\n" + REFLECTION_PROMPT)
        disp_iter = max_iter
        disp_msg = '达到最大轮数，生成最终报告...'
    else:
        prompt = ((REFINEMENT_PROMPT if file_name else REFINEMENT_NO_FILE_PROMPT)
                  + "\n\n" + REFLECTION_PROMPT)
        disp_iter = round_no
        disp_msg = '生成最终报告...'
    if file_name:
        prompt = prompt + (f"\n\n当前分析的数据文件为：{file_name}。"
                           "请将【建议操作】每条 ACTION 中的 file 参数填写为这个文件名（不要用占位符）。")
    try:
        _wl = _verified_number_whitelist(calls_made)
        if _wl:
            prompt = prompt + (
                "\n\n【数据必须真实 · 数字白名单】以下数字来自本轮成功工具返回，"
                "正文只能引用清单内的数值（可换算单位/约数），不得引入清单外的具数；"
                "清单外无法得到的数据一律写「待补充」，禁止编造：\n" + _wl
            )
    except Exception:
        pass
    messages = messages + [{'role': 'user', 'content': prompt}]
    if _fast:
        # ⚡快速档终稿：要核心结论而不是完整长报告 —— 与 2000 token 预算配套，
        # 避免长报告被 max_tokens 硬截断在句子中间（观感比"短而完整"更差）。
        messages = messages + [{'role': 'user', 'content':
                                '⚡ 快速模式：只输出核心结论与关键依据，条理清晰、'
                                '600 字以内，不必展开完整报告与冗长免责段落。'}]
    if cb:
        cb({'status': 'running', 'iteration': disp_iter,
            'max_iterations': max_iter, 'message': disp_msg})

    # 终稿合成是用户等待的大头（流式生成至多 3200 tokens），此前完全没有埋点，
    # 于是这段耗时被归到上一轮 LLM 调用上，时间线上看不出"慢在终稿"。
    _t_fin = time.time()
    _trf0 = cfg.get('trace_id')
    if _trf0:
        try:
            # force_final = 达到最大轮数强制收尾（正常流程）；truncated 一词不再用于
            # 表达该语义（2026-09-18 拆分，此前诊断页把 force_final 误读成"终稿被截断"）。
            _trace_event(_trf0, 'finalize.start', {
                'force_final': bool(truncated), 'round': round_no,
                'max_iterations': max_iter, 'messages': len(messages)})
        except (Exception, SystemExit):
            pass

    # 终稿预算与思考开关（2026-09-18）：
    # 1) max_tokens 3200 → 4800：终稿是本轮最长生成，且 reasoning 系模型的思考与正文
    #    共享 max_tokens 预算，3200 下长报告频繁触顶截断；
    # 2) sensenova reasoning 系终稿关闭思考（reasoning_effort=none，实测 0.7s vs 3.5s）：
    #    终稿是对既有工具结果的合成，思考价值低，却挤占正文预算造成截断。
    fin_cfg = dict(llm_config or {})
    if str(fin_cfg.get('provider') or 'sensenova') == 'sensenova':
        fin_cfg['reasoning_effort'] = 'none'

    _actual_trunc = False
    try:
        content, response = _final_answer_call(messages, fin_cfg, cb,
                                               max_tokens=_final_token_budget(cfg))
        _lg_emit_usage(state['usage_total'], response)
        _actual_trunc = isinstance(response, dict) and response.get('finish_reason') == 'length'
        if truncated and parse_calls(content):
            messages[-1] = {'role': 'user', 'content': (
                f"你已成功执行了 {len(calls_made)} 次工具调用，资料已经足够。"
                "现在立即停止调用工具，基于以上工具返回的真实数据直接输出最终分析报告"
                "（纯文字 Markdown，中文；个别缺失数据标注「待补充」即可）。"
                "再输出任何 JSON 工具调用将被视为失败。")}
            content, response = _final_answer_call(messages, fin_cfg, cb,
                                                   max_tokens=_final_token_budget(cfg))
            _lg_emit_usage(state['usage_total'], response)
            _actual_trunc = isinstance(response, dict) and response.get('finish_reason') == 'length'
    except Exception as _fexc:
        # P0① 终稿生成失败不再裸吞：留痕 + 记录失败，避免静默降级后污染记忆
        logger.warning('LangGraph 终稿 LLM 调用失败: %s', _fexc)
        _trf = cfg.get('trace_id')
        if _trf:
            try:
                _trace_event(_trf, 'finalize_failed', {'error': str(_fexc)[:200]})
            except (Exception, SystemExit):
                pass
        _finish_content_failed = True
    else:
        _finish_content_failed = False

    content = _ensure_final_text(content, calls_made)
    _still_empty = not (content or '').strip()
    _ok_finish = (not _finish_content_failed) and (not _still_empty)

    if _trf0:
        try:
            _trace_event(_trf0, 'finalize.done', {
                'force_final': bool(truncated), 'truncated': bool(_actual_trunc),
                'ok': _ok_finish,
                'content_len': len(content or ''),
                'empty_output': _still_empty,
                'duration_ms': int((time.time() - _t_fin) * 1000)})
        except (Exception, SystemExit):
            pass

    tracker = get_tracker(session_id) if session_id else None
    if tracker:
        tracker.finish(content, ok=_ok_finish)
    # P0① 终稿生成失败时不写长期记忆，避免兜底/空文本污染后续结论库
    if _ok_finish and content and content.strip():
        _update_memory(session_id, _lg_memory_state(cfg), cfg['user_message'], content)
        # P0: 长期记忆 - 记录本轮关键结论，供未来 Planner/Agent 衔接
        try:
            user_id = cfg.get('user_id') or session_id or 'default'
            goal = str(cfg.get('user_message') or '').strip()
            if goal:
                get_long_term(user_id).record_interaction(
                    topic=goal[:80],
                    key_finding=str(content or '')[:400],
                )
        except (Exception, SystemExit):
            # 长期记忆属旁路增强；落盘失败（含环境守卫的 SystemExit）
            # 绝不能吞掉已生成的终稿结果。
            pass
    _remove_checkpoint(session_id)
    # 语义拆分（2026-09-18）：truncated = 终稿真的被 max_tokens 截断；
    # force_final = 达到最大轮数后强制收尾（正常流程，此前被误标成 truncated）。
    # 空终稿不得再标 ok=True（EMPTY_OUTPUT 假成功，用户看到“AI 不回复”）。
    return {'messages': messages, 'output': content, 'ok': _ok_finish,
            'end_status': 'done' if _ok_finish else (
                'finalize_failed' if _finish_content_failed else 'empty_output'),
            'degraded': (not _ok_finish),
            'truncated': bool(_actual_trunc),
            'force_final': bool(truncated)}


def _lg_finalize(state: Dict[str, Any]) -> Dict[str, Any]:
    """无工具调用且收敛：正常生成终稿。"""
    return _lg_finish(state, truncated=False)


def _lg_force_final(state: Dict[str, Any]) -> Dict[str, Any]:
    """达到最大轮数后的强制收尾。"""
    return _lg_finish(state, truncated=True)


def _lg_route_agent(state: Dict[str, Any]) -> str:
    """agent 节点后据 next 定向（显式 Turn 状态机：记录本轮流转阶段）。"""
    target = state.get('next') or 'tools'
    _lg_trace_turn_state(state, 'agent', target)
    return target


# P1-1 显式 Turn 状态机：把 LangGraph 内部节点名映射为可观测的语义阶段，每次流转
# 写一条 turn.state 事件到 trace_store，使 Timeline 可还原"规划→思考→执行→收尾"完整状态机。
_LG_PHASE_NAMES = {
    'planner': 'planning', 'agent': 'thinking', 'tools': 'executing_tools',
    'retry': 'thinking', 'finalize': 'finalizing', 'force_final': 'finalizing',
    'end': 'completed',
}


def _lg_trace_turn_state(state: Dict[str, Any], from_node: str, to_node: str) -> None:
    """记录一次 Turn 状态迁移（from_stage→to_stage），随 iteration 递增；仅写 trace，零副作用。"""
    try:
        cfg = state.get('cfg') or {}
        _trace_event(cfg.get('trace_id'), 'turn.state', {
            'iteration': state.get('iteration'),
            'from': _LG_PHASE_NAMES.get(str(from_node), str(from_node)),
            'to': _LG_PHASE_NAMES.get(str(to_node), str(to_node)),
            'from_node': from_node,
            'to_node': to_node,
        })
    except Exception as _e:
        logger.debug("turn.state trace 跳过: %s", _e)


def _lg_route_tools(state: Dict[str, Any]) -> str:
    """tools 节点后判断是否已达轮次上限或超出墙钟预算。"""
    if state['iteration'] >= state['cfg']['max_iterations']:
        _lg_trace_turn_state(state, 'tools', 'force_final')
        return 'force_final'
    # P1-4②：整轮墙钟 deadline —— 超时走 force_final（保留已完成部分成稿），
    # 而非让请求无限期占用线程。轮次上限只约束轮数，不约束单轮耗时。
    _dl = state['cfg'].get('deadline_ts')
    if _dl and time.time() > _dl:
        _lg_trace_turn_state(state, 'tools', 'force_final')
        return 'force_final'
    _lg_trace_turn_state(state, 'tools', 'agent')
    return 'agent'


def _get_langgraph():
    """构建并缓存 LangGraph StateGraph（纯 Python 节点，复用现有 llm_client/工具）。
    挂载 MemorySaver 原生 checkpointer：按 thread_id 持久化每轮状态，支持图内状态
    恢复/回放（取消-恢复仍走自定义 JSON checkpoint 作降级，两者互为兜底）。"""
    global _LG_STATE_G, _LG_CHECKPOINTER
    if _LG_STATE_G is not None:
        return _LG_STATE_G
    from langgraph.graph import StateGraph, START, END
    from langgraph.checkpoint.memory import MemorySaver
    _LG_CHECKPOINTER = MemorySaver()
    schema = _lg_state_schema()
    builder = StateGraph(schema)
    builder.add_node('planner', _lg_planner)   # P0: 图示「规划」层
    builder.add_node('agent', _lg_agent)
    builder.add_node('tools', _lg_tools)
    builder.add_node('finalize', _lg_finalize)
    builder.add_node('force_final', _lg_force_final)
    # START → planner（复杂任务产出结构化计划） → agent（无 plan 时 planner 为透传）
    builder.add_edge(START, 'planner')
    builder.add_edge('planner', 'agent')
    builder.add_conditional_edges('agent', _lg_route_agent,
                                  {'tools': 'tools', 'finalize': 'finalize',
                                   'retry': 'agent', 'end': END})
    builder.add_conditional_edges('tools', _lg_route_tools,
                                  {'agent': 'agent', 'force_final': 'force_final'})
    builder.add_edge('finalize', END)
    builder.add_edge('force_final', END)
    _LG_STATE_G = builder.compile(checkpointer=_LG_CHECKPOINTER)
    return _LG_STATE_G


def agent_loop(
    user_message: str,
    llm_config: Dict[str, Any],
    max_iterations: int = 5,
    file_name: str = None,
    session_id: str = None,
    progress_callback: Callable = None,
    resume: bool = False,
    trace_run_id: str = None,
    tool_names: List[str] = None,
    slim_prompt: bool = False,
    no_plan: bool = False,
    fast: bool = False,
) -> Dict[str, Any]:
    """
    Run the agent loop with DeepSeek V3 optimization
    
    Args:
        user_message: User's question
        llm_config: LLM configuration
        max_iterations: Maximum number of tool call iterations (auto-adjusts: simple=3, complex=8)
        file_name: Current file being analyzed
        session_id: Session id for progress tracking
        progress_callback: Optional callback for progress updates
        resume: Restore from checkpoint (LangGraph checkpointing pattern)
        trace_run_id: 复用父级 trace run（如 agent_pipeline）。非空时本函数只向该 run
            追加事件、不落 run_end，收尾由父级负责 —— 保证一次用户请求对应一条时间线。
        tool_names: 工具白名单（多Agent管线按阶段裁剪用）。None=全量下发（老路径零回归）。
        slim_prompt: 提示词瘦身（管线阶段用）。跳过技能正文/技能索引、知识点库、文件 RAG、
            DID/面板/金融 CoT 框架、会话记忆等重注入 —— 这些注入每轮迭代都会重发。
        no_plan: 跳过规划（管线阶段用）。管线已通过 STAGE_DEFS 给定角色与职责，
            再跑 legacy planner + LangGraph planner 节点纯属多余的两轮 LLM 往返。
        fast: ⚡快速档（快速模式用）。收紧终稿预算（4800→2000）并要求只给核心结论
            —— 终稿是本轮最长单次生成，实测占 LLM 累计耗时一半以上。调用方
            （ai_assistant._prepare_agent_run）同时会关闭全链路思考并把迭代预算压到 2 轮。
            默认 False，深度思考与多 Agent 管线逐字节零回归。
    """
    from llm_client import chat

    _maybe_warmup_retrieval()

    # Adaptive iterations based on question complexity（控制轮数以提速）
    # 分步分析要求：无文件行业/标的调研取数维度多（规模/格局/政策/财务…），
    # 预算过小会在"还在取数"时被强制收尾 → 查到了数据却没写报告
    # 动态预算 2~8 轮：按意图/复杂度加权，DID/政策评估/深度行业调研给足取数轮次，
    # 简单单文件问题压低轮次以提速。
    if max_iterations == 'auto':
        question_len = len(user_message)
        has_did = any(k in user_message for k in ['DID', 'did', '双重差分', '因果', '政策评估', '试点'])
        has_multi = any(k in user_message for k in ['全面', '完整', '详细', '所有', '深度', '报告'])
        n = 3
        if has_did:
            n = 6                                  # 因果/政策评估：取数维度多
        elif has_multi:
            n = 5                                  # 全面/深度成果：多维多工具
        elif not file_name:
            n = 4                                  # 无文件时事/市场：联网多源检索
        # 长度加权上浮，但封顶 6（保守轮次，降低云端免费额度 429 风险）
        if question_len > 200:
            n = max(n, 5)
        elif question_len > 100:
            n = max(n, 4)
        max_iterations = n
        if file_name and question_len < 30:
            max_iterations = min(max_iterations, 2)  # 简单单文件问题：至多 2 轮
        max_iterations = min(6, max_iterations)
        _needs_plan = has_did or has_multi or question_len > 100 or not file_name
    else:
        _needs_plan = max_iterations >= 4 or not file_name

    # 工具集：管线阶段按职责裁剪白名单（每轮迭代都会重发 tool 描述，裁剪直接缩短 prompt）
    _tool_names = list(tool_names) if tool_names else None
    if _tool_names:
        _keep, _seen = [], set()
        for _n in _tool_names:
            if _n not in _seen and registry.get_tool(_n):
                _seen.add(_n)
                _keep.append(_n)
        _tool_names = _keep or None
    tools_prompt = (registry.get_tools_prompt(_tool_names) if _tool_names
                    else registry.get_tools_prompt())
    system_prompt = AGENT_SYSTEM_PROMPT.replace("{tools_prompt}", tools_prompt)
    # P1c: 按意图标注本轮高优先级工具组，降低注意力稀释（其余工具仍可用）。
    # 已裁剪白名单时不注入：提示词里点名的工具可能不在本轮白名单内，会误导模型调用。
    if not _tool_names:
        _intent_hint = _intent_tool_groups(user_message, file_name)
        if _intent_hint:
            system_prompt = system_prompt + _intent_hint

    # 注入已启用的 Skills（Trae Work 风格：自动匹配命中 → 直接注入技能正文；
    # 未命中 → 注入索引，仍可经 run_skill 主动加载）
    # slim_prompt=True（多Agent管线阶段）时整段跳过：技能正文/索引是每轮迭代都要
    # 重发的 token 大宗开销，而管线阶段的职责已由 STAGE_DEFS.focus 明确给定。
    _active_skill = None
    if not slim_prompt:
        try:
            from skill_manager import get_skills_prompt, match_skill
            from skill_manager import _SKILL_TRIGGERS  # noqa: 仅在自动匹配逻辑引用
            skill_match = match_skill(user_message)
            if skill_match.get('matched') and skill_match.get('body'):
                _active_skill = skill_match['name']
                system_prompt = system_prompt + (
                    f"\n\n# 已自动加载技能：{skill_match['name']}\n"
                    f"描述：{skill_match.get('description','')}\n\n"
                    f"用户请求命中了该技能。你必须严格遵循以下技能工作流完成分析：\n\n"
                    f"{skill_match['body']}"
                )
            else:
                # 关联技能未启用时提醒，便于上传/启用
                if skill_match.get('name') and not skill_match.get('body') and not skill_match.get('matched'):
                    system_prompt = system_prompt + (
                        f"\n\n提示：用户请求可能命中技能「{skill_match['name']}」，但该技能当前未启用。"
                        "请先完成基于现有工具的分析，并可在结尾提示用户启用该技能以获得更专业的深度分析。"
                    )
                _skills_ctx = get_skills_prompt(active_call=True)
                if _skills_ctx:
                    system_prompt = system_prompt + "\n\n" + _skills_ctx
        except Exception as e:
            logger.debug("Skills 注入失败: %s", e)
    
    # 注入知识点库上下文：用户问题命中知识点时，作为参考依据（P2-10：注入深度随复杂度自适应）
    if not slim_prompt and not _should_skip_heavy_inject(user_message):
        try:
            from knowledge_rag import get_knowledge_context
            kb_ctx = get_knowledge_context(user_message, top_k=_kb_inject_top_k(user_message))
            if kb_ctx:
                system_prompt = system_prompt + "\n\n# 知识点库参考\n" + kb_ctx
        except Exception as e:
            logger.debug("知识点上下文注入跳过: %s", e)

    # 用户文件语义索引 + RAG 提示（RAG 增强）：上传文件内容可被 file_rag 工具检索，
    # 让 Agent 能理解"文件里各列/概念的含义与量级"，超越纯 SQL。
    # 注意：即使 slim_prompt，仍要为 file_rag 工具登记当前文件（工具侧依赖该状态）。
    try:
        if file_name:
            import file_rag as _frag
            _frag.index_current_file(file_name)
            tool_file_rag._active_file = file_name
            if not slim_prompt:
                _fctx = _frag.current_file_context(user_message)
                if _fctx:
                    system_prompt = system_prompt + "\n\n# 用户文件语义检索\n" + _fctx + \
                        "\n（如需了解文件内具体列含义、概念对应关系、数值区间，可用 file_rag 工具检索。）"
    except Exception as e:
        logger.debug("用户文件 RAG 注入跳过: %s", e)

    # 注入领域分析框架：DID/政策评估 → 因果推断框架；面板结构 → 面板数据处理
    _did_kw = ('did', '双重差分', '长护险', '试点', '政策评估', '因果', '准自然', '处理组', '对照组', '政策效应')
    _panel_kw = ('省份', '年份', '面板', '31省', '省级', '个体', '时间序列', '政策前', '政策后')
    if not slim_prompt:
        if any(k in user_message for k in _did_kw):
            system_prompt = system_prompt + "\n\n" + get_prompt('did_framework')
        if any(k in user_message for k in _panel_kw):
            system_prompt = system_prompt + "\n\n" + get_prompt('panel_data_hint')

        # 金融深度分析（3.1 CoT）：基金/固收/风控/评分卡/净值等场景注入金融思考框架
        _fin_cot_kw = ('基金', '净值', '夏普', '回撤', '波动', '固收', '债券', '久期', '利差',
                       '组合', '持仓', '风控', '评分卡', 'woe', 'iv', 'psi', 'ks', '仓位',
                       '年化', '基准', '超额收益', 'alpha', 'beta', '票面', '互换')
        if any(k in user_message for k in _fin_cot_kw):
            system_prompt = system_prompt + "\n\n" + get_prompt('financial_cot')

    # 分步分析纪律（系统强制）：配合循环内的步骤守卫，严禁跳过资料获取直接写结论
    if STEP_DISCIPLINE_PROMPT:
        system_prompt = system_prompt + "\n\n" + STEP_DISCIPLINE_PROMPT

    # 无文件且属于「时事/市场/因果原因」类问题：强制先联网取数，再分维度组织报告，
    # 保证 web_search 工具在无文件深度分析中真正被调用（而不是凭空写结论）。
    _live_kw = ('为什么', '原因', '归因', '最新', '上涨', '下跌', '走高', '反弹', '回落',
                '走势', '趋势', '影响', '展望', '景气', '行情', '涨价', '降价', '供需', '新闻')
    if not file_name and any(k in user_message for k in _live_kw):
        system_prompt = system_prompt + (
            "\n\n# 联网取数要求（强制）\n本次为「未上传数据文件」的时事/市场/原因类问题。"
            "你必须在给出任何结论之前，用 web_search 工具联网检索最新公开资料（价格、政策、新闻、行情），"
            "并尽量按供需/政策/国内外/成本等多个维度展开检索与组织。所有观点须以真实检索结果支撑；"
            "检索不到的数据在报告中标注「待补充」，严禁臆造具体数字。"
        )

    # 会话记忆（跨轮次状态：上下文注入 + 历史摘要）；未传 session_id 时不启用
    # 管线阶段（slim_prompt）仍创建 memory_state（收尾要落记忆），但不注入历史上下文：
    # 阶段各自只对本次任务负责，历史摘要会稀释注意力且抬高每轮 prompt。
    memory_state = None
    if session_id:
        memory_state = _memory_manager.get_or_create(session_id)
        memory_ctx = memory_state.to_natural_language()
        if (not slim_prompt) and memory_ctx and memory_ctx != "无历史上下文":
            system_prompt = system_prompt + "\n\n# 会话上下文（来自历史分析）\n" + memory_ctx

    # ---- 恢复中断的分析（LangGraph checkpointing）----
    plan = None
    failed_tools = {}
    if resume and session_id:
        cp = _load_checkpoint(session_id)
        if cp and cp.get('messages'):
            clear_cancel(session_id)  # 清除上次的取消标记
            messages = cp['messages']
            calls_made = cp.get('calls_made', [])
            plan = cp.get('plan')
            failed_tools = cp.get('failed_tools', {})
            if progress_callback:
                progress_callback({
                    'status': 'running',
                    'message': f'已从断点恢复（已执行 {len(calls_made)} 次工具调用），继续分析...'
                })
        else:
            resume = False

    if not resume:
        # ---- Plan-and-Execute 规划阶段（LangGraph planner 模式）----
        # 复杂任务先让 LLM 产出执行计划，注入 system 提示引导逐步推进
        # no_plan=True（多Agent管线阶段）跳过：管线已用 STAGE_DEFS.focus 给出该阶段的
        # 角色与产出要求，再规划一次是纯粹的额外 LLM 往返（实测每阶段浪费 1~2 次）。
        if _needs_plan and not no_plan:
            try:
                if progress_callback:
                    progress_callback({'status': 'running', 'message': '正在制定执行计划...'})
                p_resp = chat(
                    [{"role": "system", "content": PLANNER_PROMPT},
                     {"role": "user", "content": user_message[:2000]}],
                    llm_config, max_tokens=300)
                p_content = (p_resp.get("content", "") if isinstance(p_resp, dict) else str(p_resp))
                plan = _parse_plan(p_content)
            except Exception as e:
                logger.debug("规划阶段失败（跳过，不影响主流程）: %s", e)
                plan = None

        if plan:
            plan_text = "\n".join(f"{i}. {s}" for i, s in enumerate(plan, 1))
            system_prompt = system_prompt + (
                "\n\n# 执行计划（Plan-and-Execute：请按计划逐步调用工具推进，全部完成后输出最终报告）\n"
                + plan_text
            )
            if progress_callback:
                progress_callback({
                    'type': 'plan', 'steps': list(plan),
                    'status': 'running',
                    'message': f'已生成执行计划（{len(plan)} 步），开始执行...'
                })

        # P1b + P0-1: system prompt 预算化（先语义压缩低优先级分节，仍超限才优先级截断）
        system_prompt = _budget_system_prompt(
            system_prompt, llm_config=llm_config, user_message=user_message)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message}
        ]
        calls_made = []

    usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    # 连续失败降级保护（配额耗尽/限流/超时等场景）：连续 fail_limit 次 LLM 调用失败即提前终止，
    # 避免在多轮迭代中高频空转消耗；终止信息引导用户切换模型或等待配额重置。
    _FAIL_LIMIT = 2
    _t0 = time.time()  # 总耗时统计（前端展示"思考用时"）

    # ---- P0: 结构化追踪（JSONL 轨道 + OTel 槽位）----
    # trace_run_id 非空 = 复用父级 run（当前只有 agent_pipeline 这么做）：
    # 管线一次请求会串联多个 agent_loop，若各自新建 run，一次用户请求会碎成多条
    # 互相独立的轨迹，"哪一阶段挂了"就无法在同一时间线上看出。复用模式下只追加
    # 事件、不落 run_end（收尾交父级），否则首个阶段的 run_end 会让整条轨迹提前"结束"。
    _trace_id = None
    _owns_trace = False
    try:
        if trace_run_id:
            _trace_id = trace_run_id
            _trace_event(_trace_id, "pipeline.stage_config", {
                "max_iterations": max_iterations, "plan": bool(plan),
                "skill": _active_skill, "resume": resume,
                "file": file_name or '',
                "no_plan": bool(no_plan), "slim_prompt": bool(slim_prompt),
                "tools_prompt_chars": len(tools_prompt),
                "tools_prompt_count": len(_tool_names) if _tool_names else None})
        else:
            _trace_run = _begin_run(session_id, user_message, llm_config, _active_skill)
            _trace_id = _trace_run.get("run_id")
            _owns_trace = True
            _trace_event(_trace_id, "agent_config", {
                "max_iterations": max_iterations, "plan": bool(plan),
                "skill": _active_skill, "resume": resume,
                "file": file_name or ''})
            # 自动注入技能也要可观测：与显式 run_skill 区分 source=auto_match
            if _active_skill:
                try:
                    _trace_event(_trace_id, "skill_loaded", {
                        "name": _active_skill, "source": "auto_match",
                        "matched": True})
                except (Exception, SystemExit):
                    pass
        # P2-8: 把本轮 RAG 检索摘要（含向量/精排是否打通、top 命中）写入 trace 轨道
        try:
            from knowledge_rag import get_last_retrieval_summary as _summary
            _ksum = _summary()
            if _ksum:
                _trace_event(_trace_id, "kb_retrieval", _ksum)
        except Exception as _e2:
            logger.debug("kb_retrieval trace 跳过: %s", _e2)
    except Exception as e:
        logger.debug("trace 开始失败: %s", e)

    # ---- P1a: 原生 function calling 决策（P1-6：按模型可自适应降级）----
    _llm_provider = (llm_config.get("provider", "sensenova") if llm_config else "sensenova")
    _llm_model = (llm_config.get("model", "") if llm_config else "")
    _use_native = _provider_native_tools(_llm_provider, _llm_model)
    # 管线阶段按白名单裁剪 tools payload；无白名单时保持零回归（含既有 monkeypatch 形态）
    _openai_tools = None
    if _use_native:
        _openai_tools = (_build_openai_tools(_tool_names) if _tool_names
                         else _build_openai_tools())

    # Initialize progress tracker
    tracker = get_tracker(session_id) if session_id else None
    if tracker:
        tracker.start(max_iterations)
    
    # ============================================================================
    # LangGraph 编排核心循环：图结构与节点契约见模块级 _LG_STATE_G 上方注释（L2808 附近），
    # 此处只留运行期装配。节点复用现有 llm_client / ToolRegistry / 追踪 / checkpoint，
    # agent_loop 对外返回契约保持不变。
    # ============================================================================
    # 运行期上下文（回调 / 记忆对象）外挂注册表：二者不可 msgpack 序列化，
    # 放进图状态会让 MemorySaver 在 checkpoint 时抛错，故只把 token 写进 cfg。
    _runtime_token = f"lgrun-{uuid.uuid4().hex}"
    _LG_RUNTIME[_runtime_token] = {
        'progress_callback': progress_callback,
        'memory_state': memory_state,
    }

    cfg = {
        'llm_config': llm_config,
        'file_name': file_name,
        'session_id': session_id,
        'runtime_token': _runtime_token,
        'max_iterations': max_iterations,
        'use_native': _use_native,
        'openai_tools': _openai_tools,
        'no_plan': bool(no_plan),
        'fast': bool(fast),          # ⚡快速档：终稿收紧预算 + 简洁输出（默认 False 零回归）
        'tool_names': _tool_names,
        'trace_id': _trace_id,
        'plan': plan,
        'fail_limit': _FAIL_LIMIT,
        'min_success_calls': MIN_SUCCESS_CALLS,
        'step_guard_max': _STEP_GUARD_MAX,
        'user_message': user_message,
        # P1-4② 整轮墙钟预算：默认 600s，可用 AGENT_DEADLINE 覆盖（0/负值=不启用）
        'deadline_ts': (_t0 + float(os.environ.get('AGENT_DEADLINE', '600'))
                        if float(os.environ.get('AGENT_DEADLINE', '600')) > 0 else None),
    }

    initial_state = {
        'cfg': cfg,
        'messages': messages,
        'calls_made': calls_made,
        'failed_tools': failed_tools,
        'usage_total': usage_total,
        'iteration': 0,          # 图内 1-based，自 0 起逐轮递增
        'pending_calls': [],
        'last_content': '',
        'consec_fail': 0,
        'step_guards': 0,
        'next': 'agent',
        'ok': False,
        'output': '',
        'end_status': '',
        'error': None,
        'degraded': False,
        'cancelled': False,
        'resumable': False,
        'truncated': False,
        'facts': [],
        'missing': [],
        'reflections': 0,
        'plan': [],                       # Planner 节点产出，由 _lg_planner 填充
        'plan_tools_hint': {},            # 步骤→工具提示（弱约束，agent 节点可选）
    }

    # LangGraph 原生 checkpointer：以 session_id 作 thread_id 持久化每轮状态，
    # 显式 recursion_limit 防止异常（如 step_guard 自环）耗尽默认限制导致抛错。
    _lg_config = {
        'recursion_limit': 60,
        'configurable': {'thread_id': session_id or 'default'},
    }
    try:
        final_state = _get_langgraph().invoke(initial_state, config=_lg_config)
    except (Exception, SystemExit) as _graph_exc:
        # P2-7：图执行抛错（非 force_final 路径）时也必须落 run_end，
        # 否则轨迹出现"有 run_start 无 run_end"的悬挂运行，污染收尾率统计。
        # 捕获后立即 re-raise：不吞异常，保留上层兜底语义；
        # 仅捕获 Exception/SystemExit，以保留 KeyboardInterrupt 传播。
        # 2026-09-18 修复：原先 summary 只写 'graph invoke raised' 五个字，真实异常
        # （类型+消息）丢失，历史 23 次失败无任何根因可溯。现在记录 error_type/
        # error_detail 并补一条 graph.error 事件，诊断页可直接看到出错节点线索。
        _exc_type = type(_graph_exc).__name__
        _exc_detail = str(_graph_exc).replace('\n', ' ')[:500]
        try:
            if _trace_id and _owns_trace:
                _trace_event(_trace_id, 'graph.error', {
                    'error_type': _exc_type, 'error': _exc_detail})
                _end_run(_trace_id, 'error', summary={
                    'iterations': 0, 'error': 'graph invoke raised',
                    'error_type': _exc_type, 'error_detail': _exc_detail})
        except (Exception, SystemExit):
            pass
        raise
    finally:
        # 无论成功/异常/取消，都回收运行期上下文，避免注册表无限增长
        _LG_RUNTIME.pop(_runtime_token, None)

    # ---- 提取并组装返回（与旧 for 循环各 return 分支对齐）----
    end_status = final_state.get('end_status') or ''
    ok = bool(final_state.get('ok'))
    output = final_state.get('output') or ''
    calls_made = final_state.get('calls_made') or calls_made
    iterations = final_state.get('iteration') or 0
    usage_total = final_state.get('usage_total') or usage_total
    elapsed_ms = int((time.time() - _t0) * 1000)

    # 结束追踪（run 摘要），并注入 trace_id / trace_file
    # 复用父级 run 时不落 run_end：收尾由 agent_pipeline 统一负责。
    _outcome = 'ok' if ok else (end_status or ('error' if final_state.get('error') else 'cancelled'))
    if _trace_id and _owns_trace:
        try:
            _end_run(_trace_id, _outcome, summary={
                'iterations': iterations,
                'errors': sum(1 for c in calls_made if c.get('status') == 'error'),
                'tool_calls': len(calls_made),
                'total_tokens': usage_total.get('total_tokens'),
                'elapsed_ms': elapsed_ms,
                'output_len': len(output),
                # 语义拆分（2026-09-18）：force_final=达轮次上限收尾（正常）；
                # truncated=终稿被 max_tokens 截断（真问题）。
                'force_final': bool(final_state.get('force_final')),
                'truncated': bool(final_state.get('truncated')),
            })
        except Exception:
            pass

    result = {
        'ok': ok,
        'output': output,
        'calls': calls_made,
        'iterations': iterations,
        'usage': usage_total,
        'plan': plan,
        'trace': _trace_summary(calls_made, usage_total, _active_skill),
        'elapsed_ms': elapsed_ms,
        # 失败归因（2026-09-18 晚）：此前 result 不带 error/end_status，调用方（如
        # agent_pipeline 的阶段事件）只能拿到 ok=False 却看不到原因，健康面板上
        # 表现为"阶段失败但 error 为空"。end_status 取值：done / finalize_failed /
        # empty_output / degraded / error / cancelled。
        'end_status': end_status,
    }
    # ---- 事实核查（数据必须真实）：报告数字须对上成功工具的真实返回 ----
    # 触发：本轮有成功工具调用，或有联网检索（不仅限“无文件”场景）。
    # 产出【核验数据】清单；失败工具结果不得作为事实依据。
    if ok and output:
        _ok_calls = [c for c in (calls_made or [])
                     if str((c or {}).get('status') or 'ok').lower() not in _FAIL_CALL_STATUS]
        if _ok_calls or _has_research_call(calls_made):
            try:
                _note, _fstats = _audit_fact_sources(output, calls_made)
                if _note:
                    output = output + _note
                    result['output'] = output
                if _fstats:
                    # 结果契约：前端可展示核验数据（verified_data / unverified_data）
                    result['fact_audit'] = _fstats
                    # 生成后门禁（2026-09-22）：未核验率过高 → 显式 degraded，
                    # 避免“附了核验附录但仍当完整成功报告”被前端当可信结论。
                    try:
                        _ratio = float(_fstats.get('ratio') or 0)
                    except (TypeError, ValueError):
                        _ratio = 0.0
                    if _ratio > 0.5 and _fstats.get('total'):
                        result['degraded'] = True
                        result['end_status'] = result.get('end_status') or 'degraded'
                        result['fact_unverified_high'] = True
                if _trace_id and _fstats:
                    _trace_event(_trace_id, "fact_audit", _fstats)
            except Exception as e:
                logger.debug("事实核查跳过: %s", e)
    # ---- 有数据文件却零成功工具调用：结果可信度不足，必须显式降级提示 ----
    if file_name and ok and output and not any(
            c.get('status') == 'ok' for c in (calls_made or [])):
        _warn = (f"\n\n> ⚠️ 数据可信度提示：本轮已挂载数据文件「{file_name}」，"
                 "但未成功执行任何数据工具，上述内容未基于真实文件核验，仅供参考。"
                 "请重新提问，或先在数据页确认文件已上传/选择。")
        output = output + _warn
        result['output'] = output
        result['no_tool_with_file'] = True
        if _trace_id:
            try:
                _trace_event(_trace_id, "quality.warn", {
                    "code": "NO_TOOL_WITH_FILE", "file": file_name})
            except (Exception, SystemExit):
                pass
    if final_state.get('truncated'):
        result['truncated'] = True
    if final_state.get('force_final'):
        result['force_final'] = True
    if final_state.get('degraded'):
        result['degraded'] = True
    if final_state.get('error'):
        result['error'] = final_state.get('error')
    if final_state.get('cancelled'):
        result['cancelled'] = True
    if final_state.get('resumable'):
        result['resumable'] = True
    if _trace_id:
        result['trace_id'] = _trace_id
        result['trace_file'] = _trace_file(_trace_id)
    return result


def _update_memory(session_id, memory_state, query, answer=None):
    """将本轮查询/回答写入会话记忆（保留最近 10 轮）。"""
    if not session_id or memory_state is None:
        return
    memory_state.last_query = query
    # 同一 query 在 agent 多轮工具循环中会被反复调用（每轮 iteration 末尾 + 最终）。
    # 若最后一条已是该 query，则只更新 answer，避免同一问题在摘要区重复 N 条。
    if memory_state.conversation_summary and \
            memory_state.conversation_summary[-1].get('query') == query:
        memory_state.conversation_summary[-1]['answer'] = (answer or '')[:200]
    else:
        memory_state.conversation_summary.append({
            'query': query,
            'answer': (answer or '')[:200],
            'timestamp': time.time(),
        })
    if len(memory_state.conversation_summary) > 10:
        memory_state.conversation_summary = memory_state.conversation_summary[-10:]
    memory_state.updated_at = time.time()


# 输出净化（_strip_tool_json / _clean_final_output）已迁至 agent_text_protocol.py。


def _step_guard_message(ok_calls: int, min_calls: int, plan=None) -> str:
    """分步守卫拦截消息：模型资料不足却想写终稿时，强制其回到资料获取步骤。"""
    lines = [
        "## 步骤守卫（系统强制，严禁跳过）",
        f"分步分析尚未完成：已成功执行工具 {ok_calls} 次，最低要求 {min_calls} 次。",
        "你刚才试图直接输出结论——这不符合分步分析纪律。请继续调用工具补齐资料：",
    ]
    if plan:
        lines.append("执行计划（未完成的步骤严禁跳过）：")
        lines += [f"{i}. {s}" for i, s in enumerate(plan, 1)]
    else:
        lines.append("- 有文件：columns/overview → describe/分组 → 相关/建模，逐层推进；")
        lines.append("- 无文件：web_search/金融工具分别获取核心数据（如行业规模、竞争格局、政策），分多次查询。")
    lines.append("若某项资料确实无法获取，先调用工具尝试并如实记录失败，再进入下一步。")
    return "\n".join(lines)


def _ensure_final_text(content: str, calls_made: list) -> str:
    """保证最终输出是可读文字：清理工具 JSON；若清理后为空（模型到上限一直在调用
    工具、没给文字），基于已执行的查询生成诚实兜底——绝不把裸 JSON 漏给用户，
    也不在没有资料支撑时假装有分析。同时加“终稿守卫”：当成功工具调用为 0 时，无论
    模型生成了什么，都追加免责标注，避免无据结论被当作真实分析。"""
    # 终稿守卫：尝试过工具但全部失败（成功调用为 0）→ 已生成的文字追加免责标注。
    # 若清理后无文字，则保留原「已完成的资料查询」诚实兜底。
    _ok = sum(1 for c in (calls_made or []) if c.get('status') == 'ok')
    _all_failed = bool(calls_made) and _ok == 0
    content = _clean_final_output(content)
    if content:
        if _all_failed:
            return content + ("\n\n> ⚠️ **免责标注**：本轮尝试调用的数据/联网工具均失败，"
                              "以下内容为模型根据常识推断，未经平台真实数据核验，"
                              "可能存在偏差或已过时，请谨慎参考。建议稍后重试以联网/取数核验。")
        return content
    if calls_made:
        lines = []
        for c in calls_made[-8:]:
            p = c.get('params') or {}
            key = p.get('query') or p.get('file') or p.get('skill_name') or p.get('api_path') or ''
            suffix = '[缓存]' if c.get('cached') else ''
            lines.append(f"- {c.get('tool')}({key}){suffix}")
        return ("⚠️ 模型在轮次上限内一直在调用工具，未生成文字报告。已完成的资料查询：\n"
                + "\n".join(lines)
                + "\n\n请点击「继续」或换个问法，我会基于以上已获取的资料直接输出分析结论。")
    # 零工具 + 零正文：不能静默 ok=True（用户会看到 AI 不回复）
    return (
        "⚠️ 本轮未能生成有效分析报告。\n"
        "可能原因：模型输出被思考过程占用、接口限流/异常，或未成功调用任何工具。\n"
        "请稍后重试；若已选择数据文件，可先问「看看当前文件的列名」确认数据可读后再分析。"
    )
