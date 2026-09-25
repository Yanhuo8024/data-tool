"""
models.py — SQLAlchemy ORM 数据模型
多用户隔离 + Agent 调用追踪 + 成本统计
"""
from datetime import datetime, timezone
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
import uuid
import secrets

db = SQLAlchemy()


def _uuid():
    return uuid.uuid4().hex


def _api_key():
    return secrets.token_urlsafe(32)


# ==================== 用户与认证 ====================

class User(db.Model):
    __tablename__ = 'users'

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), default='user')  # guest / user / admin
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    last_login = db.Column(db.DateTime)

    # 配额
    max_requests_per_day = db.Column(db.Integer, default=500)
    max_tokens_per_day = db.Column(db.Integer, default=100000)

    # 关系
    api_keys = db.relationship('ApiKey', backref='user', lazy='dynamic', cascade='all, delete-orphan')
    sessions = db.relationship('UserSession', backref='user', lazy='dynamic', cascade='all, delete-orphan')
    agent_calls = db.relationship('AgentCallLog', backref='user', lazy='dynamic')

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)

    def to_dict(self):
        return {
            'id': self.id, 'username': self.username, 'email': self.email,
            'role': self.role, 'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class ApiKey(db.Model):
    __tablename__ = 'api_keys'

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    user_id = db.Column(db.String(32), db.ForeignKey('users.id'), nullable=False, index=True)
    key = db.Column(db.String(64), unique=True, nullable=False, default=_api_key)
    name = db.Column(db.String(50))
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    expires_at = db.Column(db.DateTime)
    last_used_at = db.Column(db.DateTime)


class UserSession(db.Model):
    __tablename__ = 'user_sessions'

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    user_id = db.Column(db.String(32), db.ForeignKey('users.id'), nullable=False, index=True)
    token = db.Column(db.String(512), unique=True, nullable=False)
    ip_address = db.Column(db.String(45))
    user_agent = db.Column(db.String(200))
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    expires_at = db.Column(db.DateTime, nullable=False)


# ==================== Agent 调用追踪 ====================

class AgentCallLog(db.Model):
    __tablename__ = 'agent_call_logs'

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    user_id = db.Column(db.String(32), db.ForeignKey('users.id'), nullable=False, index=True)
    session_id = db.Column(db.String(32), index=True)
    task_description = db.Column(db.Text)
    
    # 执行状态: pending / running / completed / failed / cancelled
    status = db.Column(db.String(20), default='pending')
    
    # 耗时与性能
    started_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    completed_at = db.Column(db.DateTime)
    duration_ms = db.Column(db.Integer)
    iterations = db.Column(db.Integer, default=0)
    
    # Token 用量
    prompt_tokens = db.Column(db.Integer, default=0)
    completion_tokens = db.Column(db.Integer, default=0)
    total_tokens = db.Column(db.Integer, default=0)
    
    # 成本（按次计算）
    cost_cny = db.Column(db.Float, default=0.0)
    
    # 结果
    output_summary = db.Column(db.Text)
    error_message = db.Column(db.Text)

    # 关系
    tool_calls = db.relationship('ToolCallLog', backref='agent_call', lazy='dynamic',
                                  cascade='all, delete-orphan', order_by='ToolCallLog.sequence')

    def to_dict(self):
        return {
            'id': self.id, 'status': self.status,
            'duration_ms': self.duration_ms, 'iterations': self.iterations,
            'total_tokens': self.total_tokens, 'cost_cny': self.cost_cny,
            'started_at': self.started_at.isoformat() if self.started_at else None,
        }


class ToolCallLog(db.Model):
    __tablename__ = 'tool_call_logs'

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    agent_call_id = db.Column(db.String(32), db.ForeignKey('agent_call_logs.id'), nullable=False, index=True)
    sequence = db.Column(db.Integer, nullable=False)
    tool_name = db.Column(db.String(50), nullable=False)
    params_json = db.Column(db.Text)
    result_preview = db.Column(db.Text)
    
    # 状态: success / failed / cached
    status = db.Column(db.String(20), default='success')
    error_message = db.Column(db.Text)
    
    # 性能
    started_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    duration_ms = db.Column(db.Integer)


# ==================== 用户反馈 ====================

class UserFeedback(db.Model):
    __tablename__ = 'user_feedback'

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    user_id = db.Column(db.String(32), db.ForeignKey('users.id'), nullable=False, index=True)
    agent_call_id = db.Column(db.String(32), db.ForeignKey('agent_call_logs.id'))
    
    # 反馈类型: quality / bug / feature / accuracy
    feedback_type = db.Column(db.String(20), default='quality')
    rating = db.Column(db.Integer)  # 1-5
    comment = db.Column(db.Text)
    
    # 处理状态
    is_resolved = db.Column(db.Boolean, default=False)
    admin_response = db.Column(db.Text)
    
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


# ==================== 知识库 ====================

class KnowledgeBase(db.Model):
    __tablename__ = 'knowledge_bases'

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    user_id = db.Column(db.String(32), db.ForeignKey('users.id'), nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    is_public = db.Column(db.Boolean, default=False)
    doc_count = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class KnowledgeDoc(db.Model):
    __tablename__ = 'knowledge_docs'

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    kb_id = db.Column(db.String(32), db.ForeignKey('knowledge_bases.id'), nullable=False, index=True)
    user_id = db.Column(db.String(32), db.ForeignKey('users.id'), nullable=False)
    title = db.Column(db.String(200))
    content = db.Column(db.Text)
    file_path = db.Column(db.String(500))
    file_type = db.Column(db.String(20))
    chunk_count = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


# ==================== 定时任务 ====================

class ScheduledTask(db.Model):
    """定时任务定义（APScheduler 驱动）"""
    __tablename__ = 'scheduled_tasks'

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    user_id = db.Column(db.String(32), db.ForeignKey('users.id'), nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.String(500), default='')
    # cron 表达式（5段标准格式: 分 时 日 月 周），如 "30 8 * * *" = 每天 08:30
    cron = db.Column(db.String(50), nullable=False, default='0 8 * * *')
    # 动作：调用内部接口
    target = db.Column(db.String(300), nullable=False)     # 内部路径，如 /api/dashboard/index-quote
    method = db.Column(db.String(10), default='GET')       # GET / POST
    body = db.Column(db.Text)                              # POST 时的 JSON body（可空）
    # 调用时使用用户自己的 API Key
    use_api_key = db.Column(db.Boolean, default=True)
    is_active = db.Column(db.Boolean, default=True)
    # 依赖管理（轻量 DAG）：上游任务 ID，上游未成功则跳过本次执行
    depends_on = db.Column(db.String(32), default='')

    # 最近一次执行
    last_run_at = db.Column(db.DateTime)
    last_status = db.Column(db.String(20))   # ok / error / running / skipped
    last_output = db.Column(db.Text)

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class ScheduledTaskRun(db.Model):
    """定时任务执行历史"""
    __tablename__ = 'scheduled_task_runs'

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    task_id = db.Column(db.String(32), db.ForeignKey('scheduled_tasks.id'), nullable=False, index=True)
    user_id = db.Column(db.String(32), db.ForeignKey('users.id'), nullable=False, index=True)
    status = db.Column(db.String(20))        # ok / error
    http_code = db.Column(db.Integer)
    output = db.Column(db.Text)
    error = db.Column(db.Text)
    duration_ms = db.Column(db.Integer)
    trigger = db.Column(db.String(20), default='schedule')  # schedule / manual
    started_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


# ==================== 数据库连接与审计（企业版 P0）====================

class DBConnection(db.Model):
    """外部数据库直连配置（MySQL/PostgreSQL/ClickHouse/SQLite），凭证加密存储"""
    __tablename__ = 'db_connections'

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    name = db.Column(db.String(80), nullable=False)
    db_type = db.Column(db.String(20), nullable=False)   # mysql / postgres / clickhouse / sqlite
    host = db.Column(db.String(120), default='127.0.0.1')
    port = db.Column(db.Integer)
    database = db.Column(db.String(120), nullable=False)
    username = db.Column(db.String(80))
    password_enc = db.Column(db.Text)                    # 加密后的密码，绝不落明文
    remark = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        return {
            'id': self.id, 'name': self.name, 'db_type': self.db_type,
            'host': self.host, 'port': self.port, 'database': self.database,
            'username': self.username,
            'has_password': bool(self.password_enc),
            'remark': self.remark,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class DbAuditLog(db.Model):
    """数据库操作审计：谁在何时执行了什么 SQL / 导出 / 分享"""
    __tablename__ = 'db_audit_logs'

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    conn_id = db.Column(db.String(32), db.ForeignKey('db_connections.id'), index=True)
    conn_name = db.Column(db.String(80))
    action = db.Column(db.String(20))      # test / list / sql / export / share
    sql = db.Column(db.Text)               # 执行的 SQL
    row_count = db.Column(db.Integer)
    ok = db.Column(db.Boolean, default=True)
    error = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
