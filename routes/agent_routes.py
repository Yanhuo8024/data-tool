"""
routes/agent_routes.py — 多 Agent 协同任务路由
"""
from flask import Blueprint, request, jsonify, g
from models import db, AgentCallLog, ToolCallLog
from auth import require_auth_or_api_key_local, get_current_user

agent_bp = Blueprint('agent', __name__)

# 内存中的任务列表（生产环境应使用数据库或 Redis）
_tasks = {}


@agent_bp.route('/api/agent/tasks', methods=['GET'])
@require_auth_or_api_key_local
def list_tasks():
    """列出用户的所有 Agent 任务（本地匿名回退 admin，远程需认证——与全站本地优先契约一致）"""
    user = get_current_user()
    tasks = AgentCallLog.query.filter_by(user_id=user.id).order_by(
        AgentCallLog.started_at.desc()
    ).limit(20).all()
    
    return jsonify({
        'ok': True,
        'tasks': [{
            'id': t.id,
            'description': t.task_description,
            'status': t.status,
            'steps': len(t.tool_calls.all()) if hasattr(t, 'tool_calls') else 0,
            'duration_ms': t.duration_ms,
            'total_tokens': t.total_tokens,
            'started_at': t.started_at.isoformat() if t.started_at else None,
        } for t in tasks]
    })


@agent_bp.route('/api/agent/tasks', methods=['POST'])
@require_auth_or_api_key_local
def create_task():
    """创建新的 Agent 任务"""
    user = get_current_user()
    data = request.get_json()
    if not data:
        return jsonify({'error': '无效的请求数据'}), 400
    
    task_desc = data.get('description', '').strip()
    if not task_desc:
        return jsonify({'error': '任务描述不能为空'}), 400
    
    # 创建任务记录
    task = AgentCallLog(
        user_id=user.id,
        task_description=task_desc,
        session_id=data.get('session_id', ''),
        status='pending'
    )
    db.session.add(task)
    db.session.commit()
    
    # 启动异步任务处理
    import threading
    thread = threading.Thread(
        target=_process_task,
        args=(task.id, data.get('file'), user.id)
    )
    thread.daemon = True
    thread.start()
    
    return jsonify({'ok': True, 'task': {'id': task.id, 'status': 'pending'}})


@agent_bp.route('/api/agent/tasks/<task_id>', methods=['GET'])
@require_auth_or_api_key_local
def get_task(task_id):
    """获取任务详情和调用链"""
    user = get_current_user()
    task = AgentCallLog.query.filter_by(id=task_id, user_id=user.id).first()
    if not task:
        return jsonify({'error': '任务不存在'}), 404
    
    steps = []
    for tc in task.tool_calls.all():
        steps.append({
            'id': tc.id,
            'name': tc.tool_name,
            'description': tc.params_json,
            'tool_name': tc.tool_name,
            'status': tc.status,
            'duration_ms': tc.duration_ms,
        })
    
    return jsonify({
        'ok': True,
        'task': {
            'id': task.id,
            'description': task.task_description,
            'status': task.status,
            'steps': steps,
            'duration_ms': task.duration_ms,
            'total_tokens': task.total_tokens,
            'error': task.error_message,
        }
    })


@agent_bp.route('/api/agent/tasks/<task_id>', methods=['DELETE'])
@require_auth_or_api_key_local
def delete_task(task_id):
    """删除任务"""
    user = get_current_user()
    task = AgentCallLog.query.filter_by(id=task_id, user_id=user.id).first()
    if not task:
        return jsonify({'error': '任务不存在'}), 404
    
    # 删除关联的工具调用记录
    ToolCallLog.query.filter_by(agent_call_id=task_id).delete()
    db.session.delete(task)
    db.session.commit()
    
    return jsonify({'ok': True})


def _process_task(task_id, file_name, user_id):
    """异步处理任务"""
    try:
        # 导入并执行任务
        from ai_agent import agent_loop
        import time
        
        start_time = time.time()
        
        # 更新状态为 running
        task = AgentCallLog.query.get(task_id)
        if task:
            task.status = 'running'
            db.session.commit()
        
        # 执行 agent loop
        result = agent_loop(
            user_message=task.task_description if task else '',
            llm_config=None,
            file_name=file_name,
            max_iterations=5
        )
        
        # 更新任务结果
        task = AgentCallLog.query.get(task_id)
        if task:
            if result.get('ok'):
                task.status = 'completed'
                task.output_summary = result.get('output', '')[:500]
            else:
                task.status = 'failed'
                task.error_message = result.get('error', '')
            task.duration_ms = int((time.time() - start_time) * 1000)
            db.session.commit()
            
    except Exception as e:
        task = AgentCallLog.query.get(task_id)
        if task:
            task.status = 'failed'
            task.error_message = str(e)[:500]
            db.session.commit()
