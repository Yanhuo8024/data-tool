#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_server.py - 生产服务器入口（waitress, 多线程）
由 start.bat 调用，需在项目根目录运行。

说明：app.py 的 __main__ 走 Flask 开发服务器（app.run，带 debug），
生产环境用本脚本以 waitress WSGI 提供稳定服务。
线程数可通过环境变量 WAITRESS_THREADS 配置（默认 8）。

【防自动停止】服务器禁止自动停止：
- waitress 异常崩溃/异常返回 → 自动重启（连续失败超过
  WAITRESS_MAX_RESTARTS 次才放弃，默认 5，防死循环）
- Ctrl+C 优雅退出；异常日志写入 output/server.log 便于排查
- 如需进程级看门狗（进程被强杀后自动拉起），可并行运行
  `python watchdog_server.py`（见该文件说明）
"""
import os
import sys
import time as _t
import logging
import socket
import threading

_t0 = _t.time()

# ---- [可选] import 耗时诊断探针 ----
# 用法: set PROFILE_IMPORTS=1 && python run_server.py
# 输出: top N 重量级 import，用于发现冷启动真正的瓶颈模块。
# 默认关闭，对生产启动零开销。
if os.environ.get('PROFILE_IMPORTS') == '1':
    import importlib
    _import_times = {}
    _orig_import = __builtins__.__import__ if isinstance(__builtins__, dict) else __builtins__.__import__

    def _timed_import(name, globals=None, locals=None, fromlist=(), level=0):
        t0 = _t.perf_counter()
        try:
            mod = _orig_import(name, globals, locals, fromlist, level)
        except Exception:
            raise
        dt = _t.perf_counter() - t0
        # 仅记录顶层包（避免 from xxx.yyy import zzz 时把 xxx.yyy 也算）
        top = name.split('.')[0]
        _import_times[top] = _import_times.get(top, 0.0) + dt
        return mod

    if isinstance(__builtins__, dict):
        __builtins__['__import__'] = _timed_import
    else:
        __builtins__.__import__ = _timed_import
    import atexit
    def _dump_import_profile():
        if not _import_times:
            return
        items = sorted(_import_times.items(), key=lambda x: -x[1])
        print('\n========== Import Time Profile (top 20) ==========', flush=True)
        for name, dt in items[:20]:
            bar = '#' * min(50, int(dt * 10))
            print(f'  {dt:6.2f}s  {name:<32s} {bar}', flush=True)
        print(f'  total tracked: {_t.perf_counter()-_t0:.2f}s', flush=True)
        try:
            os.makedirs('output', exist_ok=True)
            with open('output/import_profile.txt', 'w', encoding='utf-8') as f:
                f.write(f'cold start total: {_t.perf_counter()-_t0:.2f}s\n')
                for name, dt in items:
                    f.write(f'{dt:7.3f}s  {name}\n')
        except Exception:
            pass
    atexit.register(_dump_import_profile)

# 便携版 Python（embeddable + ._pth）会禁用脚本目录自动入 sys.path，
# 这里显式加入项目根目录，保证任意启动方式下都能 import app
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app, HOST, PORT  # noqa: E402  (import 会完成全部路由注册)
from waitress import serve  # noqa: E402


def _port_in_use(host: str, port: int) -> bool:
    """启动前探测：端口已被占用说明已有实例在跑（双实例会导致"改了没生效"的假象）"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(2)
        return s.connect_ex((host if host != '0.0.0.0' else '127.0.0.1', port)) == 0


def _setup_logger():
    logger = logging.getLogger('data.tool.server')
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    try:
        os.makedirs('output', exist_ok=True)
        fh = logging.FileHandler('output/server.log', encoding='utf-8')
        fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
        logger.addHandler(fh)
    except Exception:
        pass
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    logger.addHandler(sh)
    return logger


if __name__ == "__main__":
    if _port_in_use(HOST, PORT):
        print(f"\n  ❌ 端口 {PORT} 已被占用——极可能已有一个 data.tool 实例在运行。")
        print(f"     请直接访问 http://127.0.0.1:{PORT} ，或先停止旧实例（stop.bat / taskkill）再启动。")
        print(f"     （多个实例同时监听会把请求随机分流到新旧代码，造成'改了没生效'）\n")
        sys.exit(1)

    # ---- 线程数自适应（U盘冷启动优化）----
    # 检测当前路径所在驱动器类型：U盘/可移动磁盘自动降到 4 线程（减少磁盘争用）
    # SSD/NVMe 用默认 8 线程。环境变量 WAITRESS_THREADS 可手动覆盖。
    # 用 ctypes 直呼 kernel32!GetDriveTypeW：不再起子进程（比 wmic/PowerShell 都快），
    # 且不受"程序黑名单/沙箱"限制。原先依赖 wmic 有两个隐患：
    #   1) wmic 自 Win11 24H2 起默认移除，探测会静默失效；
    #   2) 若 wmic 被安全策略禁止（如沙箱黑名单），拉起它的父进程会被一并终止 → 服务被杀。
    _default_threads = "8"
    try:
        _root_drive = os.path.splitdrive(os.path.abspath(__file__))[0]  # e.g. "D:"
        if _root_drive and os.name == 'nt':
            import ctypes as _ct
            _drive_letter = _root_drive.rstrip(':').upper()
            # GetDriveTypeW 返回: 0=UNKNOWN 1=NO_ROOT_DIR 2=REMOVABLE
            #                     3=FIXED 4=REMOTE 5=CDROM 6=RAMDISK
            _drv_type = int(_ct.windll.kernel32.GetDriveTypeW(f"{_drive_letter}:\\"))
            if _drv_type == 2:
                _default_threads = "4"
                print(f"  [i] Detected removable drive ({_drive_letter}:) — using 4 threads (less disk contention)")
            elif _drv_type in (3, 4):
                _default_threads = "8"
    except Exception:
        pass  # 检测失败时保持默认 8

    _threads = int(os.environ.get("WAITRESS_THREADS", _default_threads))
    _max_restarts = int(os.environ.get("WAITRESS_MAX_RESTARTS", "5"))
    _log = _setup_logger()
    _restarts = 0

    print(f"\n  data.tool  Web (waitress, {_threads} threads)")
    if HOST in ("0.0.0.0", "::"):
        print(f"    Local:  http://127.0.0.1:{PORT}")
        print(f"    LAN:    http://<本机IP>:{PORT}  (same Wi-Fi - see start.bat for the exact URL)")
    else:
        print(f"    http://{HOST}:{PORT}")
    print(f"    (Task Queue: async tasks enabled)")
    print(f"    (Auto-restart guard: max {_max_restarts} consecutive restarts)")

    while True:
        try:
            _log.info(f"server starting (threads={_threads})")
            # ---- 自动开浏览器：端口就绪后立即调用，去掉了原 start.bat 里 ping -n 3 的 3 秒空转 ----
            if os.environ.get('AUTO_OPEN_BROWSER', '1') == '1':
                def _open_browser_when_ready():
                    import webbrowser
                    url = f'http://127.0.0.1:{PORT}/'
                    # 探活端口（最多等 30 秒，远大于冷启动时长）
                    for _ in range(60):
                        try:
                            with socket.create_connection(('127.0.0.1', PORT), timeout=0.5):
                                webbrowser.open(url)
                                return
                        except OSError:
                            _t.sleep(0.5)
                    _log.warning(f"browser auto-open skipped: port {PORT} not ready in 30s")
                threading.Thread(target=_open_browser_when_ready, daemon=True).start()
            serve(app, host=HOST, port=PORT, threads=_threads)
            # serve 正常返回（极端情况）也视为退出，走自动重启
            _log.warning("waitress serve returned unexpectedly, restarting...")
        except KeyboardInterrupt:
            _log.info("SIGINT received, graceful shutdown.")
            break
        except SystemExit:
            raise
        except Exception as e:
            _log.error(f"waitress crashed: {e}", exc_info=True)

        _restarts += 1
        if _restarts > _max_restarts:
            _log.critical(
                f"server restarted {_restarts - 1} times consecutively, giving up. "
                f"Check output/server.log for the root cause."
            )
            break
        _log.warning(f"restarting in 3s (attempt {_restarts}/{_max_restarts})...")
        print(f"\n  [auto-restart] attempt {_restarts}/{_max_restarts}, "
              f"uptime so far {_t.time() - _t0:.0f}s\n")
        _t.sleep(3)

    print(f"\n  Server stopped. cold start took {_t.time() - _t0:.1f}s")
