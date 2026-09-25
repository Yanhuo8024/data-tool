#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
routes/lineage_routes.py — 数据血缘追踪 API
记录数据从哪来、经过哪些处理、被哪些报告引用
"""
from flask import request, jsonify, Blueprint

from data_lineage import lineage_mgr, LINEAGE_OPS


def register_routes(app, limiter):
    bp = Blueprint('lineage_routes', __name__)
    # ============================================================
    #  血缘总览 / 统计
    # ============================================================
    @bp.route("/api/lineage/stats", methods=["GET"])
    def api_lineage_stats():
        return jsonify({"ok": True, "stats": lineage_mgr.stats()})

    # ============================================================
    #  最近事件
    # ============================================================
    @bp.route("/api/lineage/events", methods=["GET"])
    def api_lineage_events():
        try:
            limit = min(int(request.args.get("limit", 50)), 500)
        except (TypeError, ValueError):
            limit = 50
        return jsonify({
            "ok": True,
            "events": lineage_mgr.recent(limit),
            "op_labels": LINEAGE_OPS,
        })

    # ============================================================
    #  按关键词搜索
    # ============================================================
    @bp.route("/api/lineage/search", methods=["GET"])
    def api_lineage_search():
        kw = request.args.get("q", "").strip()
        limit = 100
        try:
            limit = min(int(request.args.get("limit", 100)), 500)
        except (TypeError, ValueError):
            pass
        return jsonify({"ok": True, "events": lineage_mgr.search(kw, limit)})

    # ============================================================
    #  单文件溯源：上游（来源）+ 下游（被引用）
    # ============================================================
    @bp.route("/api/lineage/file", methods=["GET"])
    def api_lineage_file():
        file = request.args.get("file", "").strip()
        if not file:
            return jsonify({"ok": False, "error": "缺少 file 参数"}), 400
        return jsonify({
            "ok": True,
            "file": file,
            "upstream": lineage_mgr.upstream(file),
            "downstream": lineage_mgr.downstream(file),
        })

    # ============================================================
    #  全图（节点 + 边，供前端渲染链路）
    # ============================================================
    @bp.route("/api/lineage/graph", methods=["GET"])
    def api_lineage_graph():
        return jsonify({"ok": True, **lineage_mgr.graph()})
    app.register_blueprint(bp)
