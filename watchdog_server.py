#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
watchdog_server.py - data.tool 进程级看门狗（可选，防自动停止的兜底）

用途：run_server.py 只保护"自身异常崩溃自动重启"；若进程被外部强杀
（taskkill/Stop-Process/系统重启等），需要本看门狗把服务重新拉起。

原理：每 WATCHDOG_INTERVAL 秒探测 http://127.0.0.1:PORT/api/version，
连续 WATCHDOG_FAILS 次不通 → 以 subprocess 重新拉起 run_server.py。

使用（二选一）：
  1. 手动后台运行：  python watchdog_server.py
  2. start.bat 末尾追加： start "" /B python watchdog_server.py

停止看门狗：Ctrl+C；它本身不守护自己（如需要可注册为 Windows 计划任务）。
"""
import os
import sys
import time
import socket
import subprocess

HOST = os.environ.get("WATCHDOG_HOST", "127.0.0.1")
PORT = int(os.environ.get("WATCHDOG_PORT", "5000"))
INTERVAL = int(os.environ.get("WATCHDOG_INTERVAL", "10"))   # 探测间隔秒
FAILS = int(os.environ.get("WATCHDOG_FAILS", "3"))          # 连续失败次数阈值
ROOT = os.path.dirname(os.path.abspath(__file__))


def _probe() -> bool:
    """探测端口是否在监听（轻量，避免依赖 requests）"""
    try:
        with socket.create_connection((HOST, PORT), timeout=2):
            return True
    except OSError:
        return False


def _start_server():
    print(f"[watchdog] server down, starting run_server.py ...", flush=True)
    env = dict(os.environ)
    env.pop("EMBED_SCHEDULER", None)  # 主进程默认内嵌调度器
    try:
        subprocess.Popen(
            [sys.executable, "-X", "utf8", os.path.join(ROOT, "run_server.py")],
            cwd=ROOT,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    except Exception as e:
        print(f"[watchdog] failed to start server: {e}", flush=True)


def main():
    print(f"[watchdog] data.tool watchdog started -> {HOST}:{PORT} "
          f"(interval={INTERVAL}s, fails={FAILS})", flush=True)
    fails = 0
    while True:
        try:
            if _probe():
                fails = 0
            else:
                fails += 1
                print(f"[watchdog] probe failed {fails}/{FAILS}", flush=True)
                if fails >= FAILS:
                    _start_server()
                    fails = 0
                    time.sleep(INTERVAL)  # 拉起后先等待，避免反复拉起
        except KeyboardInterrupt:
            print("[watchdog] stopped by user.", flush=True)
            break
        except Exception as e:
            print(f"[watchdog] error: {e}", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
