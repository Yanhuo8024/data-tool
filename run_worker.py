#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_worker.py - 独立后台 Worker 进程（服务拆分）
职责：定时任务调度（APScheduler）+ 后台任务队列清理，与主 Web 服务进程隔离，
      单点故障互不影响。可选启动（主进程 EMBED_SCHEDULER=0 时由本进程接管调度）。
"""
import os
import time

from flask import Flask

from database import init_db


def main():
    app = Flask(__name__)
    app.config['PORT'] = int(os.environ.get('PORT', '5000'))
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY') or 'worker-secret'

    # 初始化数据库（含建表/默认管理员）
    init_db(app)

    # 启动定时任务调度器（独立进程内嵌）
    from scheduler_manager import init_scheduler
    init_scheduler(app)

    from task_queue import task_queue
    print(f"[Worker] 独立 worker 已启动 (PID {os.getpid()})")
    print(f"[Worker] 定时任务调度器运行中；每 300s 清理过期任务")

    try:
        while True:
            time.sleep(300)
            try:
                task_queue.cleanup_expired(max_age=3600)
            except Exception:
                pass
    except KeyboardInterrupt:
        print("[Worker] 已停止")


if __name__ == '__main__':
    main()
