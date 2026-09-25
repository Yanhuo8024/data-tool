#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
routes/plugin_routes.py — 插件系统 API（图表类型 / 分析模型扩展）
- GET  /api/plugins              : 插件列表
- POST /api/plugins/upload       : 上传 .py 插件（校验 PLUGIN_META + 试加载）
- POST /api/plugins/run          : 执行插件（chart→render / model→run）
- POST /api/plugins/delete       : 删除插件
"""
from core_config import exc_response
from flask import Blueprint, request, jsonify

from core_config import OUT_DIR, logger, load_dataframe, os
from auth import local_guard


def register_routes(app, limiter):
    bp = Blueprint('plugin_routes', __name__)
    # 高危面（插件上传/执行/删除）：远程请求必须认证，本地/LAN 匿名回退 admin
    bp.before_request(local_guard)

    @bp.route("/api/plugins", methods=["GET"])
    def api_plugins_list():
        from plugin_manager import list_plugins
        return jsonify({"ok": True, "plugins": list_plugins()})

    @bp.route("/api/plugins/upload", methods=["POST"])
    @limiter.limit("10 per minute")
    def api_plugins_upload():
        try:
            data = request.get_json() or {}
            filename = (data.get("filename") or "").strip()
            content = data.get("content") or ""
            if not filename or not content:
                return jsonify({"ok": False, "error": "缺少 filename/content"}), 400
            from plugin_manager import install_plugin
            name = install_plugin(content, filename)
            return jsonify({"ok": True, "name": name,
                            "message": f"插件 {name} 安装成功（已校验可加载）"})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 400

    @bp.route("/api/plugins/run", methods=["POST"])
    @limiter.limit("20 per minute")
    def api_plugins_run():
        try:
            data = request.get_json() or {}
            name = (data.get("name") or "").strip()
            params = data.get("params") or {}
            if not name:
                return jsonify({"ok": False, "error": "缺少插件名"}), 400

            # 数据来源：file（output 下文件）或 csv（内联文本）
            df = None
            file = (data.get("file") or "").strip()
            csv_text = data.get("csv") or ""
            if file:
                fp = OUT_DIR / file
                if not fp.exists():
                    return jsonify({"ok": False, "error": f"数据文件不存在: {file}"}), 404
                df = load_dataframe(str(fp))
            elif csv_text:
                import io
                import pandas as pd
                df = pd.read_csv(io.StringIO(csv_text))
            if df is None:
                return jsonify({"ok": False, "error": "请提供 file 或 csv 数据"}), 400

            from plugin_manager import run_plugin
            result = run_plugin(name, df, params)
            return jsonify({"ok": True, **result})
        except Exception as e:
            logger.error(f"plugin run error: {e}", exc_info=True)
            return exc_response(e)

    @bp.route("/api/plugins/delete", methods=["POST"])
    @limiter.limit("10 per minute")
    def api_plugins_delete():
        try:
            data = request.get_json() or {}
            name = (data.get("name") or "").strip()
            if not name:
                return jsonify({"ok": False, "error": "缺少插件名"}), 400
            from plugin_manager import delete_plugin
            delete_plugin(name)
            return jsonify({"ok": True, "message": f"插件 {name} 已删除"})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 400

    app.register_blueprint(bp)
