"""
routes/auth_routes.py — 用户认证 + API Key 管理
"""
from flask import Blueprint, request, jsonify, g
from models import db, User, ApiKey
from auth import (
    authenticate_user, generate_tokens,
    require_auth, require_role, require_auth_or_api_key, get_current_user
)
import datetime
import time

auth_bp = Blueprint('auth', __name__)

# ---- 登录防爆破（内存记录：用户名/IP 维度） ----
_MAX_FAIL = 5          # 最多连续失败次数
_LOCK_SECONDS = 900    # 锁定 15 分钟
_login_fail = {}       # key -> {'count': int, 'lock_until': float}


def _fail_key(username):
    """以 用户名+IP 作为防爆破维度"""
    ip = request.remote_addr or 'unknown'
    return f"{username}@{ip}"


def _check_locked(username):
    """返回锁定剩余秒数；未锁定返回 0"""
    rec = _login_fail.get(_fail_key(username))
    if not rec:
        return 0
    lock_until = rec.get('lock_until', 0)
    # 尚未触发锁定（只是失败计数），不清理计数状态
    if lock_until <= 0:
        return 0
    remain = lock_until - time.time()
    if remain > 0:
        return int(remain)
    # 锁定期已过，清理
    _login_fail.pop(_fail_key(username), None)
    return 0


def _record_failure(username):
    key = _fail_key(username)
    rec = _login_fail.get(key, {'count': 0, 'lock_until': 0})
    rec['count'] += 1
    if rec['count'] >= _MAX_FAIL:
        rec['lock_until'] = time.time() + _LOCK_SECONDS
        rec['count'] = 0
    _login_fail[key] = rec


def _clear_failures(username):
    _login_fail.pop(_fail_key(username), None)


@auth_bp.route('/auth/register', methods=['POST'])
def register():
    """注册"""
    data = request.get_json()
    if not data:
        return jsonify({'error': '无效的请求数据'}), 400
    
    username = data.get('username', '').strip()
    email = data.get('email', '').strip()
    password = data.get('password', '')
    
    if not username or not email or not password:
        return jsonify({'error': '用户名、邮箱、密码不能为空'}), 400
    if len(password) < 6:
        return jsonify({'error': '密码至少6位'}), 400
    if User.query.filter_by(username=username).first():
        return jsonify({'error': '用户名已存在'}), 409
    if User.query.filter_by(email=email).first():
        return jsonify({'error': '邮箱已注册'}), 409
    
    user = User(username=username, email=email)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    
    return jsonify({'ok': True, 'user': user.to_dict()}), 201


@auth_bp.route('/auth/login', methods=['POST'])
def login():
    """登录（含防爆破：连续失败 5 次锁定 15 分钟）"""
    data = request.get_json()
    if not data:
        return jsonify({'error': '无效的请求数据'}), 400

    username = data.get('username', '').strip()

    # 防爆破：先检查是否锁定
    remain = _check_locked(username)
    if remain > 0:
        return jsonify({'error': f'尝试次数过多，请 {remain // 60 + 1} 分钟后再试'}), 429

    user, error = authenticate_user(username, data.get('password', ''))
    if error:
        _record_failure(username)
        return jsonify({'error': error}), 401

    # 登录成功：清除失败记录
    _clear_failures(username)
    user.last_login = datetime.datetime.utcnow()
    db.session.commit()

    return jsonify({'ok': True, **generate_tokens(user)})


@auth_bp.route('/auth/refresh', methods=['POST'])
@require_auth
def refresh():
    """刷新 Token"""
    from flask_jwt_extended import create_access_token
    user = get_current_user()
    return jsonify({'ok': True, 'access_token': create_access_token(identity=user.id)})


@auth_bp.route('/auth/me', methods=['GET'])
@require_auth_or_api_key
def me():
    """当前用户（支持 JWT 或 API Key）"""
    return jsonify({'ok': True, 'user': get_current_user().to_dict()})


@auth_bp.route('/auth/api-keys', methods=['GET'])
@require_auth
def list_api_keys():
    """列出 API Key"""
    user = get_current_user()
    keys = ApiKey.query.filter_by(user_id=user.id).all()
    return jsonify({'ok': True, 'api_keys': [{
        'id': k.id, 'key': k.key, 'name': k.name,
        'is_active': k.is_active,
        'created_at': k.created_at.isoformat() if k.created_at else None,
        'last_used_at': k.last_used_at.isoformat() if k.last_used_at else None,
    } for k in keys]})


@auth_bp.route('/auth/api-keys', methods=['POST'])
@require_auth
def create_api_key():
    """创建 API Key"""
    user = get_current_user()
    data = request.get_json() or {}
    
    api_key = ApiKey(
        user_id=user.id,
        name=data.get('name', 'API Key'),
        expires_at=datetime.datetime.utcnow() + datetime.timedelta(days=365) if not data.get('no_expire') else None
    )
    db.session.add(api_key)
    db.session.commit()
    
    return jsonify({'ok': True, 'api_key': {
        'id': api_key.id, 'key': api_key.key, 'name': api_key.name,
        'expires_at': api_key.expires_at.isoformat() if api_key.expires_at else None,
    }}), 201


@auth_bp.route('/auth/api-keys/<key_id>', methods=['DELETE'])
@require_auth
def delete_api_key(key_id):
    """删除 API Key"""
    user = get_current_user()
    api_key = ApiKey.query.filter_by(id=key_id, user_id=user.id).first()
    if not api_key:
        return jsonify({'error': 'API Key 不存在'}), 404
    db.session.delete(api_key)
    db.session.commit()
    return jsonify({'ok': True})


# ==================== 管理员接口 ====================

@auth_bp.route('/admin/stats', methods=['GET'])
@require_auth
@require_role('admin')
def admin_stats():
    """管理员：系统统计"""
    from models import AgentCallLog, ToolCallLog
    
    total_users = User.query.count()
    active_users = User.query.filter_by(is_active=True).count()
    total_calls = AgentCallLog.query.count()
    total_tool_calls = ToolCallLog.query.count()
    
    today = datetime.datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today_calls = AgentCallLog.query.filter(AgentCallLog.started_at >= today).count()
    
    total_tokens = db.session.query(db.func.sum(AgentCallLog.total_tokens)).scalar() or 0
    total_cost = db.session.query(db.func.sum(AgentCallLog.cost_cny)).scalar() or 0
    completed_calls = AgentCallLog.query.filter_by(status='completed').count()
    avg_duration = db.session.query(db.func.avg(AgentCallLog.duration_ms)).scalar() or 0
    
    return jsonify({
        'ok': True,
        'stats': {
            'total_users': total_users,
            'active_users': active_users,
            'total_calls': total_calls,
            'total_tool_calls': total_tool_calls,
            'today_calls': today_calls,
            'total_tokens': total_tokens,
            'total_cost': float(total_cost),
            'completed_calls': completed_calls,
            'avg_duration_ms': float(avg_duration),
        }
    })


@auth_bp.route('/admin/system-health', methods=['GET'])
@require_auth
@require_role('admin')
def system_health():
    """系统健康"""
    try:
        import psutil
        return jsonify({
            'ok': True,
            'memory_rss_mb': round(psutil.Process().memory_info().rss / 1024 / 1024, 1),
            'memory_percent': round(psutil.virtual_memory().percent, 1),
            'cpu_percent': round(psutil.cpu_percent(interval=0.1), 1),
            'cpu_count': psutil.cpu_count(),
            'disk_percent': round(psutil.disk_usage('/').percent, 1),
            'disk_used_gb': round(psutil.disk_usage('/').used / 1024 / 1024 / 1024, 1),
            'disk_total_gb': round(psutil.disk_usage('/').total / 1024 / 1024 / 1024, 1),
        })
    except ImportError:
        return jsonify({'ok': True, 'memory_rss_mb': 0, 'memory_percent': 0, 'cpu_percent': 0})


@auth_bp.route('/admin/users', methods=['GET'])
@require_auth
@require_role('admin')
def list_users():
    """列出所有用户"""
    users = User.query.all()
    return jsonify({'ok': True, 'users': [u.to_dict() for u in users]})
