#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
llm_routes.py - LLM 配置和测试路由
"""

from core_config import exc_response
from flask import Blueprint, request, jsonify
from auth import local_guard
from llm_client import chat, test_connection

llm_bp = Blueprint('llm', __name__)
# LLM 调用消耗 API 额度：远程请求必须认证，本地/LAN 匿名回退 admin
llm_bp.before_request(local_guard)


@llm_bp.route('/api/llm/test', methods=['POST'])
def api_llm_test():
    """测试 LLM 连接"""
    config = request.get_json()
    # P1-2 安全：与 /api/llm/chat 一致，走同一 provider 白名单 + URL 清洗
    # （否则 provider=custom + 任意 apiUrl 构成 SSRF）
    from routes.ai_assistant_routes import _sanitize_llm_config
    try:
        config = _sanitize_llm_config(config)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    result = test_connection(config)
    if result.get("ok"):
        return jsonify(result)
    return jsonify(result), 400


@llm_bp.route('/api/llm/chat', methods=['POST'])
def api_llm_chat():
    """LLM 直接对话（支持 function calling：前端 Agent 模式依赖 tools 透传）"""
    data = request.get_json()
    messages = data.get("messages", [])
    config = data.get("config", None)
    tools = data.get("tools")
    tool_choice = data.get("tool_choice", "auto")

    # 服务端配置白名单清洗（与 ai_assistant_routes 一致）
    from routes.ai_assistant_routes import _sanitize_llm_config
    config = _sanitize_llm_config(config)

    try:
        result = chat(messages, config, tools=tools, tool_choice=tool_choice)
        # 兼容两种调用方：
        #  - /api/llm/chat 直接使用方期望 {content, tool_calls} 顶层结构
        #  - 保留 reply 字段兼容旧前端
        if isinstance(result, dict):
            return jsonify({"ok": True, "reply": result, **result})
        return jsonify({"ok": True, "reply": result, "content": result})
    except Exception as e:
        return exc_response(e)
