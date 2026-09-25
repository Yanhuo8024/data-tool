#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""routes/business_routes.py — 业务能力接口（智能运营 / 风控初审 / 能力自检）

为什么独立成模块：`ai_assistant_routes.py` 受架构门禁 R1 约束（存量基线，禁止增长）；
这三项也是"业务能力"而非"对话链路"，独立接口便于外部系统直接调用（无需构造 SSE）。

注册方式：`routes/__init__.py` 的 `_CORE_REGISTRARS` **同步注册**（懒注册竞态下首批
请求可能 404）。

能力实现：
  - 智能运营 → `ops_assistant.try_handle()`（FAQ / 产品品类匹配 / 流程引导）
  - 风控初审 → `risk_review.review()`（确定性规则 + 资料初筛 + 转人工标记）
"""
from flask import Blueprint, jsonify, request

from auth import local_guard

business_bp = Blueprint('business', __name__)
business_bp.before_request(local_guard)


@business_bp.route('/api/assistant/business/capability', methods=['GET'])
def api_business_capability():
    """三项业务能力的运行时实况（自检用：能立刻看出各能力是否真的可用）。"""
    try:
        import ops_assistant
        import risk_review
        faq = ops_assistant.load_faq()
        from ops_catalog import GUIDES, PRODUCT_CATALOG
        rules = ('return_pledge', 'trade_advice', 'missing_disclaimer',
                 'data_incomplete', 'unverified_numbers')
        return jsonify({
            'ok': True,
            'capabilities': [
                {
                    'key': 'ops_agent', 'label': '智能运营 Agent',
                    'channels': ['客户咨询应答(FAQ)', '产品品类匹配', '业务流程引导', '下一步推荐'],
                    'faq_entries': len(faq),
                    'product_categories': len(PRODUCT_CATALOG),
                    'guide_topics': len(GUIDES),
                    'ready': bool(faq),
                },
                {
                    'key': 'risk_review', 'label': '风控初审 Agent',
                    'channels': ['合规红线（收益承诺/荐股）', '免责声明完整性',
                                 '资料完整性初筛', '数字溯源核验'],
                    'rules': list(rules), 'rule_count': len(rules), 'ready': True,
                    'verdicts': ['pass', 'needs_review', 'block'],
                    'handoff': 'needs_review / block 自动标记转人工',
                },
                {
                    'key': 'kb_citation', 'label': '知识库可溯源',
                    'channels': ['条目稳定编号 KB-<分类>-<哈希>', '回答附「📚 依据」块',
                                 '引用编号反向核验（防编造出处）'],
                    'ready': True,
                },
            ],
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@business_bp.route('/api/assistant/business/ops-catalog', methods=['GET'])
def api_ops_catalog():
    """运营应答库与产品目录概览（供前端展示与运营维护）。"""
    try:
        import ops_assistant
        from ops_catalog import PRODUCT_CATALOG
        faq = ops_assistant.load_faq()
        return jsonify({
            'ok': True,
            'faq': [{'id': e.get('id'), 'q': e.get('q'),
                     'category': e.get('category'), 'risk': bool(e.get('risk'))} for e in faq],
            'products': [{'name': p['name'], 'risk': p['risk'],
                          'min_amount': p['min_amount'], 'expected': p['expected']}
                         for p in PRODUCT_CATALOG],
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@business_bp.route('/api/assistant/business/ops', methods=['POST'])
def api_ops_reply():
    """智能运营应答。

    POST {"message": "...", "file": "可选当前文件"}
    命中返回完整回复体（source = ops_faq / ops_product_match / ops_guide）；
    未命中返回 ok=False + matched=False（调用方据此交回主对话链路）。
    """
    try:
        import ops_assistant
        data = request.get_json(silent=True) or {}
        msg = str(data.get('message') or '').strip()
        if not msg:
            return jsonify({'ok': False, 'error': '缺少 message 参数'}), 400
        out = ops_assistant.try_handle(msg, current_file=data.get('file'), fast=True)
        if not out:
            return jsonify({'ok': True, 'matched': False,
                            'note': '未命中运营场景（或属分析类诉求已让路）'})
        out['matched'] = True
        return jsonify(out)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@business_bp.route('/api/assistant/business/risk-review', methods=['POST'])
def api_risk_review():
    """风控初审：对任意文本做确定性合规/资料审查。

    POST {"text": "...", "file": "可选", "dry_run": true 只返回结论不追加正文}
    返回 verdict（pass/needs_review/block）、findings、handoff（是否转人工）。
    """
    try:
        import risk_review
        data = request.get_json(silent=True) or {}
        text = str(data.get('text') or '')
        if not text.strip():
            return jsonify({'ok': False, 'error': '缺少 text 参数'}), 400
        result = risk_review.review(
            text, current_file=data.get('file'),
            file_stats=data.get('file_stats'), fact_stats=data.get('fact_stats'))
        out = dict(result)
        out['ok'] = True
        out['block'] = risk_review.render_review(result) if result['findings'] else ''
        return jsonify(out)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
