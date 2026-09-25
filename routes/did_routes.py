#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
did_routes.py - DID（双重差分）计量经济学分析 API 路由
"""

import os
import sys
import json
import traceback
from flask import Blueprint, request, jsonify
import pandas as pd
import numpy as np

# 获取项目根目录
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core_config import OUT_DIR, load_dataframe
from auth import local_guard
from did import (
    clean_data,
    descriptive_stats,
    correlation_matrix,
    did_regression,
    stepwise_regression,
    standardized_regression,
    parallel_trends,
    placebo_test,
    subsample_regression,
    heterogeneity_analysis,
    mediation_analysis,
    interaction_analysis,
    format_regression_table,
    time_trend_data,
    run_full_did_analysis,
    _build_autonomous_report_html,
    _build_correlation_html,
    _build_group_html
)

did_bp = Blueprint('did', __name__)
# 分析接口消耗本地算力：远程请求必须认证，本地/LAN 匿名回退 admin
did_bp.before_request(local_guard)


def _load_file(file_name):
    """安全加载数据文件"""
    if not file_name:
        return None, "缺少 file 参数"
    
    try:
        path = OUT_DIR / file_name
        if not path.exists():
            return None, f"文件不存在: {file_name}"
        
        df = load_dataframe(str(path))
        if df is None or df.empty:
            return None, "文件为空或读取失败"
        
        return df, None
    except Exception as e:
        return None, f"读取失败: {str(e)}"


# ============================================================
# 1. 数据清洗
# ============================================================
@did_bp.route('/api/did/clean', methods=['POST'])
def api_did_clean():
    """数据清洗"""
    data = request.get_json()
    df, err = _load_file(data.get('file'))
    if err:
        return jsonify({'ok': False, 'error': err}), 400
    
    consume_cols = data.get('consume_cols', None)
    treat_col = data.get('treat_col', 'treat')
    time_col = data.get('time_col', 'year')
    
    result = clean_data(df, consume_cols, treat_col, time_col)
    result['cleaned_df'] = result['cleaned_df'].head(10).to_html(classes='table table-sm', index=False)
    
    return jsonify({'ok': True, **{k: v for k, v in result.items() if k != 'cleaned_df'}, 'preview': result['cleaned_df']})


# ============================================================
# 2. 描述性统计
# ============================================================
@did_bp.route('/api/did/descriptive', methods=['POST'])
def api_did_descriptive():
    """描述性统计"""
    data = request.get_json()
    df, err = _load_file(data.get('file'))
    if err:
        return jsonify({'ok': False, 'error': err}), 400
    
    group_col = data.get('group_col', None)
    vars_list = data.get('vars', None)
    
    stats = descriptive_stats(df, group_col, vars_list)
    return jsonify({'ok': True, 'html': stats.to_html(classes='table table-sm'), 'data': stats.to_dict()})


# ============================================================
# 3. 相关性矩阵
# ============================================================
@did_bp.route('/api/did/correlation', methods=['POST'])
def api_did_correlation():
    """相关性矩阵"""
    data = request.get_json()
    df, err = _load_file(data.get('file'))
    if err:
        return jsonify({'ok': False, 'error': err}), 400
    
    vars_list = data.get('vars', None)
    method = data.get('method', 'pearson')
    
    corr = correlation_matrix(df, vars_list, method)
    return jsonify({'ok': True, 'html': corr.to_html(classes='table table-sm'), 'data': corr.to_dict()})


# ============================================================
# 4. DID 基准回归
# ============================================================
@did_bp.route('/api/did/regression', methods=['POST'])
def api_did_regression():
    """DID 基准回归"""
    data = request.get_json()
    df, err = _load_file(data.get('file'))
    if err:
        return jsonify({'ok': False, 'error': err}), 400
    
    dep_var = data.get('dep_var')
    if not dep_var:
        return jsonify({'ok': False, 'error': '缺少 dep_var（被解释变量）'}), 400
    
    treat_col = data.get('treat_col', 'treat')
    time_col = data.get('time_col', 'year')
    unit_col = data.get('unit_col', 'province')
    control_vars = data.get('control_vars', [])
    cluster = data.get('cluster', True)
    
    result = did_regression(df, dep_var, treat_col, time_col, unit_col, control_vars, cluster,
                            post_col=data.get('post_col'), policy_year=data.get('policy_year'))
    
    if 'error' in result:
        return jsonify({'ok': False, 'error': result['error']}), 400
    
    return jsonify({
        'ok': True,
        'summary': result['summary'],
        'params': result['params'],
        'se': result['se'],
        'tvalues': result['tvalues'],
        'pvalues': result['pvalues'],
        'r2': result['r2'],
        'n_obs': result['n_obs']
    })


# ============================================================
# 5. 逐步回归
# ============================================================
@did_bp.route('/api/did/stepwise', methods=['POST'])
def api_did_stepwise():
    """逐步加入控制变量回归"""
    data = request.get_json()
    df, err = _load_file(data.get('file'))
    if err:
        return jsonify({'ok': False, 'error': err}), 400
    
    dep_var = data.get('dep_var')
    if not dep_var:
        return jsonify({'ok': False, 'error': '缺少 dep_var'}), 400
    
    treat_col = data.get('treat_col', 'treat')
    time_col = data.get('time_col', 'year')
    unit_col = data.get('unit_col', 'province')
    control_groups = data.get('control_groups', {})
    
    results = stepwise_regression(df, dep_var, treat_col, time_col, unit_col, control_groups)
    
    # 格式化表格
    table_html = format_regression_table(results, f'{dep_var} 逐步回归结果')
    
    return jsonify({
        'ok': True,
        'table_html': table_html,
        'n_models': len(results)
    })


# ============================================================
# 6. 标准化系数回归
# ============================================================
@did_bp.route('/api/did/standardized', methods=['POST'])
def api_did_standardized():
    """标准化系数回归"""
    data = request.get_json()
    df, err = _load_file(data.get('file'))
    if err:
        return jsonify({'ok': False, 'error': err}), 400
    
    dep_var = data.get('dep_var')
    if not dep_var:
        return jsonify({'ok': False, 'error': '缺少 dep_var'}), 400
    
    treat_col = data.get('treat_col', 'treat')
    time_col = data.get('time_col', 'year')
    unit_col = data.get('unit_col', 'province')
    control_vars = data.get('control_vars', [])
    
    result = standardized_regression(df, dep_var, treat_col, time_col, unit_col, control_vars)
    
    if 'error' in result:
        return jsonify({'ok': False, 'error': result['error']}), 400
    
    return jsonify({
        'ok': True,
        'params': result['params'],
        'se': result['se'],
        'pvalues': result['pvalues'],
        'r2': result['r2'],
        'summary': result['summary']
    })


# ============================================================
# 7. 平行趋势检验
# ============================================================
@did_bp.route('/api/did/parallel-trends', methods=['POST'])
def api_did_parallel_trends():
    """平行趋势检验（Event Study）"""
    data = request.get_json()
    df, err = _load_file(data.get('file'))
    if err:
        return jsonify({'ok': False, 'error': err}), 400
    
    dep_var = data.get('dep_var')
    if not dep_var:
        return jsonify({'ok': False, 'error': '缺少 dep_var'}), 400
    
    treat_col = data.get('treat_col', 'treat')
    time_col = data.get('time_col', 'year')
    unit_col = data.get('unit_col', 'province')
    control_vars = data.get('control_vars', [])
    pre_periods = data.get('pre_periods', 5)
    post_periods = data.get('post_periods', 5)
    
    result = parallel_trends(df, dep_var, treat_col, time_col, unit_col, control_vars, pre_periods, post_periods)
    
    if 'error' in result:
        return jsonify({'ok': False, 'error': result['error']}), 400
    
    coef_data = json.loads(result['coef_df'].to_json(orient='records', date_format='iso'))
    
    return jsonify({
        'ok': True,
        'coef_data': coef_data,
        'policy_year': result['policy_year'],
        'passed': result['passed']
    })


# ============================================================
# 8. 安慰剂检验
# ============================================================
@did_bp.route('/api/did/placebo', methods=['POST'])
def api_did_placebo():
    """安慰剂检验"""
    data = request.get_json()
    df, err = _load_file(data.get('file'))
    if err:
        return jsonify({'ok': False, 'error': err}), 400
    
    dep_var = data.get('dep_var')
    if not dep_var:
        return jsonify({'ok': False, 'error': '缺少 dep_var'}), 400
    
    treat_col = data.get('treat_col', 'treat')
    time_col = data.get('time_col', 'year')
    unit_col = data.get('unit_col', 'province')
    control_vars = data.get('control_vars', [])
    n_placebo = data.get('n_placebo', 200)
    
    result = placebo_test(df, dep_var, treat_col, time_col, unit_col, control_vars, n_placebo)
    
    return jsonify({
        'ok': True,
        'true_coef': result['true_coef'],
        'p_value': result['p_value'],
        'passed': result['passed'],
        'n_placebo': result['n_placebo']
    })


# ============================================================
# 9. 子样本回归
# ============================================================
@did_bp.route('/api/did/subsample', methods=['POST'])
def api_did_subsample():
    """子样本回归"""
    data = request.get_json()
    df, err = _load_file(data.get('file'))
    if err:
        return jsonify({'ok': False, 'error': err}), 400
    
    dep_var = data.get('dep_var')
    if not dep_var:
        return jsonify({'ok': False, 'error': '缺少 dep_var'}), 400
    
    treat_col = data.get('treat_col', 'treat')
    time_col = data.get('time_col', 'year')
    unit_col = data.get('unit_col', 'province')
    control_vars = data.get('control_vars', [])
    exclude_units = data.get('exclude_units', [])
    include_units = data.get('include_units', [])
    
    result = subsample_regression(df, dep_var, treat_col, time_col, unit_col, control_vars, exclude_units, include_units)
    
    if 'error' in result:
        return jsonify({'ok': False, 'error': result['error']}), 400
    
    return jsonify({
        'ok': True,
        'params': result['params'],
        'se': result['se'],
        'pvalues': result['pvalues'],
        'r2': result['r2'],
        'n_obs': result['n_obs'],
        'n_units': result.get('n_units', '-')
    })


# ============================================================
# 10. 异质性分析
# ============================================================
@did_bp.route('/api/did/heterogeneity', methods=['POST'])
def api_did_heterogeneity():
    """异质性分析"""
    data = request.get_json()
    df, err = _load_file(data.get('file'))
    if err:
        return jsonify({'ok': False, 'error': err}), 400
    
    dep_var = data.get('dep_var')
    if not dep_var:
        return jsonify({'ok': False, 'error': '缺少 dep_var'}), 400
    
    treat_col = data.get('treat_col', 'treat')
    time_col = data.get('time_col', 'year')
    unit_col = data.get('unit_col', 'province')
    control_vars = data.get('control_vars', [])
    hetero_var = data.get('hetero_var')
    split_method = data.get('split_method', 'median')
    
    if not hetero_var:
        return jsonify({'ok': False, 'error': '缺少 hetero_var（异质性变量）'}), 400
    
    result = heterogeneity_analysis(df, dep_var, treat_col, time_col, unit_col, control_vars, hetero_var, split_method)
    
    if 'error' in result:
        return jsonify({'ok': False, 'error': result['error']}), 400
    
    return jsonify({
        'ok': True,
        'hetero_var': result['hetero_var'],
        'threshold': result['threshold'],
        'high_group': {
            'params': result['high_group']['params'],
            'pvalues': result['high_group']['pvalues'],
            'r2': result['high_group']['r2'],
            'n_obs': result['high_group']['n_obs']
        },
        'low_group': {
            'params': result['low_group']['params'],
            'pvalues': result['low_group']['pvalues'],
            'r2': result['low_group']['r2'],
            'n_obs': result['low_group']['n_obs']
        },
        'n_high': result['n_high'],
        'n_low': result['n_low']
    })


# ============================================================
# 11. 机制分析（中介效应）
# ============================================================
@did_bp.route('/api/did/mediation', methods=['POST'])
def api_did_mediation():
    """中介效应分析"""
    data = request.get_json()
    df, err = _load_file(data.get('file'))
    if err:
        return jsonify({'ok': False, 'error': err}), 400
    
    dep_var = data.get('dep_var')
    mediator = data.get('mediator')
    
    if not dep_var or not mediator:
        return jsonify({'ok': False, 'error': '缺少 dep_var 或 mediator'}), 400
    
    treat_col = data.get('treat_col', 'treat')
    time_col = data.get('time_col', 'year')
    unit_col = data.get('unit_col', 'province')
    control_vars = data.get('control_vars', [])
    
    result = mediation_analysis(df, dep_var, mediator, treat_col, time_col, unit_col, control_vars)
    
    if 'error' in result:
        return jsonify({'ok': False, 'error': result['error']}), 400
    
    return jsonify({
        'ok': True,
        'a_coefficient': result['a_coefficient'],
        'b_coefficient': result['b_coefficient'],
        'total_effect': result['total_effect'],
        'direct_effect': result['direct_effect'],
        'indirect_effect': result['indirect_effect'],
        'mediation_ratio': result['mediation_ratio'],
        'mediator': result['mediator']
    })


# ============================================================
# 12. 交互项分析
# ============================================================
@did_bp.route('/api/did/interaction', methods=['POST'])
def api_did_interaction():
    """交互项分析"""
    data = request.get_json()
    df, err = _load_file(data.get('file'))
    if err:
        return jsonify({'ok': False, 'error': err}), 400
    
    dep_var = data.get('dep_var')
    interact_var = data.get('interact_var')
    
    if not dep_var or not interact_var:
        return jsonify({'ok': False, 'error': '缺少 dep_var 或 interact_var'}), 400
    
    treat_col = data.get('treat_col', 'treat')
    time_col = data.get('time_col', 'year')
    unit_col = data.get('unit_col', 'province')
    control_vars = data.get('control_vars', [])
    
    result = interaction_analysis(df, dep_var, treat_col, time_col, unit_col, control_vars, interact_var)
    
    if 'error' in result:
        return jsonify({'ok': False, 'error': result['error']}), 400
    
    return jsonify({
        'ok': True,
        'params': result['params'],
        'se': result['se'],
        'pvalues': result['pvalues'],
        'r2': result['r2'],
        'interact_col': result['interact_col'],
        'interact_var': result['interact_var']
    })


# ============================================================
# 13. 综合DID分析（一键运行）
# ============================================================
@did_bp.route('/api/did/full-analysis', methods=['POST'])
def api_did_full_analysis():
    """一键运行完整DID分析"""
    data = request.get_json()
    df, err = _load_file(data.get('file'))
    if err:
        return jsonify({'ok': False, 'error': err}), 400
    
    dep_var = data.get('dep_var')
    if not dep_var:
        return jsonify({'ok': False, 'error': '缺少 dep_var'}), 400
    
    treat_col = data.get('treat_col', 'treat')
    time_col = data.get('time_col', 'year')
    unit_col = data.get('unit_col', 'province')
    control_vars = data.get('control_vars', [])
    consume_cols = data.get('consume_cols', None)
    
    results = run_full_did_analysis(df, dep_var, treat_col, time_col, unit_col, control_vars, consume_cols)
    
    # 格式化输出
    output = {}
    
    if 'cleaning' in results:
        output['cleaning_report'] = results['cleaning']['report']
    
    if 'baseline' in results and 'params' in results['baseline']:
        output['baseline'] = {
            'params': results['baseline']['params'],
            'pvalues': results['baseline']['pvalues'],
            'r2': results['baseline']['r2'],
            'n_obs': results['baseline']['n_obs']
        }
    
    if 'parallel_trends' in results:
        output['parallel_trends'] = {
            'passed': results['parallel_trends']['passed'],
            'policy_year': results['parallel_trends']['policy_year'],
            'coef_data': json.loads(results['parallel_trends']['coef_df'].to_json(orient='records', date_format='iso'))
        }
    
    if 'placebo' in results:
        output['placebo'] = {
            'passed': results['placebo']['passed'],
            'p_value': results['placebo']['p_value'],
            'true_coef': results['placebo']['true_coef']
        }
    
    return jsonify({'ok': True, **output})


# ============================================================
# 14. 获取列信息（用于前端选择变量）
# ============================================================
@did_bp.route('/api/did/columns', methods=['GET'])
def api_did_columns():
    """获取文件列信息"""
    file_name = request.args.get('file')
    df, err = _load_file(file_name)
    if err:
        return jsonify({'ok': False, 'error': err}), 400
    
    num_cols = df.select_dtypes(include=[np.number]).columns.tolist()
    cat_cols = df.select_dtypes(include=['object', 'category']).columns.tolist()
    
    return jsonify({
        'ok': True,
        'all_columns': df.columns.tolist(),
        'numeric_columns': num_cols,
        'categorical_columns': cat_cols,
        'n_rows': len(df),
        'n_cols': len(df.columns)
    })


# ============================================================
# 注册路由
# ============================================================
def register_routes(app, limiter):
    """注册DID路由"""
    app.register_blueprint(did_bp)
