"""Core routes — homepage, file management, downloads, stability, cache, logging"""
from core_config import exc_response
from flask import request, jsonify, render_template, Response, send_file, make_response, Blueprint
from pathlib import Path
from core_config import (
    OUT_DIR, logger, df_cache, paginator, data_limiter, StabilityConfig,
    pd, np, os, glob, json, memory_monitor, validate_upload_file,
    # 注意: HAS_ML_ENHANCED / HAS_FIN_MODELS 不再顶层 import — 它们会触发
    # ml_module (sklearn) / finance_quant (plotly) 立即加载，是冷启动最大瓶颈。
    # 改为在用到时才 lazy import。
    load_dataframe, convert_to_parquet, ASSET_VER, safe_data_path, safe_path_or_resp,
)
from monitoring import Logger
from auth import require_auth_or_api_key_local, local_guard
from io import BytesIO
from data_lineage import lineage_mgr


def register_routes(app, limiter):
    bp = Blueprint('core_routes', __name__)
    # 高危面（流式读文件/转换）：远程请求必须认证
    bp.before_request(local_guard)
    # ==============================
    #  稳定性 API
    # ==============================
    @bp.route("/api/stability/status")
    def api_stability_status():
        """获取系统稳定性状态"""
        status = memory_monitor.get_status()
        return jsonify({
            "ok": True,
            "memory": status,
            "config": {
                "max_memory_mb": StabilityConfig.MAX_MEMORY_MB,
                "max_page_size": StabilityConfig.MAX_PAGE_SIZE,
                "sql_timeout_sec": StabilityConfig.SQL_TIMEOUT_SEC,
            }
        })

    @bp.route("/api/stability/gc", methods=["POST"])
    def api_stability_gc():
        """手动触发垃圾回收"""
        memory_monitor.force_gc()
        return jsonify({"ok": True, "memory": memory_monitor.get_status()})

    # ==============================
    #  分页查询 API
    # ==============================
    @bp.route("/api/paginated/<path:name>")
    def api_paginated_data(name):
        """分页获取数据"""
        page = request.args.get("page", 1, type=int)
        page_size = request.args.get("page_size", 100, type=int)

        # 限制 page_size
        page_size = min(page_size, StabilityConfig.MAX_PAGE_SIZE)

        # 路径遍历防护
        safe_name = os.path.basename(name)
        if safe_name != name or '..' in name:
            return jsonify({"error": "invalid file name"}), 400

        path = OUT_DIR / safe_name
        if not path.exists():
            return jsonify({"error": "file not found"}), 404

        try:
            df = load_dataframe(str(path))
        except Exception as e:
            return jsonify({"error": f"Failed to load file: {str(e)}"}), 500

        # 限制总行数
        df = data_limiter.limit_dataframe_rows(df)

        # 分页
        result = paginator.paginate(df.to_dict("records"), page, page_size)

        return jsonify({
            "ok": True,
            "data": result["data"],
            "pagination": result["pagination"],
        })

    # ==============================
    #  流式数据 API
    # ==============================
    @bp.route("/api/stream/<path:name>")
    def api_stream_data(name):
        """流式获取大数据"""
        path = safe_path_or_resp(name)
        if isinstance(path, tuple):
            return path
        if not path.exists():
            return jsonify({"error": "file not found"}), 404

        def generate():
            # 分块读取大文件
            chunk_size = StabilityConfig.STREAM_CHUNK_SIZE
            for chunk in pd.read_csv(path, chunksize=chunk_size, encoding="utf-8-sig"):
                data = chunk.to_dict("records")
                yield json.dumps(data, ensure_ascii=False) + "\n"

        return Response(generate(), mimetype="application/x-ndjson")

    @bp.route("/api/cache/status")
    @limiter.limit("30 per minute")
    def api_cache_status():
        """DataFrame 缓存状态（调试用）"""
        return jsonify(df_cache.stats())

    @bp.route("/api/cache/clear", methods=["POST"])
    @limiter.limit("10 per minute")
    @require_auth_or_api_key_local
    def api_cache_clear():
        """清空 DataFrame 缓存"""
        df_cache.clear()
        return jsonify({"ok": True, "message": "Cache cleared"})

    @bp.route("/api/file/convert_parquet", methods=["POST"])
    @limiter.limit("5 per minute")
    @require_auth_or_api_key_local
    def api_convert_parquet():
        """将文件转换为 Parquet 格式加速加载"""
        data = request.json
        path = safe_path_or_resp(data["file"])
        if isinstance(path, tuple):
            return path
        if not path.exists():
            return jsonify({"error": "file not found"}), 404
        try:
            parquet_path = convert_to_parquet(str(path))
            return jsonify({"ok": True, "parquet": Path(parquet_path).name})
        except Exception as e:
            return exc_response(e)

    @bp.route("/")
    def index():
        resp = make_response(render_template("index.html"))
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp

    @bp.route("/api/version")
    def api_version():
        """前端资源版本号（供版本检测 / 自动更新提示）"""
        import time as _t
        return jsonify({"ok": True, "version": ASSET_VER, "check_at": int(_t.time())})

    @bp.route("/api/dashboard")
    def api_dashboard():
        """仪表盘首页数据"""
        import os as _os, glob as _g

        # 最近文件
        files = []
        csv_files = sorted(_g.glob(str(OUT_DIR / "*.csv")), key=_os.path.getmtime, reverse=True)
        xlsx_files = sorted(_g.glob(str(OUT_DIR / "*.xlsx")), key=_os.path.getmtime, reverse=True)
        all_files = csv_files + xlsx_files
        all_files.sort(key=_os.path.getmtime, reverse=True)

        for f in all_files[:8]:
            p = Path(f)
            if p.name.startswith(".deleted_"):
                continue
            stat = _os.stat(f)
            size = stat.st_size
            if size > 1_000_000:
                size_str = f"{size/1_000_000:.1f}MB"
            elif size > 1000:
                size_str = f"{size/1000:.0f}KB"
            else:
                size_str = f"{size}B"
            files.append({
                "name": p.name,
                "size": size_str,
                "modified": stat.st_mtime,
            })

        # 已保存模型（用惰性取用 API：占位符快照会让 from-import 拿到永久 False，故不可
        # 直接 `from core_config import HAS_ML_ENHANCED`）
        models = []
        from core_config import has_ml_enhanced, has_fin_models
        if has_ml_enhanced():
            try:
                ml_models = _g.glob(str(OUT_DIR / "*.pkl")) + _g.glob(str(OUT_DIR / "*.model"))
                for m in ml_models[-5:]:
                    models.append({"name": Path(m).name, "type": "ML模型"})
            except Exception:
                pass
        if has_fin_models():
            try:
                fin_models = _g.glob("models/*.pkl")
                for m in fin_models[-5:]:
                    models.append({"name": Path(m).name, "type": "金融模型"})
            except Exception:
                pass

        return jsonify({
            "ok": True,
            "files": files,
            "file_count": len(files),
            "models": models,
            "model_count": len(models),
        })

    @bp.route("/api/upload", methods=["POST"])
    @limiter.limit("10 per minute")
    @require_auth_or_api_key_local
    def api_upload():
        """ CSV """
        try:
            if "file" not in request.files:
                return jsonify({"error": "no file", "keys": list(request.files.keys())}), 400
            f = request.files["file"]
            if not f or not f.filename:
                return jsonify({"error": "empty filename"}), 400
            fname = f.filename.strip()

            # Safe filename + content validation
            content = f.read()
            safe_name = "".join(c for c in fname if c.isalnum() or c in "._- ").strip()
            if not safe_name:
                safe_name = "uploaded_file"

            # Security validation
            is_safe, err_msg = validate_upload_file(safe_name, content)
            if not is_safe:
                return jsonify({"error": err_msg}), 400

            # Seek back after read
            f = BytesIO(content)

            try:
                if safe_name.lower().endswith(".csv"):
                    #  utf-8 gbk
                    try:
                        df = pd.read_csv(f, encoding='utf-8-sig')
                    except UnicodeDecodeError:
                        f.seek(0)
                        df = pd.read_csv(f, encoding='gbk')
                    save_path = OUT_DIR / safe_name
                    df.to_csv(save_path, index=False, encoding='utf-8-sig')
                elif safe_name.lower().endswith(('.xlsx', '.xls')):
                    df = pd.read_excel(f)
                    save_path = OUT_DIR / safe_name
                    df.to_excel(save_path, index=False)
                else:
                    return jsonify({"error": "不支持的文件格式"}), 400

                # 清除该文件的缓存（如果有）
                df_cache.invalidate(str(save_path))
                # P1-1：工具结果缓存此前无任何失效点 —— 上传用原文件名落盘（同名覆盖是常态），
                # 若不失效，describe/overview/columns/head 等工具在 TTL 内会返回**上一版文件**
                # 的结构与统计量，导致"用旧结构分析新数据"的自相矛盾结论。
                try:
                    import ai_agent as _ai_agent
                    _n = _ai_agent.tool_cache.invalidate_file(safe_name)
                    if _n:
                        logger.info("上传 %s：已失效 %d 条工具缓存", safe_name, _n)
                except Exception as _ce:
                    logger.debug("工具缓存失效跳过: %s", _ce)
                # 血缘：记录上传来源
                try:
                    lineage_mgr.record("upload", "", safe_name,
                                       detail=f"{len(df)}行 × {len(df.columns)}列")
                except Exception:
                    pass

                return jsonify({"ok": True, "file": safe_name, "rows": len(df), "cols": list(df.columns)})
            except Exception as e:
                return jsonify({"error": f"解析失败: {str(e)}"}), 400
        except Exception as e:
            logger.error(f"Upload error: {str(e)}", exc_info=True)
            return exc_response(e)

    # ============================================================
    #  Settings API

    @bp.route("/api/log/frontend", methods=["POST"])
    def api_log_frontend():
        """前端错误上报"""
        d = request.json
        Logger.error(
            source="frontend",
            error_type=d.get("type", "js_error"),
            message=d.get("message", ""),
            stack=d.get("stack", ""),
            url=d.get("url", ""),
            ua=d.get("userAgent", "")
        )
        return jsonify({"ok": True})

    @bp.route("/api/download/<name>")
    @require_auth_or_api_key_local
    def api_download(name):
        # 路径遍历防护
        safe_name = os.path.basename(name)
        if safe_name != name or '..' in name:
            return "invalid file name", 400
        p = OUT_DIR / safe_name
        if p.exists():
            return send_file(str(p), as_attachment=True)
        return "not found", 404
    app.register_blueprint(bp)
