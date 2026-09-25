#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
routes/analytics_routes.py — DuckDB 高性能分析 API（大数据量）
- POST /api/analytics/query     : 对 output 下数据文件执行只读 SQL（sql_guard 校验 + DuckDB 列式执行）
- POST /api/analytics/describe  : DuckDB 快速聚合描述统计（GB 级秒级）
无 DuckDB / 失败时自动降级 pandas。
"""
from core_config import exc_response
from flask import Blueprint, request, jsonify

from auth import local_guard
from core_config import OUT_DIR, UPLOAD_DIR, logger, pd, safe_data_path, safe_path_or_resp
from duckdb_engine import HAS_DUCKDB, query_file, quick_describe


def register_routes(app, limiter):
    bp = Blueprint('analytics_routes', __name__)
    # 高危面（SQL 查询任意文件）：远程请求必须认证
    bp.before_request(local_guard)

    @bp.route("/api/analytics/status", methods=["GET"])
    def api_analytics_status():
        return jsonify({"ok": True, "duckdb": HAS_DUCKDB,
                        "message": "DuckDB 列式引擎可用" if HAS_DUCKDB else "DuckDB 未安装，将使用 pandas 降级"})

    @bp.route("/api/analytics/query", methods=["POST"])
    @limiter.limit("20 per minute")
    def api_analytics_query():
        """高性能 SQL 查询：对文件执行只读聚合/筛选（安全护栏 + DuckDB）"""
        try:
            data = request.get_json() or {}
            file = (data.get("file") or "").strip()
            sql = (data.get("sql") or "").strip()
            if not file or not sql:
                return jsonify({"ok": False, "error": "缺少 file/sql 参数"}), 400
            fp = safe_path_or_resp(file)
            if isinstance(fp, tuple):
                return fp
            if not fp.exists():
                return jsonify({"ok": False, "error": f"文件不存在: {file}"}), 404
            # 安全护栏：语义级只读校验（语句类型/危险函数/多语句/危险子句）
            from db_connector import validate_readonly_sql
            clean = validate_readonly_sql(sql)

            if HAS_DUCKDB:
                df = query_file(str(fp), clean)
                if df is None:
                    return jsonify({"ok": False,
                                    "error": "DuckDB 查询执行失败（请检查 SQL 语法或文件格式）"}), 500
            else:
                # 无 duckdb：仅支持简单整表读取，避免错误结果
                upper = clean.upper()
                if upper.startswith('SELECT * FROM') and ' WHERE ' not in upper and ' GROUP ' not in upper and ' JOIN ' not in upper:
                    df = pd.read_csv(fp, nrows=200000)
                else:
                    return jsonify({"ok": False,
                                    "error": "未安装 duckdb，仅支持简单整表读取（pip install duckdb 后可执行完整 SQL）"}), 400
            return jsonify({
                "ok": True,
                "engine": "duckdb" if HAS_DUCKDB else "pandas",
                "rows": len(df),
                "columns": list(df.columns),
                "data": df.head(200).fillna("").astype(str).to_dict(orient="records"),
            })
        except Exception as e:
            logger.error(f"analytics query error: {e}", exc_info=True)
            return exc_response(e)

    @bp.route("/api/analytics/describe", methods=["POST"])
    @limiter.limit("20 per minute")
    def api_analytics_describe():
        """快速描述统计（DuckDB 聚合，对 GB 级文件秒级返回）"""
        try:
            data = request.get_json() or {}
            file = (data.get("file") or "").strip()
            if not file:
                return jsonify({"ok": False, "error": "缺少 file 参数"}), 400
            fp = safe_path_or_resp(file)
            if isinstance(fp, tuple):
                return fp
            if not fp.exists():
                return jsonify({"ok": False, "error": f"文件不存在: {file}"}), 404

            if HAS_DUCKDB:
                result = quick_describe(str(fp))
                if result:
                    return jsonify({"ok": True, "engine": "duckdb", **result})
            # 降级：pandas 前 20 万行
            df = pd.read_csv(fp, nrows=200000)
            num = df.select_dtypes(include='number').columns.tolist()
            stats = {}
            for c in num:
                s = df[c].describe()
                stats[c] = {"count": int(s['count']), "mean": round(float(s['mean']), 4),
                            "std": round(float(s['std']), 4) if pd.notna(s['std']) else None,
                            "min": round(float(s['min']), 4), "max": round(float(s['max']), 4),
                            "p50": round(float(df[c].median()), 4)}
            return jsonify({"ok": True, "engine": "pandas", "columns": list(df.columns),
                            "numeric": num, "rows": len(df), "stats": stats})
        except Exception as e:
            logger.error(f"analytics describe error: {e}", exc_info=True)
            return exc_response(e)

    app.register_blueprint(bp)

    # ================================================================
    #  /api/sql/* 与 /api/security/validate
    #  历史前端依赖：static/js/system.js（数据库连接 / 自然语言转 SQL）、
    #  templates/tabs/tab-nlq.html（连接 SQLite）、tab-security.html（SQL 安全校验）。
    #  这三个端点此前在后端缺失（前端点击即 404），此处补齐，
    #  能力全部复用既有模块：db_connector / nlq_engine / sql_guard。
    # ================================================================
    sql_bp = Blueprint('sql_routes', __name__)
    # 高危面（连接外部数据库、执行 SQL 语义分析）：远程请求必须认证
    sql_bp.before_request(local_guard)

    def _normalize_db_cfg(data: dict) -> dict:
        """兼容前端两种参数命名（system.js: db/path；tab-nlq: db_type/sqlite_path）"""
        db_type = (data.get("db_type") or data.get("db") or "").strip().lower()
        path = (data.get("sqlite_path") or data.get("path")
                or data.get("database") or "").strip()
        return {
            "db_type": db_type,
            "host": data.get("host") or "127.0.0.1",
            "port": data.get("port"),
            "username": data.get("username") or "",
            "password": data.get("password") or "",
            "database": path if db_type == "sqlite" else (data.get("database") or ""),
        }

    def _safe_sqlite_path(p: str):
        """SQLite 文件必须落在站点数据目录内（统一实现见 db_connector.safe_sqlite_path）"""
        from db_connector import safe_sqlite_path
        return safe_sqlite_path(p)

    @sql_bp.route("/api/sql/connect", methods=["POST"])
    @limiter.limit("10 per minute")
    def api_sql_connect():
        """测试数据库连接并列出表名"""
        data = request.get_json() or {}
        cfg = _normalize_db_cfg(data)
        if not cfg["db_type"]:
            return jsonify({"ok": False, "error": "缺少数据库类型（db_type / db）"}), 400
        if cfg["db_type"] == "sqlite":
            sp = _safe_sqlite_path(cfg["database"])
            if sp is None:
                return jsonify({"ok": False,
                                "error": "SQLite 路径必须位于 output/、uploads/ 或 data/ 目录内"
                                         "（站点主库 app.db 除外）"}), 400
            if not sp.exists():
                return jsonify({"ok": False, "error": f"数据库文件不存在: {sp.name}"}), 404
            cfg["database"] = str(sp)
        try:
            from db_connector import test_connection, list_tables
            test_connection(cfg)
            tables = list_tables(cfg)
            return jsonify({"ok": True, "db_type": cfg["db_type"],
                            "tables": tables,
                            "message": f"连接成功，共 {len(tables)} 张表"})
        except Exception as e:
            logger.warning(f"sql connect failed: {e}")
            return jsonify({"ok": False, "error": str(e)})

    @sql_bp.route("/api/sql/nl_to_sql", methods=["POST"])
    @limiter.limit("20 per minute")
    def api_sql_nl_to_sql():
        """自然语言 → 只读 SQL（真实表结构 + LLM 生成，不执行）"""
        data = request.get_json() or {}
        query = (data.get("query") or data.get("question") or "").strip()
        if not query:
            return jsonify({"ok": False, "error": "缺少 query 参数"}), 400
        try:
            from nlq_answer import answer
            r = answer(query, db_path=data.get("sqlite_path") or data.get("database"),
                       preview=True)
            return jsonify(r)
        except Exception as e:
            logger.error(f"nl_to_sql error: {e}", exc_info=True)
            return exc_response(e)

    @sql_bp.route("/api/security/validate", methods=["POST"])
    @limiter.limit("30 per minute")
    def api_security_validate():
        """SQL 语义安全校验（sqlparse 语法树，返回 {valid, reason}）"""
        data = request.get_json() or {}
        sql = (data.get("sql") or "").strip()
        if not sql:
            return jsonify({"ok": False, "error": "缺少 sql 参数"}), 400
        allowed = data.get("allowed_tables") or None
        try:
            from sql_guard import check_sql_ast, SQLGuardError
            info = check_sql_ast(sql, allowed_tables=allowed)
            return jsonify({"ok": True, "valid": True,
                            "type": info.get("type"),
                            "tables": info.get("tables", []),
                            "functions": info.get("functions", [])})
        except SQLGuardError as e:
            return jsonify({"ok": True, "valid": False, "reason": str(e)})
        except Exception as e:
            logger.error(f"security validate error: {e}", exc_info=True)
            return exc_response(e)

    app.register_blueprint(sql_bp)
