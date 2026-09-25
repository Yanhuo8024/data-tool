#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
routes/template_routes.py — 分析模板市场 API
- GET /api/templates  : 模板列表（分析模板 + 报告模板市场，统一出口）
"""
from flask import Blueprint, jsonify

from analysis_templates import ANALYSIS_TEMPLATES


def register_routes(app, limiter):
    bp = Blueprint('template_routes', __name__)

    @bp.route("/api/templates", methods=["GET"])
    def api_templates():
        payload = {
            "ok": True,
            "templates": ANALYSIS_TEMPLATES,
            "total": len(ANALYSIS_TEMPLATES),
        }
        # 合并报告模板市场数据（report_market 的同名路由已移除，避免双重注册）
        try:
            from routes.report_market import BUILTIN_TEMPLATES
            payload["report_templates"] = list(BUILTIN_TEMPLATES.values())
        except Exception:
            pass
        return jsonify(payload)

    app.register_blueprint(bp)
