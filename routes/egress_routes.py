#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""routes/egress_routes.py — 工具闸门策略接口（数据出境 / 工具副作用）

为什么独立成模块：这两项是"本机数据是否离开本机"与"工具能否动副作用"的安全开关，
属安全面而非助手功能面；且 `ai_assistant_routes.py` 已到 935 行
（架构门禁 R1 的存量基线，禁止继续增长）。

**两个方向正交**（对标 DeepSeek Harness 的 permission 插件层）：
  · 数据出境：东西能不能往**外**发（联网搜索 / 文件样本 / 图片）
  · 工具副作用：工具本身会**动**什么（写盘 / 出站），见 `egress_policy.TOOL_RISK`

注册方式：`routes/__init__.py` 的 `_CORE_REGISTRARS` **同步注册**——懒注册竞态下首批请求
可能 404，而设置面板一打开就会拉这个接口。

策略与优先级见 `egress_policy.py`；外发清单见 `design/DATA_EGRESS.md`。
"""

from flask import Blueprint, jsonify, request

from auth import local_guard

egress_bp = Blueprint('egress', __name__)
egress_bp.before_request(local_guard)


@egress_bp.route('/api/assistant/tool-risk', methods=['GET'])
def api_tool_risk():
    """工具风险清单（GET）。

    返回按副作用等级（readonly / write / external）分组的工具名，以及全局开关状态。
    只读接口 —— 开关本身由环境变量 `ALLOW_TOOL_SIDE_EFFECTS` 控制，不在面板改
    （本层提供**能力**与**可见性**，默认行为刻意不变，避免打断本地自用工作流）。
    """
    try:
        import egress_policy as ep
        return jsonify(ep.tool_risk_snapshot())
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@egress_bp.route('/api/assistant/egress-policy', methods=['GET', 'POST'])
def api_egress_policy():
    """读取/切换数据出境开关。

    GET  → 每项返回 enabled / default / source（env|file|default）/ locked / upstream / when_off
    POST → {"web_search": bool, "file_sample_egress": bool}
    取值优先级：环境变量（部署方强制） > 面板持久化（data/egress_policy.json） > 默认开启；
    被环境变量强制时返回 409 拒绝，避免"运维关了、界面又被打开"。
    """
    try:
        import egress_policy as ep
        if request.method == 'POST':
            data = request.get_json(silent=True) or {}
            applied = {k: ep.set_flag(k, bool(data[k])) for k in ep.FEATURES if k in data}
            if applied and not any(applied.values()):
                snap = ep.snapshot()
                snap.update({'ok': False, 'changed': applied,
                             'error': '该项已由环境变量强制，设置面板无法修改'})
                return jsonify(snap), 409
            snap = ep.snapshot()
            snap['changed'] = applied
            return jsonify(snap)
        return jsonify(ep.snapshot())
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
