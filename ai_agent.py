#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ai_agent.py — Agent 编排入口（薄门面）
==========================================
模块拆分（2026-09-25）：
  agent_tool_runtime / agent_tool_registry / agent_graph_nodes /
  agent_fact_audit / agent_text_protocol

对外 API（agent_loop、registry、tool_cache…）保持不变。
"""
import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import llm_client
from agent_memory import SessionMemoryManager
from agent_long_memory import get_long_term, LongTermMemory  # noqa: F401
from trace_store import begin_run as _begin_run, event as _trace_event, end_run as _end_run
from trace_store import trace_file as _trace_file

# ---- 拆分模块：工具运行时 / 注册表 / 图节点 / 事实核查 ----
from agent_tool_runtime import (  # noqa: F401
    ToolCache,
    ToolTimeoutError,
    ProgressTracker,
    tool_cache,
    get_tracker,
    remove_tracker,
    cancel_session,
    clear_cancel,
    is_cancelled,
    _call_tool_with_timeout,
    _file_fingerprint,
    _tool_ttl,
    _tool_executor,
    TOOL_TIMEOUT_S,
    _active_trackers,
    _cancel_flags,
)
from agent_tool_registry import model_tool_chat as _model_tool_chat_impl  # noqa: E402
from agent_tool_registry import (  # noqa: F401
    ToolRegistry,
    registry,
    register_tool,
    bind_runtime as _bind_tools_runtime,
    _build_openai_tools,
    _normalize_tool_params,
    _TOOL_PARAM_SCHEMA,
    _clip_tool_result,
    _tool_return_budget,
    _first_round_retry_prompt,
    NO_CALLS_NATIVE_RETRY,
    tool_list_skills,
    tool_run_skill,
)
from agent_fact_audit import (  # noqa: F401
    _FAIL_CALL_STATUS,
    _audit_fact_sources,
    _real_tool_evidence,
    _verified_number_whitelist,
    _extract_numeric_tokens,
)
from agent_graph_nodes import (  # noqa: F401
    bind_runtime as _bind_graph_runtime,
    _compress_observation,
    _prepare_tool_call,
    _lg_planner,
    _lg_agent,
    _lg_tools,
    _lg_finish,
    _lg_finalize,
    _lg_force_final,
    _lg_route_agent,
    _lg_route_tools,
    _lg_safe_cfg,
    _lg_ctx,
    _lg_cb,
    _lg_emit_usage,
    _lg_memory_state,
    _get_langgraph,
    _LG_RUNTIME,
)
from agent_text_protocol import parse_calls, _extract_json_objects, _extract_reasoning  # noqa: F401
from agent_text_protocol import _strip_tool_json, _clean_final_output  # noqa: F401

from prompts import get_prompt as _get_prompt
get_prompt = _get_prompt

AGENT_SYSTEM_PROMPT = _get_prompt('agent_system')
REFLECTION_PROMPT = _get_prompt('reflection')
STEP_DISCIPLINE_PROMPT = _get_prompt('step_discipline')
FORCE_FINAL_PROMPT = _get_prompt('force_final')
FORCE_FINAL_NO_FILE_PROMPT = _get_prompt('force_final_no_file')
PLANNER_PROMPT = _get_prompt('planner')
NO_TOOLS_PROMPT = _get_prompt('no_tools_retry')

# ---- 门面 re-export（单一实现均在 agent_tool_registry / tool_boundary）----
# 修复双实现拆分遗留：tests/test_tool_boundary.py 等以 `ai_agent.xxx` 为稳定导入点；
# 此前符号缺失导致 11 个边界测试 AttributeError（endpoint_risk/safe_output_path/
# tool_clean_data/_FUNCTIONAL_PREFIXES）。
from agent_graph_nodes import (  # noqa: E402,F401
    _final_answer_call,
    _looks_like_tool_call,
    _TOOLS_PARALLEL,
    _final_token_budget,
    _FAST_FINAL_MAX_TOKENS,
    _FINAL_MAX_TOKENS,
)
from agent_tool_registry import (  # noqa: E402,F401
    endpoint_risk,
    safe_output_path,
    tool_clean_data,
    tool_feature_eng,
    tool_ml_train,
    _FUNCTIONAL_PREFIXES,
)
REFINEMENT_PROMPT = _get_prompt('refinement')
REFINEMENT_NO_FILE_PROMPT = _get_prompt('refinement_no_file')
try:
    AGENT_SYSTEM_PROMPT = AGENT_SYSTEM_PROMPT + (
        "\n\n# 投资合规要求（必须遵守）\n"
        "凡涉及投资/估值/财务的判断，结论处注明「本结论为客观数据分析，仅供参考，不构成任何投资建议」。"
    )
except Exception:
    pass

def _has_research_call(calls) -> bool:
    if not calls:
        return False
    for c in calls:
        t = str((c or {}).get('tool') or (c or {}).get('name') or '').lower()
        if any(r in t for r in ('web_search', 'search', 'web', 'fetch', 'news', 'crawl', 'scrape')):
            return True
    return False


def model_tool_chat(*args, **kwargs):
    """兼容入口：同步 registry 的 _build_openai_tools，便于测试 monkeypatch 本模块符号。"""
    try:
        import agent_tool_registry as _R
        _R._build_openai_tools = _build_openai_tools
    except Exception:
        pass
    return _model_tool_chat_impl(*args, **kwargs)


_KB_DEEP_KW = ()
tool_file_rag = None

logger = logging.getLogger(__name__)
_SYSTEM_BUDGET_CHARS = int(os.environ.get('AGENT_SYSTEM_BUDGET', '15000'))
_LOW_PRIORITY_HINTS = ('会话上下文', '用户文件语义检索', '知识点库', '领域框架')
_RETRIEVAL_WARMUP_DONE = False
MIN_SUCCESS_CALLS = 2
_STEP_GUARD_MAX = 2
_FAIL_LIMIT = 3
_memory_manager = SessionMemoryManager()

# 预算 / 技能注入等编排辅助
def _should_plan(user_message: str, max_iterations: int) -> bool:
    """是否启动 Planner：短问题/单工具任务跳过以省一轮 LLM"""
    if max_iterations <= 3:
        return False
    if len(user_message or '') < 12:
        return False
    multi_keywords = ("全面", "完整", "详细", "所有", "深度", "报告", "对比",
                      "然后", "接着", "并发", "再", "并", "分析", "评估")
    return any(k in user_message for k in multi_keywords)


def _extract_user_goal(messages: List[Dict[str, Any]]) -> str:
    """从消息列表中提取最近一条 user 消息作为目标"""
    for m in reversed(messages or []):
        if isinstance(m, dict) and m.get('role') == 'user':
            return str(m.get('content', ''))
    return ''


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


def _budget_system_prompt(prompt: str, llm_config: dict = None,
                          user_message: str = None) -> str:
    """System Prompt 预算化（门面）——实现统一委托 agent_tool_registry（单一事实源）。

    此前的本地副本引用了未导入的 _SEMANTIC_COMPACT_* 常量，超预算分支直接 NameError
    导致深度分析崩到兜底；委托后与 registry 共享同一实现，消除双实现漂移。"""
    try:
        from agent_tool_registry import _budget_system_prompt as _impl
        return _impl(prompt, llm_config, user_message)
    except Exception:
        return prompt


def _kb_inject_top_k(message: str) -> int:
    msg = str(message)
    if len(msg) > 60:          # 长问题隐含多维诉求
        return 4
    return 4 if any(k in msg for k in _KB_DEEP_KW) else 2


def _should_skip_heavy_inject(user_message: str) -> bool:
    """简单闲聊/短问句跳过知识库等重注入，压 p95 长尾。"""
    msg = (user_message or '').strip()
    if len(msg) > 80:
        return False
    heavy = ('全面', '深度', '报告', '研究', '分析', '对比', '尽调', 'DID',
             '回归', '建模', '回测', '归因', '估值', '风险')
    return not any(k in msg for k in heavy)


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


def _section_header(pr: str, body: str) -> str:
    """提取分节标题（用于压缩时的定位与锚点标题）。"""
    m = re.match(r'^(\S.*?)(:|\n|，|。|$)', body)
    return (pr + body)[:60]


def _llm_compact_low_sections(low_sections, llm_config=None, user_message=None):
    """低优先级分节语义压缩（门面）——实现委托 agent_tool_registry 单一事实源。"""
    try:
        from agent_tool_registry import _llm_compact_low_sections as _impl
        return _impl(low_sections, llm_config, user_message)
    except Exception:
        return None


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


# ---- 运行时绑定：把共享符号注入工具/图节点模块 ----
_bind_tools_runtime(
    _memory_manager=_memory_manager,
    get_long_term=get_long_term,
    _begin_run=_begin_run,
    _trace_event=_trace_event,
    _end_run=_end_run,
    _trace_file=_trace_file,
    llm_client=llm_client,
    parse_calls=parse_calls,
    _lg_emit_usage=_lg_emit_usage,
)
_bind_graph_runtime(
    _LG_RUNTIME=_LG_RUNTIME,
    _begin_run=_begin_run,
    _trace_event=_trace_event,
    _end_run=_end_run,
    _trace_file=_trace_file,
    get_long_term=get_long_term,
    _parse_plan=_parse_plan,
    _parse_plan_v2=_parse_plan_v2 if "_parse_plan_v2" in dir() else _parse_plan,
    _should_plan=_should_plan if "_should_plan" in dir() else (lambda *a, **k: False),
    _extract_user_goal=_extract_user_goal if "_extract_user_goal" in dir() else (lambda *a, **k: ""),
    _memory_manager=_memory_manager,
    _save_checkpoint=_save_checkpoint,
    _load_checkpoint=_load_checkpoint,
    _remove_checkpoint=_remove_checkpoint,
    REFINEMENT_PROMPT=REFINEMENT_PROMPT,
    REFINEMENT_NO_FILE_PROMPT=REFINEMENT_NO_FILE_PROMPT,
    FORCE_FINAL_PROMPT=FORCE_FINAL_PROMPT,
    FORCE_FINAL_NO_FILE_PROMPT=FORCE_FINAL_NO_FILE_PROMPT,
    REFLECTION_PROMPT=REFLECTION_PROMPT,

    _first_round_retry_prompt=_first_round_retry_prompt,
    _trace_summary=_trace_summary,
)

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
                    output = _fstats.pop('annotated_text', output) + _note   # 正文内联标注 ⚠️
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


# ---- 模块拆分兼容层（2026-09-25）----
# 权威实现在 agent_fact_audit.py；若需替代本地实现，可在此 re-export。






# ---- 原生 tools 自适应降级：状态挂在本模块，便于测试 monkeypatch ----
_NATIVE_MISS_THRESHOLD = 2
_native_miss = {}
_native_disabled = set()
_native_guard = threading.Lock()
_NATIVE_AUTO_DEGRADE = True
_NATIVE_TOOLS_ENABLED = True


def _native_model_key(provider: str, model: str) -> str:
    return f"{(provider or '').lower()}/{(model or '').lower()}"


def _provider_native_tools(provider: str, model: str = None) -> bool:
    if not _NATIVE_TOOLS_ENABLED or (provider or '').lower() not in (
            'openai', 'deepseek', 'groq', 'zhipu', 'dashscope', 'sensenova'):
        return False
    if _NATIVE_AUTO_DEGRADE and model:
        with _native_guard:
            if _native_model_key(provider, model) in _native_disabled:
                return False
    return True


def _note_native_miss(provider: str, model: str, trace_id: str = None) -> bool:
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
    return triggered


# ---- 兼容 re-export（测试与旧调用点）----
from agent_tool_registry import (  # noqa: E402
    _native_disabled,
    _native_miss,
    _native_guard,
    tool_list_files,
    tool_list_analysis_api,
    tool_run_analysis_api,
    _discover_functional_endpoints,
)
from agent_graph_nodes import _safe_int_usage  # noqa: E402
from agent_graph_nodes import _intent_tool_groups  # noqa: E402
from agent_tool_registry import _NATIVE_MISS_THRESHOLD, _FUNCTIONAL_PREFIXES  # noqa: E402

from agent_tool_registry import _NATIVE_AUTO_DEGRADE, _NATIVE_TOOLS_ENABLED, _analysis_api_registered  # noqa: E402

MIN_SUCCESS_CALLS = 2
STEP_GUARD_MAX = 2
_FAIL_LIMIT = 3

# 兼容：部分测试直接引用 ai_agent._parse_plan_v2 / _should_plan
try:
    _parse_plan_v2
except NameError:
    _parse_plan_v2 = _parse_plan


# 迟绑定：函数/常量定义完成后再注入图节点模块
try:
    _bind_graph_runtime(
        REFINEMENT_PROMPT=REFINEMENT_PROMPT,
        REFINEMENT_NO_FILE_PROMPT=REFINEMENT_NO_FILE_PROMPT,
        FORCE_FINAL_PROMPT=FORCE_FINAL_PROMPT,
        FORCE_FINAL_NO_FILE_PROMPT=FORCE_FINAL_NO_FILE_PROMPT,
        REFLECTION_PROMPT=REFLECTION_PROMPT,
        _step_guard_message=_step_guard_message,
        _first_round_retry_prompt=_first_round_retry_prompt,
        _ensure_final_text=_ensure_final_text,
        _update_memory=_update_memory,
    )
except Exception:
    pass


# 超时入口可被测试 monkeypatch（指向本模块全局 _call_tool_with_timeout）
def _patchable_timeout(func, params, timeout=None):
    # 用本模块 TOOL_TIMEOUT_S，便于测试 monkeypatch
    return _call_tool_with_timeout(func, params, TOOL_TIMEOUT_S if timeout is None else timeout)


try:
    import agent_graph_nodes as _gmod
    _gmod._call_tool_with_timeout = _patchable_timeout
except Exception:
    pass
