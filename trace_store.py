#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
trace_store.py - Agent 结构化追踪（P0）
=====================================================
目标：
1. 把每个 Agent 运行的调用轨迹落盘为 JSONL（事件时间线），供前端时间线化展示。
2. 支持坏 Case 回放：list / get 单个运行，恢复完整事件流。
3. 预留 OpenTelemetry 兼容槽位：设置 TRACE_OTEL_ENDPOINT（或 TRACE_EXPORTER=otel）
   且安装了 opentelemetry 时，会把 span 导出到远端；否则仅本地 JSONL 落地（默认、零依赖）。

数据文件：<OUT_DIR>/traces/<run_id>.jsonl
每行一个事件：{"ts":..., "run_id":..., "seq":..., "type":..., "data":{...}}
"""
import os
import json
import time
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, List

from trace_otel import emit_span, OTEL_ENABLED, OTEL_ENDPOINT  # re-export：实际判定在 trace_otel 内

logger = logging.getLogger(__name__)

# 运行追踪目录覆盖（测试可 monkeypatch 本属性）。
# 注意：不要只依赖 import 时捕获的值——conftest/外部脚本可能在 import 之后才设置
# 环境变量 TRACE_DIR；解析时必须回落到实时 os.environ，否则测试轨迹会写进生产目录。
TRACE_DIR = os.environ.get("TRACE_DIR", "")


def _resolve_trace_root() -> str:
    """解析追踪根目录覆盖值。

    优先级：
      1. 模块属性 TRACE_DIR（非空，测试 monkeypatch / import 时已设）
      2. 实时环境变量 TRACE_DIR（覆盖 import 早于 set 的顺序问题）
      3. 空串 → 调用方回落 OUT_DIR
    """
    if TRACE_DIR:
        return TRACE_DIR
    return (os.environ.get("TRACE_DIR") or "").strip()


def _base_dir() -> Path:
    """决定追踪根目录：优先 TRACE_DIR（模块属性或实时环境变量），其次后端 OUT_DIR。"""
    override = _resolve_trace_root()
    if override:
        p = Path(override)
        p.mkdir(parents=True, exist_ok=True)
        return p
    try:
        from core_config import OUT_DIR  # 避免启动时循环导入
        base = OUT_DIR
    except Exception:
        base = Path(__file__).parent / "output"
    return base


def _trace_dir() -> Path:
    d = _base_dir() / "traces"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _new_run_id(session_id: str = None) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    rand = os.urandom(3).hex()
    slug = ""
    if session_id:
        import re
        slug = re.sub(r"[^\w\-]", "", str(session_id))[-16:] + "-"
    return f"{stamp}-{slug}{rand}"


# ---------- 写入锁（多会话并发安全） ----------
_locks: Dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()

# P2-1：进程内 seq 计数器。旧实现每写一条事件都「全量读+逐行 json.loads」整个 JSONL
# 取最大 seq，单 run N 条事件即 O(N²) 次解析。改为内存计数：仅首次（或进程重启后继续
# 写同一 run）读一次文件对齐，之后 O(1) 递增。
_seq_cache: Dict[str, int] = {}
_seq_guard = threading.Lock()


def _next_seq(path: Path, run_id: str) -> int:
    """返回该 run 的下一个 seq（进程内递增；首次从文件对齐一次）。"""
    with _seq_guard:
        cur = _seq_cache.get(run_id)
        if cur is None:
            cur = 0
            try:
                if path.exists():
                    for line in path.read_text(encoding="utf-8").splitlines():
                        try:
                            cur = max(cur, int(json.loads(line).get("seq", 0)))
                        except Exception:
                            continue
            except Exception:
                cur = 0
        cur += 1
        _seq_cache[run_id] = cur
        return cur


def _forget_seq(run_id: str) -> None:
    with _seq_guard:
        _seq_cache.pop(run_id, None)


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _locks_guard:
        if key not in _locks:
            _locks[key] = threading.Lock()
        return _locks[key]


class TraceError(Exception):
    pass


# ------------------------------------------------------------------
# TraceStore：JSONL 落盘 + 查询 + 回放
# ------------------------------------------------------------------
def begin_run(session_id: Optional[str] = None, user_message: str = "",
              llm_config: Dict[str, Any] = None, skill: str = None) -> Dict[str, Any]:
    """开始一次运行，返回 run 元信息（含 run_id / 文件路径）。"""
    run_id = _new_run_id(session_id)
    cfg = llm_config or {}
    record = {
        "run_id": run_id,
        "session_id": session_id,
        "started_at": datetime.now().isoformat(timespec="milliseconds"),
        "ts0": time.time(),
        "user_message": str(user_message)[:4000],
        "provider": cfg.get("provider", ""),
        "model": cfg.get("model", ""),
        "skill": skill,
        "events": 0,
        "status": "running",
    }
    event(run_id, "run_start",
          {"user_message": record["user_message"],
           "provider": record["provider"], "model": record["model"], "skill": skill})
    # P2-2：按小时节流地清理过期/超量轨迹（失败静默，绝不影响主流程）
    try:
        _maybe_prune()
    except Exception:
        pass
    return record


def event(run_id: str, etype: str, data: Dict[str, Any] = None) -> int:
    """追加一条事件到 run 的 JSONL。返回序列号 seq（从 1 递增）。"""
    if not _valid_run_id(run_id):
        return 0
    path = _trace_dir() / f"{run_id}.jsonl"
    seq = 0
    try:
        # P2-1：进程内计数器递增（首次或进程重启后读一次文件对齐），避免 O(n²) 全量解析
        seq = _next_seq(path, run_id)
        rec = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "ts_mono": round((time.time() - time.monotonic()) + time.monotonic(), 3),
            "run_id": run_id,
            "seq": seq,
            "type": etype,
            "data": data or {},
        }
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with _lock_for(path):
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        if data:
            emit_span(run_id, etype, {"attrs": data or {}}, seq=seq)
        else:
            emit_span(run_id, etype, {}, seq=seq)
    except (Exception, SystemExit) as e:
        # SystemExit 亦须捕获：环境删除/写入守卫会以 SystemExit 中断文件操作。
        # trace 属旁路可观测性，任何写入失败都不得中断或吞掉 agent 主流程结果。
        logger.debug("trace 事件写入失败 %s: %s", etype, e)
    return seq


def end_run(run_id: str, outcome: str = "ok", summary: Dict[str, Any] = None) -> None:
    """结束一次运行，写 run_end 事件（含错误数/总token/耗时摘要）。"""
    event(run_id, "run_end",
          {"outcome": outcome,
           "ok": outcome == "ok",
           "summary": summary or {}})
    _forget_seq(run_id)
    # 尝试清理空文件（无任何事件时删除）
    path = _trace_dir() / f"{run_id}.jsonl"
    try:
        if path.exists() and path.stat().st_size == 0:
            path.unlink()
    except (Exception, SystemExit):
        pass


# ---------- P2-2：轨迹保留策略（避免 traces 目录无限增长） ----------
_PRUNE_KEEP_MAX = int(os.environ.get("TRACE_KEEP_MAX", "500"))
_PRUNE_KEEP_DAYS = float(os.environ.get("TRACE_KEEP_DAYS", "30"))
_prune_last_ts = 0.0
_prune_guard = threading.Lock()


def prune_runs(keep_max: int = None, keep_days: float = None) -> int:
    """清理轨迹：保留最近 keep_max 个 run，且删除超过 keep_days 天的旧文件。返回删除数。"""
    km = _PRUNE_KEEP_MAX if keep_max is None else int(keep_max)
    kd = _PRUNE_KEEP_DAYS if keep_days is None else float(keep_days)
    if km <= 0 and kd <= 0:
        return 0
    d = _trace_dir()
    removed = 0
    try:
        files = sorted(d.glob("*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True)
        cutoff = time.time() - kd * 86400 if kd > 0 else None
        for idx, f in enumerate(files):
            try:
                st = f.stat()
            except Exception:
                continue
            stale = (cutoff is not None and st.st_mtime < cutoff)
            overflow = (km > 0 and idx >= km)
            if stale or overflow:
                try:
                    f.unlink()
                    _forget_seq(f.stem)
                    removed += 1
                except (Exception, SystemExit):
                    pass
    except Exception as e:
        logger.debug("轨迹清理跳过: %s", e)
    return removed


def _maybe_prune() -> None:
    """按小时节流地触发一次清理（在 begin_run 时调用，失败静默）。

    2026-09-18：改为后台线程执行——此前同步 prune 实测首调 ~3.5s，
    会拖慢用户第一次 Agent 请求（被误判为管线慢）。
    """
    global _prune_last_ts
    now = time.time()
    if now - _prune_last_ts < 3600:
        return
    with _prune_guard:
        if now - _prune_last_ts < 3600:
            return
        _prune_last_ts = now

    def _bg():
        try:
            prune_runs()
        except (Exception, SystemExit):
            pass

    try:
        threading.Thread(target=_bg, daemon=True, name='trace-prune').start()
    except Exception:
        try:
            prune_runs()
        except (Exception, SystemExit):
            pass


_RUN_ID_RE = None  # 惰性编译


def _valid_run_id(run_id: str) -> bool:
    """run_id 白名单校验（P2-7）：仅允许字母/数字/下划线/连字符，
    防止拼接路径穿越（如 `../../x`）。"""
    global _RUN_ID_RE
    if _RUN_ID_RE is None:
        import re as _re
        _RUN_ID_RE = _re.compile(r'^[A-Za-z0-9_-]{1,128}$')
    return bool(run_id) and bool(_RUN_ID_RE.match(str(run_id)))


def read_run(run_id: str) -> Optional[Dict[str, Any]]:
    """读取一个 run 的完整事件流。返回 {run_id, events:[...], count} 或 None。"""
    if not _valid_run_id(run_id):
        return None
    path = _trace_dir() / f"{run_id}.jsonl"
    if not path.exists():
        return None
    events = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        logger.debug("trace 读取失败: %s", run_id)
        return None
    return {"run_id": run_id, "events": events, "count": len(events),
            "file": str(path)}


def trace_file(run_id: str) -> str:
    """返回 run 的 JSONL 轨迹文件路径（不存在时不创建）。"""
    if not _valid_run_id(run_id):
        return ""
    path = _trace_dir() / f"{run_id}.jsonl"
    return str(path)


def list_runs(limit: int = 30) -> List[Dict[str, Any]]:
    """列出最近的 run（按运行开始时间倒序）。返回精简元信息。"""
    d = _trace_dir()
    items = []
    try:
        for f in sorted(d.glob("*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True)[:limit]:
            meta = {"run_id": f.stem, "file": str(f), "mtime": f.stat().st_mtime}
            # 读取首行 run_start 与末行 run_end，做轻量索引
            lines = []
            try:
                lines = f.read_text(encoding="utf-8").splitlines()[:50]
            except Exception:
                pass
            for line in lines:
                try:
                    rec = json.loads(line)
                    if rec.get("type") == "run_start":
                        meta["user_message"] = (rec.get("data") or {}).get("user_message", "")[:80]
                        meta["provider"] = (rec.get("data") or {}).get("provider", "")
                        meta["model"] = (rec.get("data") or {}).get("model", "")
                    elif rec.get("type") == "run_end":
                        meta["ok"] = (rec.get("data") or {}).get("ok")
                except Exception:
                    continue
            items.append(meta)
    except Exception as e:
        logger.debug("trace 列表读取失败: %s", e)
    return items


def delete_run(run_id: str) -> bool:
    """删除指定 run 的轨迹文件。"""
    if not _valid_run_id(run_id):
        return False
    path = _trace_dir() / f"{run_id}.jsonl"
    try:
        if path.exists():
            path.unlink()
            return True
    except Exception:
        pass
    return False