"""
routes/scheduler_routes.py — 定时任务管理路由
"""
from flask import Blueprint, request, jsonify, g
from models import db, ScheduledTask, ScheduledTaskRun
from auth import require_auth_or_api_key_local, get_current_user
import datetime

scheduler_bp = Blueprint('scheduler', __name__)


def _task_dict(t):
    return {
        'id': t.id,
        'name': t.name,
        'description': t.description,
        'cron': t.cron,
        'target': t.target,
        'method': t.method,
        'body': t.body,
        'use_api_key': t.use_api_key,
        'is_active': t.is_active,
        'depends_on': t.depends_on or '',
        'last_run_at': t.last_run_at.isoformat() if t.last_run_at else None,
        'last_status': t.last_status,
        'last_output': t.last_output,
        'created_at': t.created_at.isoformat() if t.created_at else None,
    }


def _cron_preset_to_expr(preset, clock):
    """把前端预设（daily/hourly/interval/once）+ 时间字符串转成 5 段 cron"""
    if preset == 'daily':
        hh, mm = (clock.split(':') + ['0', '0'])[:2]
        return f"{int(mm)} {int(hh)} * * *"
    if preset == 'hourly':
        mm = (clock.split(':') + ['0'])[0]
        return f"{int(mm) % 60} * * * *"
    if preset == 'interval_min':
        return f"*/{int(clock) if str(clock).isdigit() else 30} * * * *"
    # 自定义 cron 直接使用
    return clock


# ---- 列表 ----
@scheduler_bp.route('/scheduler/tasks', methods=['GET'])
@require_auth_or_api_key_local
def list_tasks():
    user = get_current_user()
    tasks = ScheduledTask.query.filter_by(user_id=user.id).order_by(
        ScheduledTask.created_at.desc()).all()
    return jsonify({'ok': True, 'tasks': [_task_dict(t) for t in tasks]})


# ---- 创建 ----
@scheduler_bp.route('/scheduler/tasks', methods=['POST'])
@require_auth_or_api_key_local
def create_task():
    user = get_current_user()
    data = request.get_json() or {}

    name = (data.get('name') or '').strip()
    target = (data.get('target') or '').strip()
    if not name or not target:
        return jsonify({'error': '任务名称和目标接口不能为空'}), 400
    if not target.startswith('/api/'):
        return jsonify({'error': '目标必须是内部接口路径，如 /api/dashboard/index-quote'}), 400

    preset = data.get('preset', 'daily')
    clock = data.get('clock', '08:30')
    cron = _cron_preset_to_expr(preset, clock)

    task = ScheduledTask(
        user_id=user.id,
        name=name,
        description=data.get('description', ''),
        cron=cron,
        target=target,
        method=data.get('method', 'GET'),
        body=data.get('body') or '',
        use_api_key=True if data.get('use_api_key', True) else False,
        is_active=True if data.get('is_active', True) else False,
        depends_on=(data.get('depends_on') or '').strip(),
    )
    db.session.add(task)
    db.session.commit()

    from scheduler_manager import sync_task
    sync_task(task)

    return jsonify({'ok': True, 'task': _task_dict(task)}), 201


# ---- 更新（启停/改 cron） ----
@scheduler_bp.route('/scheduler/tasks/<task_id>', methods=['PUT'])
@require_auth_or_api_key_local
def update_task(task_id):
    user = get_current_user()
    task = ScheduledTask.query.filter_by(id=task_id, user_id=user.id).first()
    if not task:
        return jsonify({'error': '任务不存在'}), 404

    data = request.get_json() or {}
    if 'name' in data and data['name'].strip():
        task.name = data['name'].strip()
    if 'description' in data:
        task.description = data.get('description', '')
    if 'target' in data and data['target'].strip():
        task.target = data['target'].strip()
    if 'method' in data:
        task.method = data.get('method', 'GET')
    if 'body' in data:
        task.body = data.get('body') or ''
    if 'use_api_key' in data:
        task.use_api_key = bool(data.get('use_api_key', True))
    if 'is_active' in data:
        task.is_active = bool(data.get('is_active', True))
    if 'depends_on' in data:
        task.depends_on = (data.get('depends_on') or '').strip()
    if 'cron' in data and data.get('cron'):
        task.cron = data['cron']
    db.session.commit()

    from scheduler_manager import sync_task
    sync_task(task)

    return jsonify({'ok': True, 'task': _task_dict(task)})


# ---- 删除 ----
@scheduler_bp.route('/scheduler/tasks/<task_id>', methods=['DELETE'])
@require_auth_or_api_key_local
def delete_task(task_id):
    user = get_current_user()
    task = ScheduledTask.query.filter_by(id=task_id, user_id=user.id).first()
    if not task:
        return jsonify({'error': '任务不存在'}), 404

    ScheduledTaskRun.query.filter_by(task_id=task_id).delete()
    db.session.delete(task)
    db.session.commit()

    from scheduler_manager import delete_task_job
    delete_task_job(task_id)

    return jsonify({'ok': True})


# ---- 手动触发 ----
@scheduler_bp.route('/scheduler/tasks/<task_id>/trigger', methods=['POST'])
@require_auth_or_api_key_local
def trigger_task(task_id):
    user = get_current_user()
    task = ScheduledTask.query.filter_by(id=task_id, user_id=user.id).first()
    if not task:
        return jsonify({'error': '任务不存在'}), 404

    from scheduler_manager import trigger_now
    try:
        trigger_now(task.id)
        task = ScheduledTask.query.get(task.id)
        return jsonify({'ok': True, 'task': _task_dict(task)})
    except Exception as e:
        return jsonify({'error': f'触发失败: {str(e)}'}), 500


# ---- 执行历史 ----
@scheduler_bp.route('/scheduler/runs', methods=['GET'])
@require_auth_or_api_key_local
def list_runs():
    user = get_current_user()
    task_id = request.args.get('task_id')
    q = ScheduledTaskRun.query.filter_by(user_id=user.id)
    if task_id:
        q = q.filter_by(task_id=task_id)
    runs = q.order_by(ScheduledTaskRun.started_at.desc()).limit(100).all()
    return jsonify({'ok': True, 'runs': [{
        'id': r.id,
        'task_id': r.task_id,
        'status': r.status,
        'http_code': r.http_code,
        'output': r.output,
        'error': r.error,
        'duration_ms': r.duration_ms,
        'trigger': r.trigger,
        'started_at': r.started_at.isoformat() if r.started_at else None,
    } for r in runs]})


# ---- 调度器状态 ----
@scheduler_bp.route('/scheduler/status', methods=['GET'])
@require_auth_or_api_key_local
def scheduler_status():
    from scheduler_manager import get_scheduler_status
    return jsonify({'ok': True, 'scheduler': get_scheduler_status()})
