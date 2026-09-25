#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
db_connector.py - 外部数据库直连 + 安全只读查询（企业版 P0）

能力：
  1. 连接配置：MySQL / PostgreSQL / ClickHouse / SQLite，凭证用 Fernet 加密存储（不落明文）
  2. 连接测试、表浏览、表结构查看
  3. 安全只读执行：仅允许 SELECT/WITH/EXPLAIN/SHOW/DESCRIBE，
     强制 LIMIT、拒绝多语句与 DDL/DML，带超时终止
  4. 表结构序列化（供 LLM 生成 SQL）

安全红线：
  - 只读优先：任何查询默认只读，写操作一律拒绝
  - 凭证不落地：密码加密存储，密钥文件与数据库文件同目录
  - 审计在路由层完成（见 routes/db_routes.py）
"""
import os
import re
import logging
import concurrent.futures

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
OUT_DIR = os.path.join(BASE_DIR, 'output')
UPLOAD_DIR = os.path.join(BASE_DIR, 'uploads')

# 查询硬限制
MAX_ROWS = 500          # 单次查询最多返回行数
DEFAULT_LIMIT = 200     # 未写 LIMIT 时自动追加
QUERY_TIMEOUT = 15      # 查询超时（秒）

# 只读关键字白名单（SQL 去除注释/字符串后的首关键字）
_READONLY_KEYWORDS = ('SELECT', 'WITH', 'EXPLAIN', 'SHOW', 'DESCRIBE', 'DESC', 'PRAGMA')
# 高危/写操作黑名单
_BLOCKED_KEYWORDS = (
    'INSERT', 'UPDATE', 'DELETE', 'DROP', 'ALTER', 'CREATE', 'TRUNCATE',
    'GRANT', 'REVOKE', 'CALL', 'EXEC', 'MERGE', 'REPLACE', 'ATTACH',
    'DETACH', 'VACUUM', 'SET ', 'COMMIT', 'ROLLBACK', 'BEGIN', 'LOAD',
)

# 方言 → SQLAlchemy URL 前缀
_DIALECT_URL = {
    'mysql': 'mysql+pymysql',
    'postgres': 'postgresql+psycopg2',
    'clickhouse': 'clickhouse+clickhouse_connect',
    'sqlite': 'sqlite',
}


# ==================== 凭证加密 ====================

_fernet = None


def _get_fernet():
    """惰性加载 Fernet；密钥文件不存在则首次生成（data/db_secret.key）"""
    global _fernet
    if _fernet is not None:
        return _fernet
    try:
        from cryptography.fernet import Fernet
        key_path = os.path.join(DATA_DIR, 'db_secret.key')
        if os.path.exists(key_path):
            with open(key_path, 'rb') as f:
                key = f.read()
        else:
            os.makedirs(DATA_DIR, exist_ok=True)
            key = Fernet.generate_key()
            with open(key_path, 'wb') as f:
                f.write(key)
        _fernet = Fernet(key)
    except Exception as e:
        logger.error("初始化加密失败: %s", e)
        _fernet = None
    return _fernet


def encrypt_password(plain: str) -> str:
    """加密密码；失败时抛异常（宁可失败也不落明文）"""
    if not plain:
        return ''
    f = _get_fernet()
    if f is None:
        raise RuntimeError("加密模块不可用，拒绝保存明文密码")
    return f.encrypt(plain.encode('utf-8')).decode('utf-8')


def decrypt_password(token: str) -> str:
    if not token:
        return ''
    f = _get_fernet()
    if f is None:
        return ''
    try:
        return f.decrypt(token.encode('utf-8')).decode('utf-8')
    except Exception as e:
        logger.error("解密失败: %s", e)
        return ''


# ==================== 引擎工厂 ====================

def build_url(cfg: dict) -> str:
    """根据连接配置构造 SQLAlchemy URL（密码内联时做 URL 转义）"""
    from urllib.parse import quote_plus
    db_type = cfg.get('db_type', 'mysql')
    prefix = _DIALECT_URL.get(db_type)
    if prefix is None:
        raise ValueError(f"不支持的数据库类型: {db_type}")
    if db_type == 'sqlite':
        return f"sqlite:///{cfg.get('database', '')}"
    user = quote_plus(cfg.get('username') or '')
    pwd = quote_plus(cfg.get('password') or '')
    host = cfg.get('host') or '127.0.0.1'
    port = cfg.get('port') or _default_port(db_type)
    db = cfg.get('database') or ''
    return f"{prefix}://{user}:{pwd}@{host}:{port}/{db}?charset=utf8mb4" if db_type == 'mysql' \
        else f"{prefix}://{user}:{pwd}@{host}:{port}/{db}"


def safe_sqlite_path(path):
    """
    SQLite 文件必须落在站点数据目录内（output/ uploads/ data/），防止任意文件读取。
    站点主库 app.db 存放用户凭据，禁止通过 SQL 接口直连。合法则返回 Path，否则 None。

    单一实现：/api/sql/connect、自然语言查数均调用本函数，避免两处白名单漂移。
    """
    from pathlib import Path
    if not path:
        return None
    try:
        rp = Path(str(path)).resolve()
    except Exception as e:
        logger.debug(f"sqlite path resolve failed: {e}")
        return None
    if rp.name.lower() == 'app.db':
        return None
    for base in (OUT_DIR, UPLOAD_DIR, DATA_DIR):
        try:
            if rp.is_relative_to(Path(base).resolve()):
                return rp
        except Exception as e:
            logger.debug(f"sqlite whitelist check failed for {base}: {e}")
            continue
    return None


def _default_port(db_type: str) -> int:
    return {'mysql': 3306, 'postgres': 5432, 'clickhouse': 8123}.get(db_type, 3306)


def make_engine(cfg: dict):
    """创建 SQLAlchemy 引擎（带连接超时）"""
    from sqlalchemy import create_engine
    url = build_url(cfg)
    kwargs = {}
    db_type = cfg.get('db_type', 'mysql')
    if db_type == 'mysql':
        kwargs['connect_args'] = {'connect_timeout': 8, 'read_timeout': QUERY_TIMEOUT, 'charset': 'utf8mb4'}
    elif db_type == 'postgres':
        kwargs['connect_args'] = {'connect_timeout': 8}
    elif db_type == 'clickhouse':
        kwargs['connect_args'] = {'connect_timeout': 8, 'read_timeout': QUERY_TIMEOUT}
    return create_engine(url, pool_pre_ping=True, pool_recycle=300, **kwargs)


# ==================== 连接测试 / 表浏览 ====================

def test_connection(cfg: dict) -> bool:
    """测试连接：执行 SELECT 1"""
    engine = make_engine(cfg)
    try:
        _run_with_timeout(_raw_scalar, engine, "SELECT 1")
        return True
    finally:
        engine.dispose()


def _raw_scalar(engine, sql: str):
    with engine.connect() as conn:
        return conn.execute(__import__('sqlalchemy').text(sql)).scalar()


def list_tables(cfg: dict) -> list:
    """列出库中表/视图名"""
    engine = make_engine(cfg)
    try:
        if cfg.get('db_type') == 'clickhouse':
            rows = _run_with_timeout(_raw_rows, engine, "SHOW TABLES")
            return [r[0] for r in rows]
        from sqlalchemy import inspect
        ins = inspect(engine)
        return list(ins.get_table_names())
    finally:
        engine.dispose()


def _raw_rows(engine, sql: str, limit: int = MAX_ROWS):
    from sqlalchemy import text
    with engine.connect() as conn:
        rs = conn.execute(text(sql))
        cols = list(rs.keys())
        rows = [list(r) for r in rs.fetchmany(limit)]
        return cols, rows


def get_schema(cfg: dict, table: str) -> list:
    """获取表结构：返回 [{'name','type','nullable'}]"""
    engine = make_engine(cfg)
    try:
        if cfg.get('db_type') == 'clickhouse':
            cols, rows = _run_with_timeout(_raw_rows, engine, f"DESCRIBE TABLE {_q_ident(table)}")
            return [{'name': r[0], 'type': r[1], 'nullable': True} for r in rows]
        from sqlalchemy import inspect
        ins = inspect(engine)
        cols = ins.get_columns(table)
        return [{'name': c['name'], 'type': str(c['type']), 'nullable': bool(c.get('nullable'))} for c in cols]
    finally:
        engine.dispose()


def schema_text(cfg: dict, tables: list = None) -> str:
    """把表结构序列化成文本，供 LLM 生成 SQL 使用"""
    engine = make_engine(cfg)
    try:
        names = tables or list_tables(cfg)
        lines = []
        for t in names[:50]:
            cols = get_schema(cfg, t)
            col_str = ', '.join(f"{c['name']} {c['type']}" for c in cols[:30])
            lines.append(f"表 {t}({col_str})")
        return "\n".join(lines)
    finally:
        engine.dispose()


# ==================== 安全只读执行 ====================

def _strip_sql(sql: str) -> str:
    """去除注释、字符串字面量，得到用于关键字判断的骨架"""
    s = re.sub(r"--[^\n]*", " ", sql)
    s = re.sub(r"/\*.*?\*/", " ", s, flags=re.DOTALL)
    s = re.sub(r"'[^']*'", " ", s)
    s = re.sub(r'"[^"]*"', " ", s)
    s = re.sub(r"`[^`]*`", " ", s)
    return s


def validate_readonly_sql(sql: str, allowed_tables: list = None) -> str:
    """
    安全校验：返回清洗后的单条只读 SQL；不合法则抛 ValueError。
    - 第一层（正则）：拒绝多语句、首关键字白名单、全词黑名单扫描
    - 第二层（语义，sql_guard）：sqlparse 语法树解析做二次确认——
      语句类型、危险函数（SLEEP/LOAD_FILE 等）、危险子句（INTO OUTFILE/FOR UPDATE）、
      查询表范围白名单（含子查询）
    """
    if not sql or not sql.strip():
        raise ValueError("SQL 为空")
    stripped = _strip_sql(sql).strip()
    if not stripped:
        raise ValueError("SQL 无有效内容（疑似全注释/全字符串）")
    # 多语句检测：基于骨架（字符串/注释已剥离），去掉末尾分号后仍有分号 → 拒绝
    skeleton = stripped.rstrip().rstrip(';')
    if ';' in skeleton:
        raise ValueError("出于安全考虑，只允许单条查询语句（禁止多语句执行）")
    # 首关键字
    m = re.match(r'^\s*([A-Za-z]+)', stripped)
    if not m:
        raise ValueError("无法识别 SQL 语句")
    first = m.group(1).upper()
    if first not in _READONLY_KEYWORDS:
        raise ValueError(f"只允许只读查询（SELECT/WITH/EXPLAIN/SHOW/DESCRIBE），已拦截: {first}")
    # 全词扫描黑名单（防止 SELECT 后跟子查询写操作等）
    upper = stripped.upper()
    for kw in _BLOCKED_KEYWORDS:
        # 注意 SET 用 "SET " 防误伤 set 开头的单词
        if kw in upper or (kw.strip() and re.search(rf'\b{re.escape(kw.strip())}\b', upper)):
            raise ValueError(f"检测到被禁止的关键字: {kw.strip()}，已拒绝执行")
    # 第二层：语义解析二次确认（权威校验，覆盖注释/子查询/危险函数/表白名单）
    try:
        from sql_guard import check_sql_ast
        check_sql_ast(sql, allowed_tables=allowed_tables)
    except ImportError:
        pass  # sqlparse 未安装时降级为仅正则（保持可用性）
    # 返回原始 SQL（去末尾分号），绝不能用剥离后的骨架 —— 否则引号内内容（如中文别名）会被误删
    return sql.rstrip().rstrip(';').strip()


def _ensure_limit(sql: str) -> str:
    """无 LIMIT 时追加默认 LIMIT（SHOW/EXPLAIN/DESCRIBE 除外）"""
    upper = sql.strip().upper()
    if not (upper.startswith('SELECT') or upper.startswith('WITH')):
        return sql
    # 已含 LIMIT（含子查询里的 LIMIT 也算，保守起见不重复追加）
    if re.search(r'\bLIMIT\s+\d', upper, flags=re.IGNORECASE):
        return sql
    return sql.rstrip().rstrip(';') + f" LIMIT {DEFAULT_LIMIT}"


def _execute(engine, sql: str):
    """实际执行并返回 (columns, rows)；行数受限 MAX_ROWS"""
    return _run_with_timeout(_raw_rows, engine, sql, MAX_ROWS)


def _run_with_timeout(fn, *args, timeout: int = QUERY_TIMEOUT, **kwargs):
    """子线程执行 + 超时终止（Windows 无 SIGALRM，用线程 future）"""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(fn, *args, **kwargs)
        try:
            return fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise TimeoutError(f"查询超时（>{timeout}s），已终止")


def execute_readonly_sql(cfg: dict, sql: str, allowed_tables: list = None):
    """
    安全执行只读 SQL：校验（正则 + 语义二次确认）→ 补 LIMIT → 执行
    → 返回 {'columns','rows','truncated','sql'}
    抛 ValueError(校验失败) / TimeoutError(超时) / Exception(DB 错误)
    """
    clean = validate_readonly_sql(sql, allowed_tables=allowed_tables)
    final_sql = _ensure_limit(clean)
    engine = make_engine(cfg)
    try:
        cols, rows = _execute(engine, final_sql)
        truncated = len(rows) >= MAX_ROWS
        return {'columns': cols, 'rows': rows[:MAX_ROWS], 'truncated': truncated, 'sql': final_sql}
    finally:
        engine.dispose()


def _q_ident(name: str) -> str:
    """标识符安全引用（仅用于表名等，防注入）"""
    if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name or ''):
        return name
    return f'"{name}"'
