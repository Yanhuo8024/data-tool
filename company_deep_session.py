#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""company_deep_session.py — 企业深析的**会话级**状态容器（替代模块级裸全局）

## 为什么需要这个模块

`agent_tool_registry.py` 原先用一个模块级裸变量 `_CD_LAST_STATE` 承接
`company_deep_analyze` 的分析结果，供 `company_deep_redflags` / `company_deep_moat_score` /
`company_deep_playbook` / `company_deep_fill` / `company_deep_scorecard` 复用。

该变量在 waitress（8 线程）下是**全局共享**的，且无锁、无会话键、无 TTL、永不清理：

    用户 A：company_deep_analyze("贵州茅台")      → _CD_LAST_STATE = 茅台
    用户 B：company_deep_analyze("某问题公司")    → 覆盖 _CD_LAST_STATE
    用户 A：company_deep_redflags()              → 读到**B 公司的财务数字**
    用户 A：company_deep_export_report()         → 导出**B 公司的报告**

本项目对 `conversation_memory` / `ai_assistant._route_cache` 都已做会话隔离，
此处属于漏网。本模块补齐，并沿用同一套"会话键"语义。

## 会话键的解析顺序

1. **ContextVar**：由 Agent 入口 / 工具执行器显式绑定（最准，见 `wrap()`）。
2. **Flask 请求上下文**：`X-Conversation-Id` 请求头 → 回落客户端 IP。
3. **`conversation_memory.active_conversation_id`**：项目既有的会话维度。
4. `'default'`：非请求、非 Agent 场景（脚本 / 自测）。

## 线程安全与容量

`_tool_executor()` 会把工具提交到**独立线程池**执行，contextvars 不会自动跨线程传递，
因此 `agent_tool_runtime._call_tool_with_timeout()` 在**提交线程**读取键，
再用 `wrap()` 在**工作线程**内重新绑定。这是本模块与线程池配合的唯一约定。
"""
from __future__ import annotations

import contextvars
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

# 请求/Agent 运行级会话键（跨线程需显式 wrap）
_KEY = contextvars.ContextVar('cd_session_key', default=None)

# key -> {"state": dict, "name": str, "ts": float}
_STATES: dict = {}
_LOCK = threading.RLock()

# 容量与存活期：本地自用工具，给足 2 小时；条目数封顶防内存无界增长
TTL_S = float(os.environ.get('CD_SESSION_TTL', '7200'))
MAX_KEYS = int(os.environ.get('CD_SESSION_MAX', '32'))

DEFAULT_KEY = 'default'


def resolve_key_from_context() -> str:
    """解析会话键。优先级：

      1. `X-Conversation-Id` 请求头（前端可显式指定，最精确）
      2. 请求体里的 `conversation_id`（**无竞态**：随请求携带，AI 助手与深析路由都有）
      3. `conversation_memory.active_conversation_id`（项目既有会话维度）
         ⚠️ 该值是进程级单例属性，并发请求间存在竞态；仅作兜底，不作首选。
      4. 客户端 IP（多用户经 LAN / 隧道访问时至少做到按设备隔离）
      5. `'default'`
    """
    try:
        from flask import has_request_context, request
        if has_request_context():
            cid = str(request.headers.get('X-Conversation-Id') or '').strip()
            if cid:
                return 'conv:' + cid[:80]
            try:
                body = request.get_json(silent=True) or {}
                bc = str(body.get('conversation_id') or '').strip()
                if bc:
                    return 'conv:' + bc[:80]
            except Exception as e:
                logger.debug("cd_session: 读取请求体 conversation_id 失败: %s", e)
            try:
                from conversation_memory import get_memory
                mc = str(getattr(get_memory(), 'active_conversation_id', '') or '').strip()
                if mc and mc != 'legacy':
                    return 'conv:' + mc[:80]
            except Exception as e:
                logger.debug("cd_session: conversation_memory 不可用: %s", e)
            return 'ip:' + str(request.remote_addr or '?')[:40]
    except Exception as e:
        logger.debug("cd_session: flask 上下文不可用: %s", e)
    try:
        from conversation_memory import get_memory
        cid = getattr(get_memory(), 'active_conversation_id', None)
        if cid:
            return 'mem:' + str(cid)[:80]
    except Exception as e:
        logger.debug("cd_session: conversation_memory 不可用: %s", e)
    return DEFAULT_KEY


def set_key(key: str):
    """绑定当前上下文的会话键。返回 token，供 `reset_key()` 还原。"""
    return _KEY.set(str(key or DEFAULT_KEY))


def reset_key(token):
    try:
        _KEY.reset(token)
    except (ValueError, LookupError) as e:
        logger.debug("cd_session: reset_key 失败（token 跨上下文）: %s", e)


def current_key() -> str:
    """当前会话键：ContextVar 优先，其次请求/项目上下文，最后 'default'。"""
    k = _KEY.get()
    return k or resolve_key_from_context()


def wrap(func, key: str = None):
    """把 `func` 包成"在工作线程内也带同一会话键"的可调用对象。

    线程池（`_tool_executor`）不复制 contextvars，故必须在提交线程取键、
    在工作线程内重新绑定，否则工具会落到 'default' 桶里、重新变成共享状态。
    """
    k = key or current_key()

    def _runner(*args, **kwargs):
        token = _KEY.set(k)
        try:
            return func(*args, **kwargs)
        finally:
            reset_key(token)
    return _runner


def _prune_locked():
    """调用方须持有 `_LOCK`：先清过期，再按容量淘汰最旧。"""
    now = time.time()
    for k in [k for k, v in _STATES.items() if now - float(v.get('ts') or 0) > TTL_S]:
        _STATES.pop(k, None)
    if len(_STATES) > MAX_KEYS:
        for k, _ in sorted(_STATES.items(), key=lambda kv: float(kv[1].get('ts') or 0))[: len(_STATES) - MAX_KEYS]:
            _STATES.pop(k, None)


def set_state(state: dict, name: str = None, key: str = None):
    """写入当前会话的分析状态（仅本会话可见）。"""
    k = key or current_key()
    with _LOCK:
        slot = _STATES.get(k) or {}
        slot['state'] = state or {}
        slot['name'] = name if name is not None else slot.get('name') or ''
        slot['ts'] = time.time()
        _STATES[k] = slot
        _prune_locked()
    return slot['state']


def get_state(key: str = None) -> dict:
    """读取当前会话的分析状态；无则返回 `{}`（**不回落到他人会话**）。"""
    k = key or current_key()
    with _LOCK:
        slot = _STATES.get(k) or {}
        if slot and time.time() - float(slot.get('ts') or 0) > TTL_S:
            _STATES.pop(k, None)
            return {}
        return dict(slot.get('state') or {})


def set_name(name: str, key: str = None) -> str:
    k = key or current_key()
    with _LOCK:
        slot = _STATES.get(k) or {}
        slot['name'] = str(name or '')
        slot['ts'] = time.time()
        _STATES[k] = slot
    return slot['name']


def get_name(key: str = None) -> str:
    k = key or current_key()
    with _LOCK:
        return str((_STATES.get(k) or {}).get('name') or '')


def clear(key: str = None):
    k = key or current_key()
    with _LOCK:
        _STATES.pop(k, None)


def stats() -> dict:
    """诊断用：只返回键的数量与年龄，**不含任何公司数据**。"""
    now = time.time()
    with _LOCK:
        return {
            'keys': len(_STATES),
            'max_keys': MAX_KEYS, 'ttl_s': TTL_S,
            'ages_s': sorted(round(now - float(v.get('ts') or 0), 1) for v in _STATES.values()),
            'current_key': current_key(),
        }
