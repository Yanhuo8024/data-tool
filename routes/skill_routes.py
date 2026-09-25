# -*- coding: utf-8 -*-
"""Skill 管理路由 — AI 助手技能导入/列表/启停/删除"""
from core_config import exc_response
from flask import Blueprint, request, jsonify
import traceback

from auth import local_guard

skill_bp = Blueprint('skill', __name__)
# 高危面（技能导入/删除影响 Agent 行为）：远程请求必须认证
skill_bp.before_request(local_guard)


@skill_bp.route('/api/skills', methods=['GET'])
def api_skills_list():
    try:
        from skill_manager import list_skills
        return jsonify({"ok": True, "skills": list_skills()})
    except Exception as e:
        return exc_response(e)


@skill_bp.route('/api/skills/import', methods=['POST'])
def api_skills_import():
    """导入 Skill：粘贴 SKILL.md 文本（form: content/name）或上传 zip（file）"""
    try:
        from skill_manager import import_skill_from_md, import_skill_from_zip
        file = request.files.get('file')
        if file:
            data = file.read()
            if data[:4] == b'PK\x03\x04':
                result = import_skill_from_zip(data)
            else:
                result = import_skill_from_md(data.decode('utf-8', errors='replace'))
        else:
            data = request.get_json(silent=True) or request.form
            content = data.get('content') or data.get('md') or ''
            name = data.get('name') or ''
            result = import_skill_from_md(content, name)
        return jsonify(result)
    except Exception as e:
        return exc_response(e)


@skill_bp.route('/api/skills/<name>/toggle', methods=['POST'])
def api_skills_toggle(name):
    try:
        from skill_manager import set_skill_enabled
        body = request.get_json(silent=True) or {}
        enabled = bool(body.get('enabled', True))
        return jsonify(set_skill_enabled(name, enabled))
    except Exception as e:
        return exc_response(e)


@skill_bp.route('/api/skills/<name>', methods=['DELETE'])
def api_skills_delete(name):
    try:
        from skill_manager import delete_skill
        return jsonify(delete_skill(name))
    except Exception as e:
        return exc_response(e)
