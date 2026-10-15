"""
auth.py — JWT 认证 + API Key 认证 + 权限控制
"""
from functools import wraps
from flask import request, jsonify, g
from flask_jwt_extended import (
    JWTManager, create_access_token, create_refresh_token,
    jwt_required, get_jwt_identity, verify_jwt_in_request
)
from models import User, ApiKey, db
import datetime
import ipaddress
import os

jwt = JWTManager()

# ---- 每日配额（内存计数；重启归零，适合单机部署） ----
_daily_requests = {}   # f"{user_id}:{date}" -> count
_token_usage_cache = {}  # f"{user_id}:{date}" -> (tokens, ts)
_TOKEN_CACHE_TTL = 60


def _today_str():
    return datetime.date.today().isoformat()


def _count_tokens_today(user_id):
    """统计用户今日 Agent 调用消耗的 token（带 60s 缓存，避免每请求查库）"""
    import time
    key = f"{user_id}:{_today_str()}"
    now = time.time()
    cached = _token_usage_cache.get(key)
    if cached and now - cached[1] < _TOKEN_CACHE_TTL:
        return cached[0]
    from models import AgentCallLog
    start = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    total = db.session.query(
        db.func.sum(AgentCallLog.total_tokens)
    ).filter(
        AgentCallLog.user_id == user_id,
        AgentCallLog.started_at >= start
    ).scalar() or 0
    _token_usage_cache[key] = (int(total), now)
    return int(total)


def check_quota(user):
    """检查并累加用户当日配额，返回 (ok, error)；超限时 error 非空"""
    if user.role == 'admin':
        return True, None
    today = _today_str()
    rkey = f"{user.id}:{today}"

    max_req = user.max_requests_per_day or 500
    _daily_requests[rkey] = _daily_requests.get(rkey, 0) + 1
    if _daily_requests[rkey] > max_req:
        return False, '当日请求次数已达上限，请明天再试'

    max_tokens = user.max_tokens_per_day or 100000
    if _count_tokens_today(user.id) > max_tokens:
        return False, '当日 Token 用量已达上限，请明天再试'

    return True, None


def init_auth(app):
    """初始化 JWT"""
    app.config['JWT_SECRET_KEY'] = app.config.get('SECRET_KEY', 'dev-secret-key-change-in-production')
    app.config['JWT_ACCESS_TOKEN_EXPIRES'] = datetime.timedelta(hours=24)
    app.config['JWT_REFRESH_TOKEN_EXPIRES'] = datetime.timedelta(days=30)
    jwt.init_app(app)


def authenticate_user(username, password):
    """验证用户名密码，返回 (user, error)"""
    user = User.query.filter_by(username=username, is_active=True).first()
    if not user:
        return None, '用户不存在或已禁用'
    if not user.check_password(password):
        return None, '密码错误'
    return user, None


def generate_tokens(user):
    """生成 access + refresh token"""
    return {
        'access_token': create_access_token(identity=user.id),
        'refresh_token': create_refresh_token(identity=user.id),
        'user': user.to_dict()
    }


def require_auth(f):
    """JWT 认证装饰器"""
    @wraps(f)
    def decorated(*args, **kwargs):
        try:
            verify_jwt_in_request()
            user_id = get_jwt_identity()
            user = User.query.get(user_id)
            if not user or not user.is_active:
                return jsonify({'error': '用户不存在或已禁用'}), 401
            ok, err = check_quota(user)
            if not ok:
                return jsonify({'error': err}), 429
            g.current_user = user
            return f(*args, **kwargs)
        except Exception as e:
            return jsonify({'error': f'认证失败: {str(e)}'}), 401
    return decorated


def require_api_key(f):
    """API Key 认证装饰器（仅接受 X-API-Key 请求头；查询串传递已禁用，
    避免 Key 泄漏到访问日志/浏览器历史/Referer）"""
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get('X-API-Key')
        if not key:
            return jsonify({'error': '缺少 API Key'}), 401
        
        api_key = ApiKey.query.filter_by(key=key, is_active=True).first()
        if not api_key:
            return jsonify({'error': '无效的 API Key'}), 401
        
        if api_key.expires_at and api_key.expires_at < datetime.datetime.utcnow():
            return jsonify({'error': 'API Key 已过期'}), 401
        
        user = User.query.get(api_key.user_id)
        if not user or not user.is_active:
            return jsonify({'error': '用户不存在或已禁用'}), 401
        
        api_key.last_used_at = datetime.datetime.utcnow()
        db.session.commit()
        
        g.current_user = user
        g.api_key = api_key
        return f(*args, **kwargs)
    return decorated


def require_auth_or_api_key(f):
    """JWT 或 API Key 二选一认证

    网页登录用户带 JWT Token，脚本/自动化任务带 X-API-Key 请求头（或 ?api_key= 参数），
    两者任一通过即可访问。通过后 g.current_user 均被设置，可继续使用 require_role 做权限控制。
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        # 1. 先尝试 JWT（网页登录态）
        try:
            verify_jwt_in_request()
            user_id = get_jwt_identity()
            user = User.query.get(user_id)
            if user and user.is_active:
                ok, err = check_quota(user)
                if not ok:
                    return jsonify({'error': err}), 429
                g.current_user = user
                return f(*args, **kwargs)
        except Exception:
            pass

        # 2. 再尝试 API Key（脚本/自动化）
        key = request.headers.get('X-API-Key')
        if not key:
            return jsonify({'error': '认证失败: 请携带 JWT Token 或 API Key (请求头 X-API-Key)'}), 401

        api_key = ApiKey.query.filter_by(key=key, is_active=True).first()
        if not api_key:
            return jsonify({'error': '认证失败: 无效的 API Key'}), 401
        if api_key.expires_at and api_key.expires_at < datetime.datetime.utcnow():
            return jsonify({'error': '认证失败: API Key 已过期'}), 401

        user = User.query.get(api_key.user_id)
        if not user or not user.is_active:
            return jsonify({'error': '认证失败: 用户不存在或已禁用'}), 401

        ok, err = check_quota(user)
        if not ok:
            return jsonify({'error': err}), 429

        api_key.last_used_at = datetime.datetime.utcnow()
        db.session.commit()

        g.current_user = user
        g.api_key = api_key
        return f(*args, **kwargs)
    return decorated


def _request_peer_ip():
    """取客户端真实 IP：TRUST_PROXY=1 时信任 X-Forwarded-For（serveo/nginx 等代理部署）；
    默认（单机直连）直接取 TCP 对端地址，避免伪造头绕过认证。"""
    if os.environ.get('TRUST_PROXY') == '1':
        xff = request.headers.get('X-Forwarded-For', '')
        first = xff.split(',')[0].strip()
        if first:
            return first
    return (request.remote_addr or '').strip()


def _is_local_or_lan(addr: str) -> bool:
    """本地回环 + 局域网私网段 → 单机工具免登录范围；公网地址一律不在此列（需认证）"""
    if not addr:
        return False
    addr = addr.lower().strip()
    if addr in ('127.0.0.1', '::1', 'localhost'):
        return True
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def require_auth_or_api_key_local(f):
    """JWT/API Key 优先；本地(localhost)匿名请求回退到默认管理员。

    用于单机本地工具的治理类接口（如定时任务调度器）：
    - 带 JWT（网页登录态）或 X-API-Key 时按原逻辑校验；
    - 未带任何凭证且请求来自 localhost 时，以默认 admin 身份放行，
      使本地部署无需登录即可使用调度器，同时保留远程访问必须认证的能力。
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        # 1. 先尝试 JWT（网页登录态）
        try:
            verify_jwt_in_request()
            user_id = get_jwt_identity()
            user = User.query.get(user_id)
            if user and user.is_active:
                ok, err = check_quota(user)
                if not ok:
                    return jsonify({'error': err}), 429
                g.current_user = user
                return f(*args, **kwargs)
        except Exception:
            pass

        # 2. 再尝试 API Key（脚本/自动化）
        key = request.headers.get('X-API-Key')
        if key:
            api_key = ApiKey.query.filter_by(key=key, is_active=True).first()
            if api_key and (not api_key.expires_at or api_key.expires_at >= datetime.datetime.utcnow()):
                user = User.query.get(api_key.user_id)
                if user and user.is_active:
                    ok, err = check_quota(user)
                    if not ok:
                        return jsonify({'error': err}), 429
                    api_key.last_used_at = datetime.datetime.utcnow()
                    db.session.commit()
                    g.current_user = user
                    g.api_key = api_key
                    return f(*args, **kwargs)
            return jsonify({'error': '认证失败: 无效的 API Key'}), 401

        # 3. 本地/LAN 匿名 -> 默认管理员（单机工具免登录：127.0.0.1 + 私网段）
        addr = _request_peer_ip()
        if _is_local_or_lan(addr):
            admin = User.query.filter_by(username='admin', is_active=True).first()
            if admin:
                g.current_user = admin
                return f(*args, **kwargs)

        return jsonify({'error': '认证失败: 请携带 JWT Token 或 API Key (请求头 X-API-Key)'}), 401
    return decorated


def optional_auth(f):
    """可选认证（公开接口）：无凭证匿名放行；带了 JWT/API Key 则必须有效并计入配额

    用于"默认公开、脚本可带 Key 访问"的数据接口。带了无效/过期凭证会 401（让脚本及时发现 Key 失效）。
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        # 1. 尝试 JWT
        try:
            verify_jwt_in_request()
            user_id = get_jwt_identity()
            user = User.query.get(user_id)
            if user and user.is_active:
                ok, err = check_quota(user)
                if not ok:
                    return jsonify({'error': err}), 429
                g.current_user = user
                return f(*args, **kwargs)
        except Exception:
            pass

        # 2. 尝试 API Key：带了就必须有效，否则 401；没带则匿名放行
        key = request.headers.get('X-API-Key')
        if not key:
            return f(*args, **kwargs)

        api_key = ApiKey.query.filter_by(key=key, is_active=True).first()
        if not api_key:
            return jsonify({'error': '认证失败: 无效的 API Key'}), 401
        if api_key.expires_at and api_key.expires_at < datetime.datetime.utcnow():
            return jsonify({'error': '认证失败: API Key 已过期'}), 401

        user = User.query.get(api_key.user_id)
        if not user or not user.is_active:
            return jsonify({'error': '认证失败: 用户不存在或已禁用'}), 401

        ok, err = check_quota(user)
        if not ok:
            return jsonify({'error': err}), 429

        api_key.last_used_at = datetime.datetime.utcnow()
        db.session.commit()

        g.current_user = user
        g.api_key = api_key
        return f(*args, **kwargs)
    return decorated


def local_guard():
    """Blueprint 级 before_request 守卫（语义同 require_auth_or_api_key_local）。

    - JWT / API Key 有效 → 放行并计入配额；
    - 携带了 API Key 但无效/过期 → 401（让脚本及时发现 Key 失效）；
    - 无凭证且来源为本地/局域网（单机工具免登录范围）→ 回退默认 admin；
    - 无凭证且来自公网（如隧道暴露）→ 401，必须认证。

    用于守卫文件读写/代码执行/模型训练等高危 Blueprint；
    OPTIONS 预检请求直接放行。
    """
    if request.method == 'OPTIONS':
        return None

    # 1. JWT（网页登录态）
    try:
        verify_jwt_in_request()
        user_id = get_jwt_identity()
        user = User.query.get(user_id)
        if user and user.is_active:
            ok, err = check_quota(user)
            if ok:
                g.current_user = user
                return None
    except Exception:
        pass

    # 2. API Key（脚本/自动化）：带了就必须有效
    key = request.headers.get('X-API-Key')
    if key:
        api_key = ApiKey.query.filter_by(key=key, is_active=True).first()
        if api_key and (not api_key.expires_at or api_key.expires_at >= datetime.datetime.utcnow()):
            user = User.query.get(api_key.user_id)
            if user and user.is_active:
                ok, err = check_quota(user)
                if ok:
                    api_key.last_used_at = datetime.datetime.utcnow()
                    db.session.commit()
                    g.current_user = user
                    g.api_key = api_key
                    return None
        return jsonify({'error': '认证失败: 无效的 API Key'}), 401

    # 3. 本地/局域网匿名 → 默认管理员
    addr = _request_peer_ip()
    if _is_local_or_lan(addr):
        admin = User.query.filter_by(username='admin', is_active=True).first()
        if admin:
            g.current_user = admin
            return None

    return jsonify({'error': '认证失败: 请携带 JWT Token 或 API Key (请求头 X-API-Key)'}), 401


def require_role(*roles):
    """角色权限装饰器"""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not hasattr(g, 'current_user'):
                return jsonify({'error': '未认证'}), 401
            if g.current_user.role not in roles:
                return jsonify({'error': '权限不足'}), 403
            return f(*args, **kwargs)
        return decorated
    return decorator


def get_current_user():
    """获取当前用户"""
    return getattr(g, 'current_user', None)
