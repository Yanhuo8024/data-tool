#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
task_queue.py - 异步任务队列
"""
import threading, queue, uuid, time, traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Any, Optional, Dict
from flask import Response


class TaskStatus:
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class TaskInfo:
    task_id: str
    name: str
    status: str = TaskStatus.PENDING
    result: Any = None
    error: str = None
    progress: float = 0.0
    progress_msg: str = ""
    elapsed: float = field(default_factory=time.time)

    def to_dict(self):
        return {"task_id": self.task_id, "name": self.name, "status": self.status,
                "result": self.result if self.status == TaskStatus.COMPLETED else None,
                "error": self.error, "progress": self.progress, "progress_msg": self.progress_msg}


class TaskQueue:
    """全局异步任务队列单例"""
    _instance = None

    def __new__(cls, max_workers=4):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, max_workers=4):
        if hasattr(self, "_ts"):
            return
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="taskq")
        self._tasks: Dict[str, TaskInfo] = {}
        self._streams: Dict[str, queue.Queue] = {}
        self._lock = threading.Lock()
        self._ts = time.time()

    def submit(self, name, fn, *args, timeout=None, **kwargs):
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        info = TaskInfo(task_id=task_id, name=name)

        with self._lock:
            self._tasks[task_id] = info
            self._streams[task_id] = queue.Queue()

        def _progress(pct, msg=""):
            info.progress = min(100, max(0, pct))
            if msg:
                info.progress_msg = msg
            try: self._streams[task_id].put({"type": "progress", "info": info.to_dict()})
            except Exception: pass

        def _log(msg):
            try: self._streams[task_id].put({"type": "log", "time": time.strftime("%H:%M:%S"), "msg": msg})
            except Exception: pass

        try:
            import inspect
            sig = inspect.signature(fn)
            if "progress" in sig.parameters: kwargs["progress"] = _progress
            if "task_log" in sig.parameters: kwargs["task_log"] = _log
        except Exception: pass

        def _run():
            info.status = TaskStatus.RUNNING
            try:
                info.result = fn(*args, **kwargs)
                info.status = TaskStatus.COMPLETED
                info.progress = 100
                info.progress_msg = "Done"
            except Exception as e:
                info.error = str(e)
                info.status = TaskStatus.FAILED
                _log(f"Failed: {e}")
            finally:
                info._finish_time = time.time()
                try: self._streams[task_id].put({"type": "done" if info.status == TaskStatus.COMPLETED else "error", "info": info.to_dict()})
                except Exception: pass

        self._pool.submit(_run)

        if timeout and timeout > 0:
            def _todo():
                time.sleep(timeout)
                with self._lock:
                    if info.status == TaskStatus.RUNNING:
                        info.status = TaskStatus.FAILED
                        info.error = f"Timeout ({timeout}s)"
                        info.progress = 0
                        info.progress_msg = "Timeout"
                        try: self._streams[task_id].put({"type": "error", "info": info.to_dict()})
                        except Exception: pass
            threading.Thread(target=_todo, daemon=True).start()

        return task_id

    def get_stream(self, task_id):
        return self._streams.get(task_id)

    def get_task(self, task_id):
        return self._tasks.get(task_id)

    def cancel(self, task_id):
        """取消任务：未开始的直接标记失败；运行中的标记取消信号（线程协作退出）"""
        with self._lock:
            info = self._tasks.get(task_id)
            if not info:
                return False
            if info.status == TaskStatus.PENDING:
                info.status = TaskStatus.FAILED
                info.error = "Cancelled before start"
                info._finish_time = time.time()
            elif info.status == TaskStatus.RUNNING:
                # 协作式取消：置标志，任务函数可检查；此处先标记状态
                info.status = TaskStatus.FAILED
                info.error = "Cancelled (running task may finish in background)"
                info._finish_time = time.time()
            else:
                return False
        try:
            self._streams[task_id].put({"type": "error", "info": info.to_dict()})
        except Exception:
            pass
        return True

    def list_tasks(self, limit: int = 20):
        """列出最近的任务（按创建时间倒序）。/api/task/list 依赖此方法"""
        try:
            limit = max(1, int(limit))
        except (TypeError, ValueError):
            limit = 20
        with self._lock:
            tasks = list(self._tasks.values())
        # elapsed 字段实际记录创建时刻，最新创建的排在最前
        tasks.sort(key=lambda t: getattr(t, "elapsed", 0) or 0, reverse=True)
        return [t.to_dict() for t in tasks[:limit]]

    def stream_events(self, task_id):
        import json as _json
        q = self._streams.get(task_id)
        if not q:
            yield f"data: {_json.dumps({'type': 'error', 'msg': f'task {task_id} not found'})}\n\n"
            return
        while True:
            try:
                ev = q.get(timeout=20)
                yield f"data: {_json.dumps(ev, ensure_ascii=False, default=str)}\n\n"
                if ev.get("type") in ("done", "error"):
                    break
            except queue.Empty:
                yield f"data: {_json.dumps({'type': 'ping'})}\n\n"

    def sse_response(self, task_id):
        return Response(self.stream_events(task_id), mimetype="text/event-stream",
                       headers={"Cache-Control": "no-cache"})

    def cleanup_expired(self, max_age=3600):
        now = time.time()
        expired = []
        for tid, t in self._tasks.items():
            if t.status not in ("completed", "failed"):
                continue
            finish = getattr(t, "_finish_time", None) or getattr(t, "elapsed", 0) or 0
            if (now - finish) > max_age:
                expired.append(tid)
        for tid in expired:
            del self._tasks[tid]
            if tid in self._streams:
                del self._streams[tid]


task_queue = TaskQueue(max_workers=4)
