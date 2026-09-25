#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
monitoring.py - 全链路监控与日志
"""
import os, sys, time, threading, psutil, gc
from datetime import datetime
from typing import Dict, List
from dataclasses import dataclass
from pathlib import Path

LOG_DIR = Path("output/logs")
LOG_DIR.mkdir(exist_ok=True)

ERROR_LOG = LOG_DIR / "errors.log"
ACCESS_LOG = LOG_DIR / "access.log"
MEMORY_LOG = LOG_DIR / "memory.log"
SYSTEM_LOG = LOG_DIR / "system.log"

MEMORY_WARNING = 500
MEMORY_CRITICAL = 1000


@dataclass
class MemorySnapshot:
    timestamp: str
    rss_mb: float
    vms_mb: float
    percent: float

    def to_dict(self):
        return self.__dict__


class Logger:
    _lock = threading.Lock()

    @staticmethod
    def _write(filepath: Path, content: str):
        with Logger._lock:
            with open(filepath, 'a', encoding='utf-8') as f:
                f.write(content + '\n')

    @classmethod
    def error(cls, source: str, error_type: str, message: str, stack: str = "", url: str = "", ua: str = ""):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"[{ts}] [{source}] {error_type}: {message}"
        if stack: entry += f"\nStack: {stack}"
        if url: entry += f"\nURL: {url}"
        cls._write(ERROR_LOG, entry)

    @classmethod
    def info(cls, msg: str):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cls._write(SYSTEM_LOG, f"[{ts}] {msg}")

    @classmethod
    def memory(cls, rss: float, vms: float, pct: float):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cls._write(MEMORY_LOG, f"[{ts}] RSS={rss:.1f}MB VMS={vms:.1f}MB {pct:.1f}%")

    @classmethod
    def access(cls, method: str, path: str, status: int, duration_ms: float):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cls._write(ACCESS_LOG, f"[{ts}] {method} {path} {status} {duration_ms:.1f}ms")


class MemoryMonitor:
    def __init__(self, interval: int = 30):
        self.interval = interval; self.running = False
        self.thread = None; self.peak_rss = 0
        self.history: List[MemorySnapshot] = []
        self._lock = threading.Lock()

    def start(self):
        if self.running: return
        self.running = True; self.thread = threading.Thread(target=self._monitor, daemon=True); self.thread.start()
        Logger.info("Memory monitor started")

    def stop(self): self.running = False

    def _monitor(self):
        while self.running:
            try:
                proc = psutil.Process(os.getpid())
                mem = proc.memory_info()
                rss_mb = mem.rss / 1024 / 1024; vms_mb = mem.vms / 1024 / 1024; pct = proc.memory_percent()
                if rss_mb > self.peak_rss: self.peak_rss = rss_mb
                snap = MemorySnapshot(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), round(rss_mb, 1), round(vms_mb, 1), round(pct, 1))
                with self._lock:
                    self.history.append(snap)
                    if len(self.history) > 100: self.history = self.history[-100:]
                Logger.memory(rss_mb, vms_mb, pct)
                if rss_mb > MEMORY_CRITICAL:
                    Logger.error("backend", "MEMORY_CRITICAL", f"{rss_mb:.1f}MB"); gc.collect()
                elif rss_mb > MEMORY_WARNING:
                    Logger.error("backend", "MEMORY_WARNING", f"{rss_mb:.1f}MB")
            except Exception as e: Logger.error("backend", "MONITOR_ERROR", str(e))
            time.sleep(self.interval)

    def get_status(self) -> Dict:
        proc = psutil.Process(os.getpid()); mem = proc.memory_info()
        return {"rss_mb": round(mem.rss/1024/1024, 1), "vms_mb": round(mem.vms/1024/1024, 1),
                "percent": round(proc.memory_percent(), 1), "peak_rss_mb": round(self.peak_rss, 1)}

    def is_safe(self) -> bool:
        """检查内存是否安全"""
        try:
            rss_mb = psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
            return rss_mb < 900  # 900MB 阈值
        except Exception:
            return True

    def is_warning(self) -> bool:
        """检查是否达到警告阈值"""
        try:
            rss_mb = psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
            return rss_mb > 768  # 768MB 阈值
        except Exception:
            return False

    def force_gc(self):
        """强制垃圾回收"""
        import gc
        gc.collect()

    def get_memory_mb(self) -> float:
        """获取当前内存 (MB)"""
        try:
            return psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
        except Exception:
            return 0.0

    def get_history(self) -> List[Dict]:
        with self._lock: return [s.to_dict() for s in self.history]


class TaskTracker:
    def __init__(self):
        self.tasks: Dict[str, Dict] = {}; self._lock = threading.Lock()

    def start_task(self, task_id: str, task_type: str):
        with self._lock:
            self.tasks[task_id] = {"id": task_id, "type": task_type, "status": "running",
                "start_time": time.time(), "end_time": None, "start_memory": self._get_memory_mb(), "peak_memory": 0, "error": None}
        Logger.info(f"Task started: {task_id} ({task_type})")

    def end_task(self, task_id: str, error: str = None):
        with self._lock:
            if task_id in self.tasks:
                t = self.tasks[task_id]; t["status"] = "failed" if error else "completed"
                t["end_time"] = time.time(); t["error"] = error
                Logger.info(f"Task ended: {task_id} ({t['status']})")

    def get_tasks(self) -> List[Dict]:
        with self._lock:
            result = []
            for t in self.tasks.values():
                d = dict(t); d["duration"] = round((d["end_time"] or time.time()) - d["start_time"], 1)
                d["start_memory_mb"] = d.pop("start_memory", 0); d["peak_memory_mb"] = d.pop("peak_memory", 0)
                result.append(d)
            return sorted(result, key=lambda x: x["start_time"], reverse=True)[:20]

    def _get_memory_mb(self) -> float:
        try: return psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
        except Exception: return 0


memory_monitor = MemoryMonitor(interval=30)
task_tracker = TaskTracker()


def init_monitoring():
    memory_monitor.start()
    Logger.info("Monitoring system initialized")


def get_system_health() -> Dict:
    mem = memory_monitor.get_status()
    cpu = psutil.cpu_percent(interval=0.1); disk = psutil.disk_usage('/')
    return {"status": "healthy" if mem["rss_mb"] < MEMORY_WARNING else "warning",
            "memory": mem, "cpu_percent": cpu,
            "disk": {"total_gb": round(disk.total/1024**3, 1), "used_gb": round(disk.used/1024**3, 1), "percent": disk.percent},
            "python_version": sys.version.split('[')[0].strip(), "pid": os.getpid(),
            "uptime_seconds": round(time.time() - psutil.Process(os.getpid()).create_time(), 0)}


def get_recent_errors(limit: int = 50) -> List[str]:
    if not ERROR_LOG.exists(): return []
    try: return ERROR_LOG.read_text(encoding='utf-8').strip().split('\n')[-limit:]
    except Exception: return []


def get_recent_access(limit: int = 50) -> List[str]:
    if not ACCESS_LOG.exists(): return []
    try: return ACCESS_LOG.read_text(encoding='utf-8').strip().split('\n')[-limit:]
    except Exception: return []
