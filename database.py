"""
database.py — 数据库初始化 + 连接管理
"""
import os
import secrets
import logging

from flask import Flask
from models import db, User

# DATATOOL_DATA_DIR 优先（npx 部署时指向 ~/.datatool-web/data）
_data_dir = os.getenv('DATATOOL_DATA_DIR') or os.path.join(os.path.dirname(__file__), 'data')
DB_PATH = os.path.join(_data_dir, 'app.db')
logger = logging.getLogger('data.tool')


def init_db(app: Flask):
    """初始化数据库"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{DB_PATH}'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    
    with app.app_context():
        db.create_all()
        # 兼容：为既有库补齐新增列（SQLite 的 create_all 不会修改已有表）
        _ensure_column('scheduled_tasks', 'depends_on',
                       'ALTER TABLE scheduled_tasks ADD COLUMN depends_on VARCHAR(32) DEFAULT \'\'')
        
        # 创建默认管理员
        # 密码优先级：ADMIN_PASSWORD 环境变量 > 随机生成（不再使用硬编码弱密码）
        # 生产部署请务必通过环境变量 ADMIN_PASSWORD 设置强密码，登录后也应立即修改。
        if not User.query.filter_by(username='admin').first():
            admin_password = os.environ.get('ADMIN_PASSWORD') or secrets.token_urlsafe(12)
            admin = User(
                username='admin',
                email='admin@data.tool',
                role='admin'
            )
            admin.set_password(admin_password)
            db.session.add(admin)
            db.session.commit()
            if os.environ.get('ADMIN_PASSWORD'):
                logger.info('[DB] 默认管理员已创建: admin（密码取自 ADMIN_PASSWORD 环境变量）')
            else:
                logger.warning(
                    f'[DB] 默认管理员已创建: admin / {admin_password}\n'
                    '     ⚠ 这是随机生成的初始密码（未设置 ADMIN_PASSWORD 环境变量），请登录后立即修改！'
                )


def _ensure_column(table: str, column: str, alter_sql: str):
    """SQLite 兼容：若表缺少列则执行 ALTER（捕获已存在错误）"""
    try:
        rows = db.session.execute(db.text(f'PRAGMA table_info({table})')).fetchall()
        cols = [r[1] for r in rows]
        if column not in cols:
            db.session.execute(db.text(alter_sql))
            db.session.commit()
            logger.info(f'[DB] 已为 {table} 补齐列 {column}')
    except Exception as e:
        logger.warning(f'[DB] 补齐列 {table}.{column} 失败（忽略）: {e}')


def get_db():
    return db
