# -*- coding: utf-8 -*-
"""agent_tool_runtime.py — 工具缓存 / 超时 / 进度（从 ai_agent 拆出）"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from typing import Dict

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


def bind_session(func):
    """把工具函数包成"在工作线程内带同一会话键"（contextvars 不跨线程）。

    企业深析的 `_CD_LAST_STATE` 已改为会话级容器（`company_deep_session`），
    其键存在 ContextVar 里；线程池不会复制 contextvars，故**必须**在提交线程取键、
    在工作线程内重新绑定。会话模块不可用时原样返回，保持旧行为。
    """
    try:
        from company_deep_session import current_key, wrap
        return wrap(func, current_key())
    except Exception:
        return func


def submit_bound(func, params: dict):
    """把（已绑定会话键的）工具提交到工具池，返回 Future；池不可用返回 None。

    与 `_call_tool_with_timeout` 共用同一个 `_tool_executor()`，
    供 LangGraph 的并行工具节点使用（该路径不经超时包装）。
    """
    try:
        return _tool_executor().submit(bind_session(func), **params)
    except Exception:
        return None


def _call_tool_with_timeout(func, params: dict, timeout: float = None):
    """在受控线程池中执行工具，超时抛 ToolTimeoutError。

    timeout<=0 表示不启用超时（直接同步调用，保留旧行为）。

    ⚠️ 线程池**不复制 contextvars**：企业深析的会话键（`company_deep_session._KEY`）
    必须在**提交线程**读取，再用 `wrap()` 在**工作线程**内重新绑定，
    否则所有分析状态都会落进 'default' 桶 → 又变回跨会话共享。
    """
    _to = TOOL_TIMEOUT_S if timeout is None else float(timeout)
    if _to <= 0:
        return func(**params)
    import concurrent.futures as _cf
    try:
        from company_deep_session import current_key, wrap
        _target = wrap(func, current_key())
    except Exception:                       # 会话模块不可用 → 退回原行为
        _target = func
    try:
        fut = _tool_executor().submit(_target, **params)
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

