"""
scheduler_manager.py — 定时任务调度器（基于 APScheduler）

任务动作：定时调用 data.tool 内部接口（GET/POST），支持携带用户自己的 API Key。
任务定义与执行历史均持久化到 SQLite。
"""
import json
import threading
import time
import requests

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

_scheduler = None
_started = False
_app = None
_lock = threading.Lock()


def init_scheduler(app):
    """应用启动时调用：加载数据库中的启用任务并启动调度器"""
    global _scheduler, _started, _app
    _app = app
    if _started:
        return
    _scheduler = BackgroundScheduler(timezone='Asia/Shanghai')
    _load_jobs_from_db()
    _scheduler.start()
    _started = True
    print('[Scheduler] 定时任务调度器已启动')


def _load_jobs_from_db():
    from models import db, ScheduledTask
    with _app.app_context():
        tasks = ScheduledTask.query.filter_by(is_active=True).all()
        for t in tasks:
            _schedule_job(t)


def _job_id(task_id):
    return f"task_{task_id}"


def _schedule_job(task):
    """为任务注册 APScheduler job"""
    try:
        trigger = CronTrigger.from_crontab(task.cron)
    except Exception as e:
        print(f"[Scheduler] 任务 {task.name} cron 无效: {e}")
        return
    _scheduler.add_job(
        _run_in_context,
        trigger=trigger,
        id=_job_id(task.id),
        args=[task.id],
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=300,
    )


def _run_in_context(task_id):
    """APScheduler 后台线程 → 包装 app context 执行"""
    with _app.app_context():
        _execute_task(task_id, trigger='schedule')


def _execute_task(task_id, trigger='schedule'):
    """执行一次任务，更新任务状态并写入执行历史"""
    from models import db, ScheduledTask, ScheduledTaskRun, ApiKey
    task = db.session.get(ScheduledTask, task_id)
    if not task or not task.is_active:
        return

    # ---- 依赖管理：上游任务未成功则跳过（轻量 DAG） ----
    if task.depends_on:
        upstream = db.session.get(ScheduledTask, task.depends_on)
        if upstream is not None:
            if upstream.last_status != 'ok':
                task.last_status = 'skipped'
                task.last_output = f"上游任务「{upstream.name}」状态为 {upstream.last_status or '未执行'}，本次跳过"
                db.session.commit()
                run = ScheduledTaskRun(
                    task_id=task.id, user_id=task.user_id, status='skipped',
                    http_code=None, output=task.last_output, error='',
                    duration_ms=0, trigger=trigger,
                )
                db.session.add(run)
                db.session.commit()
                return
        else:
            task.depends_on = ''
            db.session.commit()

    task.last_status = 'running'
    db.session.commit()

    start = time.time()
    status, http_code, output, error = 'error', None, '', ''
    try:
        port = _app.config.get('PORT', 5000)
        # 端口从环境变量兜底
        import os
        port = int(os.environ.get('PORT', port))
        url = f"http://127.0.0.1:{port}{task.target}"

        headers = {}
        if task.use_api_key:
            key = ApiKey.query.filter_by(user_id=task.user_id, is_active=True).first()
            if key:
                headers['X-API-Key'] = key.key

        if task.method.upper() == 'POST' and task.body:
            try:
                payload = json.loads(task.body)
            except Exception:
                payload = task.body
            resp = requests.post(url, headers=headers, json=payload, timeout=60)
        else:
            resp = requests.get(url, headers=headers, timeout=60)

        http_code = resp.status_code
        output = resp.text[:800]
        if resp.status_code < 400:
            status = 'ok'
        else:
            error = f"HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as e:
        error = str(e)[:500]

    duration_ms = int((time.time() - start) * 1000)

    from datetime import datetime, timezone
    task.last_run_at = datetime.now(timezone.utc)
    task.last_status = status
    task.last_output = (output or error)[:500]
    db.session.commit()

    run = ScheduledTaskRun(
        task_id=task.id,
        user_id=task.user_id,
        status=status,
        http_code=http_code,
        output=output,
        error=error,
        duration_ms=duration_ms,
        trigger=trigger,
    )
    db.session.add(run)
    db.session.commit()


def sync_task(task):
    """任务增/改/删后调用：保持 APScheduler 与数据库一致"""
    with _lock:
        if not _started:
            return
        job_id = _job_id(task.id)
        if _scheduler.get_job(job_id):
            _scheduler.remove_job(job_id)
        if task.is_active:
            _schedule_job(task)


def delete_task_job(task_id):
    """删除任务对应的调度 job"""
    with _lock:
        if _started and _scheduler.get_job(_job_id(task_id)):
            _scheduler.remove_job(_job_id(task_id))


def trigger_now(task_id):
    """手动触发一次（在请求线程同步执行，便于返回结果）"""
    with _app.app_context():
        _execute_task(task_id, trigger='manual')


def get_scheduler_status():
    """调度器状态（供监控/调试）"""
    jobs = _scheduler.get_jobs() if _scheduler else []
    return {
        'started': _started,
        'job_count': len(jobs),
        'jobs': [{'id': j.id, 'next_run': str(j.next_run_time)} for j in jobs],
    }
