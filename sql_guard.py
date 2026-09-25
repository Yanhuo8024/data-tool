#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sql_guard.py — SQL 语义安全校验层（基于 sqlparse 语法树，二次确认）

背景：仅靠正则关键字黑名单可被注释/大小写/子查询等手法绕过。
本模块对 SQL 做真正的语法树解析（AST），从结构上确认：
  1. 语句类型（stmt.get_type()）——只允许只读类型；
  2. 多语句检测——分号拼接一律拒绝；
  3. 危险函数调用（SLEEP/BENCHMARK/LOAD_FILE 等时间盲注/文件读取）——拒绝；
  4. 危险子句（INTO OUTFILE/DUMPFILE、FOR UPDATE/SHARE）——拒绝；
  5. 查询表范围——可传 allowed_tables 白名单，越权表拒绝（含子查询递归提取）。

用法：check_sql_ast(sql, allowed_tables=None) -> {"type","tables","functions"}，不合法抛 ValueError。
"""
import sqlparse
from sqlparse.sql import Identifier, IdentifierList, Function, Parenthesis
from sqlparse.tokens import Keyword

# 只读语句类型（与 db_connector 一致）
READONLY_TYPES = ('SELECT', 'WITH', 'EXPLAIN', 'SHOW', 'DESCRIBE', 'DESC', 'PRAGMA')
# 绝对禁止的语句类型（无论出现在主句还是子句）
BLOCKED_TYPES = (
    'INSERT', 'UPDATE', 'DELETE', 'DROP', 'ALTER', 'CREATE', 'TRUNCATE',
    'GRANT', 'REVOKE', 'CALL', 'EXEC', 'EXECUTE', 'MERGE', 'REPLACE',
    'ATTACH', 'DETACH', 'VACUUM', 'COMMIT', 'ROLLBACK', 'BEGIN', 'SET',
    'LOAD', 'COPY', 'IMPORT', 'EXPORT', 'ANALYZE', 'REINDEX', 'SECURITY',
)

# 危险函数（时间盲注 / 文件读取 / 报错注入 / 锁等待）
DANGEROUS_FUNCS = (
    'sleep', 'pg_sleep', 'benchmark', 'load_file', 'get_lock',
    'release_lock', 'extractvalue', 'updatexml', 'gtid_subset', 'gtid_subtract',
)

# 表名前置关键字（其后应跟表标识符）
_TABLE_PRECEDERS = (
    'FROM', 'JOIN', 'INNER JOIN', 'LEFT JOIN', 'RIGHT JOIN', 'FULL JOIN',
    'CROSS JOIN', 'NATURAL JOIN', 'STRAIGHT_JOIN', 'INTO', 'TABLE',
    'UPDATE', 'DELETE', 'TRUNCATE', 'DROP TABLE', 'RENAME TABLE',
)


class SQLGuardError(ValueError):
    """SQL 校验失败"""
    pass


def _tok_upper(tok) -> str:
    return str(tok.value).strip().upper()


def _is_skip(tok) -> bool:
    """是否空白/注释 token（兼容裸 Token 与结构化 token）"""
    if getattr(tok, 'is_whitespace', False) or getattr(tok, 'is_comment', False):
        return True
    tt = getattr(tok, 'ttype', None)
    if tt is None:
        return False
    try:
        from sqlparse.tokens import Whitespace, Comment
        if tt in (Whitespace,) or tt[0] in (Whitespace, Comment):
            return True
    except Exception:
        pass
    return False


def _join_kw(tok) -> bool:
    """是否为 JOIN 系关键字（含 'JOIN' 或为 FROM/INTO/TABLE 等）"""
    kw = _tok_upper(tok)
    if kw == 'FROM' or kw == 'INTO' or kw == 'TABLE':
        return True
    if kw == 'JOIN' or kw.endswith(' JOIN') or kw == 'STRAIGHT_JOIN':
        return True
    return False


def _id_name(id_) -> str:
    """取标识符主名（去引号/去别名/去 schema 前缀保留表名）"""
    name = ''
    try:
        name = id_.get_name() or str(id_)
    except Exception:
        name = str(id_)
    return (name or '').strip().strip('`"[]')


def _extract_tables(node) -> list:
    """结构化递归提取语句涉及的表名（覆盖子查询/WHERE 内层）"""
    tables = []
    pending = False
    for tok in getattr(node, 'tokens', []) or []:
        if _is_skip(tok):
            continue
        cur = _tok_upper(tok)
        # 表前置关键字（FROM/JOIN/INTO/TABLE/UPDATE/DELETE…）
        if cur in _TABLE_PRECEDERS:
            pending = True
            continue
        if pending and isinstance(tok, Identifier):
            tables.append(_id_name(tok))
            pending = False
            continue
        if pending and isinstance(tok, IdentifierList):
            for id_ in tok.get_identifiers():
                tables.append(_id_name(id_))
            pending = False
            continue
        # 递归所有结构化容器（Where/Parenthesis/Group/Comparison…）
        if hasattr(tok, 'tokens'):
            tables += _extract_tables(tok)
        pending = False
    return [t for t in tables if t]


def _extract_functions(node) -> list:
    """结构化递归提取全部函数调用名（覆盖子查询）"""
    funcs = []
    for tok in getattr(node, 'tokens', []) or []:
        if _is_skip(tok):
            continue
        if isinstance(tok, Function):
            try:
                name = (tok.get_real_name() or '').lower()
            except Exception:
                name = str(tok.value).split('(')[0].strip().lower()
            if name:
                funcs.append(name)
        if hasattr(tok, 'tokens'):
            funcs += _extract_functions(tok)
    return funcs


def _has_dangerous_pattern(node) -> str:
    """检测危险子句：INTO OUTFILE/DUMPFILE、FOR UPDATE/SHARE"""
    flat = [(tok.ttype, _tok_upper(tok)) for tok in node.flatten() if not _is_skip(tok)]
    for i in range(len(flat) - 1):
        ttype, val = flat[i]
        if ttype in (Keyword, Keyword.DML, Keyword.DDL) or True:
            if val == 'INTO' and i + 1 < len(flat) and flat[i + 1][1] in ('OUTFILE', 'DUMPFILE'):
                return f"禁止 INTO {flat[i + 1][1]}（文件写入）"
            if val == 'FOR' and i + 1 < len(flat) and flat[i + 1][1] in ('UPDATE', 'SHARE'):
                return f"禁止 FOR {flat[i + 1][1]}（行锁）"
    return ''


def check_sql_ast(sql: str, allowed_tables: list = None) -> dict:
    """
    语义级校验（二次确认）。不合法抛 SQLGuardError。
    返回 {"type": str, "tables": list, "functions": list}
    """
    if not sql or not sql.strip():
        raise SQLGuardError("SQL 为空")

    statements = sqlparse.parse(sql)
    if not statements:
        raise SQLGuardError("SQL 解析失败，无法确认语义")

    # 多语句：解析出 >1 条，或单条内仍有顶层分号拼接
    if len(statements) > 1:
        raise SQLGuardError(f"禁止多语句执行（解析出 {len(statements)} 条语句）")

    stmt = statements[0]
    stype = (stmt.get_type() or 'UNKNOWN').upper()

    # 语句类型白名单（语义级，替代首关键字正则）
    if stype in BLOCKED_TYPES:
        raise SQLGuardError(f"检测到被禁止的语句类型: {stype}")
    if stype not in READONLY_TYPES and stype != 'UNKNOWN':
        raise SQLGuardError(f"只允许只读查询（{'/'.join(READONLY_TYPES)}），已拦截: {stype}")
    if stype == 'UNKNOWN':
        # 无法识别类型：检查是否藏有 DML/DDL 关键字
        upper = sql.upper()
        for b in BLOCKED_TYPES:
            if b != 'SET' and re_search_word(upper, b):
                raise SQLGuardError(f"无法确认语句类型，且检测到被禁止的关键字: {b}")

    # 危险子句（INTO OUTFILE / FOR UPDATE）
    bad_pattern = _has_dangerous_pattern(stmt)
    if bad_pattern:
        raise SQLGuardError(bad_pattern)

    # 危险函数
    funcs = _extract_functions(stmt)
    for f in funcs:
        if f in DANGEROUS_FUNCS:
            raise SQLGuardError(f"检测到危险函数调用: {f}（可能用于注入/文件读取/锁等待）")

    # 表范围白名单（含子查询）
    tables = _extract_tables(stmt)
    if allowed_tables:
        allow = {t.strip().lower() for t in allowed_tables if t}
        for t in tables:
            tl = t.lower()
            # 允许 schema.table 形式中 table 段命中
            bare = tl.split('.')[-1]
            if bare not in allow and tl not in allow:
                raise SQLGuardError(f"查询涉及白名单之外的表: {t}")

    return {"type": stype, "tables": list(dict.fromkeys(tables)),
            "functions": list(dict.fromkeys(funcs))}


def re_search_word(text: str, word: str) -> bool:
    """全词匹配（用于 UNKNOWN 类型的兜底关键字检测）"""
    import re
    return re.search(rf'\b{re.escape(word)}\b', text) is not None
