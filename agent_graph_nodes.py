# -*- coding: utf-8 -*-
"""agent_graph_nodes.py — LangGraph 节点（从 ai_agent 拆出）"""
from __future__ import annotations
import json
import logging
import os
import re
import time
import uuid
from typing import Any, Callable, Dict, List, Optional
import llm_client
from agent_tool_runtime import (
    ToolTimeoutError,
    ProgressTracker,
    get_tracker,
    is_cancelled,
    _call_tool_with_timeout,
    submit_bound,
    _file_fingerprint,
    _tool_ttl,
    tool_cache,
)
from agent_tool_registry import (
    _first_round_retry_prompt,
    _clip_tool_result,
    _tool_return_budget,
    registry,
    _build_openai_tools,
    _normalize_tool_params,
    _TOOL_PARAM_SCHEMA,
    _provider_native_tools,
    _note_native_miss,
)
from agent_fact_audit import _verified_number_whitelist
from agent_text_protocol import _extract_reasoning, parse_calls, _extract_json_objects
logger = logging.getLogger(__name__)
MIN_SUCCESS_CALLS = 2
_STEP_GUARD_MAX = 2
_FAIL_LIMIT = 3
# 运行期注入
_LG_RUNTIME = {}
_LG_STATE_G = None
REFINEMENT_PROMPT = ''
REFINEMENT_NO_FILE_PROMPT = ''
FORCE_FINAL_PROMPT = ''
FORCE_FINAL_NO_FILE_PROMPT = ''
REFLECTION_PROMPT = ''
_step_guard_message = None
_update_memory = None
TOOL_TIMEOUT_S = 90.0
_save_checkpoint = None
_remove_checkpoint = None
_ensure_final_text = None
_LG_CHECKPOINTER = None
def _noop(*a, **k):
    return None
_begin_run = _noop
_trace_event = _noop
_end_run = _noop
_trace_file = _noop
get_long_term = None
_parse_plan = _parse_plan_v2 = _should_plan = _extract_user_goal = None
_memory_manager = None
session_memory = None

def bind_runtime(**kw):
    g = globals()
    for k, v in kw.items():
        g[k] = v


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
        return submit_bound(func, params)
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
                # P0（2026-10-09）：失败必须留痕「已尝试 X 工具，返回空」，
                # 且明确禁止滑向 web_search 顶替核心口径（web_search 仅作补充）。
                result += (f"\n（已尝试 {tool_name} 工具，返回空/失败，已留痕。"
                           "请检查参数是否正确后重试同一工具；"
                           "web_search 仅可作补充背景，不得用它顶替该口径的核心数据。）")
            else:
                result += (f"\n（{tool_name} 已连续失败 {_fails} 次，返回空，已留痕。"
                           "请勿再调用该工具；该口径数据标注「待补充」，"
                           "不得以 web_search 顶替核心数据。）")
            missing.append(f"{tool_name}：已尝试该工具，返回空（已留痕），该口径应标注「待补充」")
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

