#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
report_market.py - 报告模板市场

内置模板 + API 路由
"""

from core_config import exc_response, OUT_DIR
from pathlib import Path
from typing import Dict, Any, List
from datetime import datetime
from flask import Blueprint, jsonify, request
from auth import local_guard

report_market_bp = Blueprint('report_market', __name__)
# 高危面（模板生成报告读文件）：远程请求必须认证
report_market_bp.before_request(local_guard)

# ==================== 内置报告模板 ====================

BUILTIN_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "data_exploration": {
        "id": "data_exploration",
        "name": "数据探索报告",
        "name_en": "Data Exploration",
        "description": "快速了解数据分布、缺失值、异常值和数据质量",
        "icon": "📊",
        "category": "通用",
        "author": "System",
        "version": "1.0",
        "builtin": True,
    },
    "financial_analysis": {
        "id": "financial_analysis",
        "name": "财务分析报告",
        "name_en": "Financial Analysis",
        "description": "利润表/资产负债表/现金流量表分析，含杜邦分析",
        "icon": "💰",
        "category": "金融",
        "author": "System",
        "version": "1.0",
        "builtin": True,
    },
    "ml_model_report": {
        "id": "ml_model_report",
        "name": "机器学习报告",
        "name_en": "ML Model Report",
        "description": "模型训练/评估/特征重要性/混淆矩阵",
        "icon": "🤖",
        "category": "AI",
        "author": "System",
        "version": "1.0",
        "builtin": True,
    },
    "timeseries_report": {
        "id": "timeseries_report",
        "name": "时序分析报告",
        "name_en": "Time Series Report",
        "description": "趋势分解/季节性/平稳性检验/预测结果",
        "icon": "📈",
        "category": "通用",
        "author": "System",
        "version": "1.0",
        "builtin": True,
    },
    "stock_analysis": {
        "id": "stock_analysis",
        "name": "个股分析报告",
        "name_en": "Stock Analysis",
        "description": "K线分析/技术指标/资金流向/筹码分布",
        "icon": "📉",
        "category": "金融",
        "author": "System",
        "version": "1.0",
        "builtin": True,
    },
    "hypothesis_test": {
        "id": "hypothesis_test",
        "name": "假设检验报告",
        "name_en": "Hypothesis Testing",
        "description": "T检验/ANOVA/卡方检验/非参数检验",
        "icon": "🔬",
        "category": "统计",
        "author": "System",
        "version": "1.0",
        "builtin": True,
    },
}


# ==================== API 路由 ====================
# 注：GET /api/templates 由 template_routes 统一提供（避免双重注册冲突），
# 此处仅保留模板详情与使用接口。


@report_market_bp.route('/api/templates/<template_id>', methods=['GET'])
def api_get_template(template_id):
    """获取模板详情"""
    template = BUILTIN_TEMPLATES.get(template_id)
    if not template:
        return jsonify({'ok': False, 'error': '模板不存在'}), 404
    return jsonify({'ok': True, 'data': template})


@report_market_bp.route('/api/templates/use', methods=['POST'])
def api_use_template():
    """使用模板生成报告"""
    data = request.get_json()
    if not data or 'file' not in data:
        return jsonify({'ok': False, 'error': '缺少 file 参数'}), 400

    template_id = data.get('template_id', 'data_exploration')
    file_path = data['file']

    if template_id not in BUILTIN_TEMPLATES:
        return jsonify({'ok': False, 'error': f'模板 {template_id} 不存在'}), 404

    # 安全检查：路径必须在 output/ 内
    from core_config import safe_data_path, safe_path_or_resp
    full_path = safe_path_or_resp(file_path)
    if isinstance(full_path, tuple):
        return full_path
    if not full_path.exists():
        return jsonify({'ok': False, 'error': f'文件 {file_path} 不存在'}), 404
    
    try:
        from report_generator import generate_report as _gen
        html_content, meta = _gen(str(full_path), template_id, "html")
        
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"report_{template_id}_{ts}.html"
        output_path = OUT_DIR / filename
        
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(html_content)
        
        return jsonify({'ok': True, 'file': filename, 'meta': meta})
    except Exception as e:
        return exc_response(e)
