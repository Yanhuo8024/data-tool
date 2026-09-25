# -*- coding: utf-8 -*-
"""nlq_engine.py 单测：时间解析 / SQL 安全校验 / 规则生成（无 LLM、无 DB）"""
import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nlq_engine import TimeParser, SQLValidator, NLQEngine, SchemaLinker


# ==================== TimeParser ====================

def test_time_parser_today():
    now = datetime(2026, 8, 15, 12, 0)
    tp = TimeParser(current_date=now)
    start, end = tp.parse("今天")
    assert start == end == "2026-08-15"


def test_time_parser_yesterday():
    now = datetime(2026, 8, 15, 12, 0)
    tp = TimeParser(current_date=now)
    start, end = tp.parse("昨天")
    assert start == end == "2026-08-14"


def test_time_parser_last_n_days():
    now = datetime(2026, 8, 15, 12, 0)
    tp = TimeParser(current_date=now)
    start, end = tp.parse("最近7天")
    assert start == "2026-08-09" and end == "2026-08-15"


def test_time_parser_this_month():
    now = datetime(2026, 8, 15, 12, 0)
    tp = TimeParser(current_date=now)
    start, end = tp.parse("本月")
    assert start == "2026-08-01"


def test_time_parser_explicit_date():
    tp = TimeParser()
    start, end = tp.parse("2026-08-01")
    assert start == end == "2026-08-01"


def test_time_parser_fallback():
    now = datetime(2026, 8, 15, 12, 0)
    tp = TimeParser(current_date=now)
    start, end = tp.parse("随便什么")
    assert start == "2026-08-01"  # 回退到本月第一天


# ==================== SQLValidator ====================

def test_validator_accepts_select():
    ok, reason = SQLValidator.validate("SELECT * FROM sales")
    assert ok is True


def test_validator_rejects_non_select():
    ok, reason = SQLValidator.validate("UPDATE sales SET x=1")
    assert ok is False
    assert "只允许 SELECT" in reason


def test_validator_rejects_write_keyword_in_comment():
    ok, _ = SQLValidator.validate("SELECT * FROM sales -- DROP TABLE")
    assert ok is False


def test_validator_rejects_empty():
    ok, reason = SQLValidator.validate("  ")
    assert ok is False


def test_validator_rejects_multi_statement():
    ok, _ = SQLValidator.validate("SELECT * FROM sales; DROP TABLE users")
    assert ok is False


def test_auto_fix_typos_and_limit():
    sql = SQLValidator.auto_fix("SELET * FORM sales WERE x > 1")
    assert sql.startswith("SELECT")
    assert "FROM" in sql and "WHERE" in sql
    assert "LIMIT 100" in sql  # 自动补 LIMIT


# ==================== SchemaLinker / NLQEngine ====================

SCHEMA = {"tables": [{"name": "sales", "columns": [{"name": "amount"}, {"name": "user_id"}, {"name": "date"}]}]}


def test_schema_linker_finds_table():
    linker = SchemaLinker(SCHEMA)
    res = linker.link("查询销售额")
    assert res["tables"] and res["tables"][0]["name"] == "sales"
    assert res["tables"][0]["relevance_score"] > 0


def test_nlq_topn_sql():
    engine = NLQEngine(schema=SCHEMA)
    res = engine.nlq_to_sql("查询销售额前10的用户")
    assert "LIMIT 10" in res["sql"]
    assert res["validation"]["valid"] is True


def test_nlq_aggregate_count():
    engine = NLQEngine(schema=SCHEMA)
    res = engine.nlq_to_sql("统计订单数量")
    assert "COUNT" in res["sql"]


def test_nlq_confidence_present():
    engine = NLQEngine(schema=SCHEMA)
    res = engine.nlq_to_sql("查询销售额")
    assert "confidence" in res
    assert "final" in res["confidence"]
    assert 0 <= res["confidence"]["final"] <= 1
