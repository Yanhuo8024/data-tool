#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
routes/db_routes.py - 外部数据库直连 + 智能 SQL（企业版 P0）

安全设计：
  - 本地免登录（require_auth_or_api_key_local）
  - 凭证加密存储，返回前端时绝不回传密码
  - 查询强制只读（SELECT/WITH 等），DDL/DML 全部拦截
  - 每次操作写审计日志（DbAuditLog）
"""
from flask import Blueprint, request, jsonify
from models import db, DBConnection, DbAuditLog
from auth import require_auth_or_api_key_local
from datetime import datetime, timezone
import traceback

db_bp = Blueprint('db', __name__)


def _audit(conn_id=None, conn_name='', action='', sql='', row_count=None, ok=True, error=''):
    try:
        db.session.add(DbAuditLog(
            conn_id=conn_id, conn_name=conn_name, action=action,
            sql=(sql or '')[:4000], row_count=row_count, ok=ok, error=(error or '')[:500],
        ))
        db.session.commit()
    except Exception:
        db.session.rollback()


def _conn_to_cfg(c: DBConnection) -> dict:
    """ORM 对象 → 引擎配置（解密密码）"""
    from db_connector import decrypt_password
    return {
        'db_type': c.db_type, 'host': c.host, 'port': c.port,
        'database': c.database, 'username': c.username,
        'password': decrypt_password(c.password_enc or ''),
    }


def _load_conn(conn_id):
    c = DBConnection.query.get(conn_id)
    if not c:
        return None, (jsonify({'ok': False, 'error': '连接不存在'}), 404)
    return c, None


# ==================== 连接管理 ====================

@db_bp.route('/api/db/connections', methods=['GET'])
@require_auth_or_api_key_local
def list_connections():
    conns = DBConnection.query.order_by(DBConnection.created_at.desc()).all()
    return jsonify({'ok': True, 'connections': [c.to_dict() for c in conns]})


@db_bp.route('/api/db/connections', methods=['POST'])
@require_auth_or_api_key_local
def create_connection():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    db_type = (data.get('db_type') or '').strip().lower()
    database = (data.get('database') or '').strip()
    if not name or not db_type or not database:
        return jsonify({'ok': False, 'error': '名称、类型、数据库名必填'}), 400
    if db_type not in ('mysql', 'postgres', 'clickhouse', 'sqlite'):
        return jsonify({'ok': False, 'error': f'不支持的数据库类型: {db_type}'}), 400

    from db_connector import encrypt_password
    c = DBConnection(
        name=name, db_type=db_type, host=(data.get('host') or '127.0.0.1').strip(),
        port=data.get('port'), database=database,
        username=(data.get('username') or '').strip(),
        password_enc=encrypt_password(data.get('password') or ''),
        remark=(data.get('remark') or '').strip(),
    )
    db.session.add(c)
    db.session.commit()
    _audit(conn_id=c.id, conn_name=c.name, action='create')
    return jsonify({'ok': True, 'connection': c.to_dict()})


@db_bp.route('/api/db/connections/<conn_id>', methods=['PUT'])
@require_auth_or_api_key_local
def update_connection(conn_id):
    c, err = _load_conn(conn_id)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    from db_connector import encrypt_password
    for k in ('name', 'db_type', 'host', 'database', 'username', 'remark'):
        if k in data and data[k] is not None:
            setattr(c, k, str(data[k]).strip() if isinstance(data[k], str) else data[k])
    if 'port' in data:
        c.port = data['port'] or None
    # 密码：传了新密码才更新（留空则保持不变）
    if data.get('password'):
        c.password_enc = encrypt_password(str(data['password']))
    db.session.commit()
    _audit(conn_id=c.id, conn_name=c.name, action='update')
    return jsonify({'ok': True, 'connection': c.to_dict()})


@db_bp.route('/api/db/connections/<conn_id>', methods=['DELETE'])
@require_auth_or_api_key_local
def delete_connection(conn_id):
    c, err = _load_conn(conn_id)
    if err:
        return err
    name = c.name
    db.session.delete(c)
    db.session.commit()
    _audit(conn_id=conn_id, conn_name=name, action='delete')
    return jsonify({'ok': True})


# ==================== 连接测试（不保存）====================

@db_bp.route('/api/db/test', methods=['POST'])
@require_auth_or_api_key_local
def test_connection_api():
    data = request.get_json(silent=True) or {}
    try:
        from db_connector import test_connection
        cfg = {
            'db_type': data.get('db_type', 'mysql'), 'host': data.get('host', '127.0.0.1'),
            'port': data.get('port'), 'database': data.get('database', ''),
            'username': data.get('username', ''), 'password': data.get('password', ''),
        }
        ok = test_connection(cfg)
        _audit(action='test', conn_name=data.get('name', ''), ok=ok,
               error='' if ok else '连接失败')
        return jsonify({'ok': ok, 'message': '连接成功' if ok else '连接失败：无法连通或认证失败'})
    except Exception as e:
        _audit(action='test', conn_name=data.get('name', ''), ok=False, error=str(e))
        return jsonify({'ok': False, 'error': f'连接失败: {e}'})


# ==================== 表浏览 ====================

@db_bp.route('/api/db/connections/<conn_id>/tables', methods=['GET'])
@require_auth_or_api_key_local
def list_tables_api(conn_id):
    c, err = _load_conn(conn_id)
    if err:
        return err
    try:
        from db_connector import list_tables
        tables = list_tables(_conn_to_cfg(c))
        return jsonify({'ok': True, 'tables': tables})
    except Exception as e:
        _audit(conn_id=c.id, conn_name=c.name, action='list', ok=False, error=str(e))
        return jsonify({'ok': False, 'error': str(e)}), 400


@db_bp.route('/api/db/connections/<conn_id>/schema/<path:table>', methods=['GET'])
@require_auth_or_api_key_local
def get_schema_api(conn_id, table):
    c, err = _load_conn(conn_id)
    if err:
        return err
    try:
        from db_connector import get_schema
        cols = get_schema(_conn_to_cfg(c), table)
        return jsonify({'ok': True, 'table': table, 'columns': cols})
    except Exception as e:
        _audit(conn_id=c.id, conn_name=c.name, action='list', ok=False, error=str(e))
        return jsonify({'ok': False, 'error': str(e)}), 400


# ==================== 安全只读执行 ====================

@db_bp.route('/api/db/connections/<conn_id>/query', methods=['POST'])
@require_auth_or_api_key_local
def execute_query(conn_id):
    c, err = _load_conn(conn_id)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    sql = (data.get('sql') or '').strip()
    if not sql:
        return jsonify({'ok': False, 'error': 'SQL 不能为空'}), 400
    try:
        from db_connector import execute_readonly_sql
        result = execute_readonly_sql(_conn_to_cfg(c), sql)
        _audit(conn_id=c.id, conn_name=c.name, action='sql', sql=result['sql'],
               row_count=len(result['rows']), ok=True)
        return jsonify({'ok': True, **result})
    except Exception as e:
        _audit(conn_id=c.id, conn_name=c.name, action='sql', sql=sql, ok=False, error=str(e))
        return jsonify({'ok': False, 'error': str(e)}), 400


# ==================== 智能 SQL（自然语言 → SQL → 执行）====================

@db_bp.route('/api/db/connections/<conn_id>/nlq', methods=['POST'])
@require_auth_or_api_key_local
def nlq_query(conn_id):
    c, err = _load_conn(conn_id)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    question = (data.get('question') or '').strip()
    if not question:
        return jsonify({'ok': False, 'error': '问题不能为空'}), 400
    from nlq_answer import answer as nlq_answer_fn
    result = nlq_answer_fn(question, conn_id=conn_id, preview=bool(data.get('preview')),
                           allowed_tables=(data.get('tables') or None))
    sql = result.get('sql') or ''
    if not result.get('ok'):
        err_msg = '；'.join(result.get('errors') or []) or '生成或执行失败'
        _audit(conn_id=c.id, conn_name=c.name, action='nlq', sql=sql, ok=False, error=err_msg)
        return jsonify({'ok': False, 'error': err_msg}), 400
    if result.get('preview'):
        _audit(conn_id=c.id, conn_name=c.name, action='nlq_preview', sql=sql, ok=True)
        return jsonify({'ok': True, 'question': question, 'preview': True, 'sql': sql})
    _audit(conn_id=c.id, conn_name=c.name, action='nlq', sql=sql,
           row_count=len(result.get('rows') or []), ok=True)
    return jsonify({'ok': True, 'question': question, 'columns': result.get('columns'),
                    'rows': result.get('rows'), 'truncated': result.get('truncated'), 'sql': sql})


# ==================== 审计日志 ====================

@db_bp.route('/api/db/audit', methods=['GET'])
@require_auth_or_api_key_local
def audit_logs():
    logs = DbAuditLog.query.order_by(DbAuditLog.created_at.desc()).limit(100).all()
    return jsonify({'ok': True, 'logs': [{
        'id': l.id, 'conn_name': l.conn_name, 'action': l.action,
        'sql': l.sql, 'row_count': l.row_count, 'ok': l.ok, 'error': l.error,
        'created_at': l.created_at.isoformat() if l.created_at else None,
    } for l in logs]})
