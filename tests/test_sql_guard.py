#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_sql_guard.py — SQL 语义安全校验层测试（sql_guard + validate_readonly_sql）
覆盖：DML/DDL 拦截、多语句、注释绕过、危险函数、INTO OUTFILE、FOR UPDATE、表白名单
"""
import pytest

from sql_guard import check_sql_ast, SQLGuardError
from db_connector import validate_readonly_sql


# ---------------- 语句类型（语义级） ----------------

@pytest.mark.parametrize("sql", [
    "DROP TABLE customers",
    "DELETE FROM customers WHERE id=1",
    "UPDATE customers SET a=1",
    "INSERT INTO customers VALUES (1)",
    "ALTER TABLE customers ADD COLUMN x INT",
    "CREATE TABLE evil (id INT)",
    "TRUNCATE TABLE customers",
    "GRANT ALL ON * TO 'x'",
    "BEGIN; SELECT 1",
])
def test_blocked_dml_ddl(sql):
    with pytest.raises((SQLGuardError, ValueError)):
        validate_readonly_sql(sql)


# ---------------- 多语句 ----------------

def test_multi_statement_blocked():
    with pytest.raises((SQLGuardError, ValueError)):
        validate_readonly_sql("SELECT 1; DROP TABLE customers")
    with pytest.raises((SQLGuardError, ValueError)):
        validate_readonly_sql("SELECT 1; SELECT 2")


# ---------------- 注释 / 子查询绕过（语义层专长） ----------------

def test_dangerous_function_in_subquery():
    # 正则层放行，语义层必须拦截（时间盲注最典型）
    with pytest.raises((SQLGuardError, ValueError)):
        validate_readonly_sql("SELECT (SELECT SLEEP(5))")
    with pytest.raises((SQLGuardError, ValueError)):
        validate_readonly_sql("SELECT * FROM t WHERE 1=1 AND SLEEP(5)")


def test_into_outfile_blocked():
    with pytest.raises((SQLGuardError, ValueError)):
        validate_readonly_sql("SELECT * FROM customers INTO OUTFILE '/tmp/x'")


def test_for_update_blocked():
    with pytest.raises((SQLGuardError, ValueError)):
        validate_readonly_sql("SELECT * FROM customers FOR UPDATE")


def test_comment_not_tricking_semantics():
    # 注释里的 DROP 不影响语义，应放行（正则层也能正确处理）
    ok = validate_readonly_sql("SELECT 1 -- DROP TABLE customers")
    assert ok.strip().upper().startswith("SELECT")


# ---------------- 表白名单（含子查询递归） ----------------

def test_allowed_tables_whitelist():
    # 白名单外表 → 拦截
    with pytest.raises((SQLGuardError, ValueError)):
        validate_readonly_sql("SELECT * FROM secret_table", allowed_tables=["customers"])
    # 白名单内表 → 放行
    ok = validate_readonly_sql("SELECT * FROM customers", allowed_tables=["customers", "orders"])
    assert "customers" in ok.lower()


def test_allowed_tables_subquery():
    # 子查询越权表 → 拦截（递归提取）
    with pytest.raises((SQLGuardError, ValueError)):
        validate_readonly_sql(
            "SELECT * FROM orders WHERE cid IN (SELECT id FROM secret_table)",
            allowed_tables=["orders"])


# ---------------- 正常查询放行 ----------------

@pytest.mark.parametrize("sql", [
    "SELECT * FROM customers",
    "SELECT city, COUNT(*) AS cnt FROM customers GROUP BY city ORDER BY cnt DESC",
    "WITH c AS (SELECT id FROM customers) SELECT * FROM c",
    "SHOW TABLES",
    "DESCRIBE customers",
    "EXPLAIN SELECT * FROM customers",
])
def test_normal_select_allowed(sql):
    ok = validate_readonly_sql(sql)
    assert ok


# ---------------- AST 元数据 ----------------

def test_ast_meta():
    meta = check_sql_ast("SELECT city, COUNT(*) AS cnt FROM customers GROUP BY city")
    assert meta["type"] == "SELECT"
    assert "customers" in meta["tables"]
