#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
report_helpers.py - 分析报告 HTML 构建公共模块

集中各模块（ai_assistant 路由 / did / finance 等）共用的报告 HTML 构建函数，
避免在不同文件重复实现。实现为增强超集：兼容基础字段（overview / key_findings）
与扩展字段（anomalies / recommendations / causal_hints / top_group 等）。
"""


def build_autonomous_report_html(report: dict, file_name: str) -> str:
    """自主/深度分析报告 HTML：概览 + 关键发现 + 异常检测 + 建议"""
    html = f'<div style="padding:16px;"><h4>🧠 AI 自主分析报告</h4><p style="color:var(--ink-subtle);font-size:12px;">文件: {file_name}</p>'

    # 概览
    overview = report.get('overview', {})
    if overview:
        html += '<div style="margin-top:12px;"><b>📊 数据概览</b><div style="display:flex;gap:16px;margin-top:8px;">'
        html += f'<div style="padding:8px 12px;background:rgba(99,102,241,0.1);border-radius:6px;font-size:12px;">行数: {overview.get("rows", "-")}</div>'
        html += f'<div style="padding:8px 12px;background:rgba(99,102,241,0.1);border-radius:6px;font-size:12px;">列数: {overview.get("cols", "-")}</div>'
        if overview.get("num_cols"):
            html += f'<div style="padding:8px 12px;background:rgba(99,102,241,0.1);border-radius:6px;font-size:12px;">数值列: {len(overview["num_cols"])}</div>'
        html += '</div></div>'

    # 关键发现
    findings = report.get('key_findings', [])
    if findings:
        html += '<div style="margin-top:12px;"><b>💡 关键发现</b><ul style="margin-top:8px;padding-left:20px;">'
        for f in findings:
            html += f'<li style="font-size:12px;margin-bottom:4px;">{f}</li>'
        html += '</ul></div>'

    # 异常检测
    anomalies = report.get('anomalies', {})
    if anomalies and anomalies.get('total_outliers', 0) > 0:
        html += (f'<div style="margin-top:12px;"><b>⚠️ 异常检测</b>'
                 f'<p style="font-size:12px;margin-top:4px;">发现 {anomalies["total_outliers"]} 个异常点，'
                 f'涉及列: {", ".join(anomalies.get("affected_cols", []))}</p></div>')

    # 建议
    recs = report.get('recommendations', [])
    if recs:
        html += '<div style="margin-top:12px;"><b>🎯 分析建议</b><ul style="margin-top:8px;padding-left:20px;">'
        for r in recs:
            html += f'<li style="font-size:12px;margin-bottom:4px;">{r}</li>'
        html += '</ul></div>'

    html += '</div>'
    return html


def build_analysis_html(report: dict, file_name: str) -> str:
    """标准分析报告 HTML（与自主分析同构）"""
    return build_autonomous_report_html(report, file_name)


def build_correlation_html(corr: dict, file_name: str) -> str:
    """相关性分析报告 HTML：最强相关对 + 潜在因果"""
    html = f'<div style="padding:16px;"><h4>🔗 相关性分析</h4><p style="color:var(--ink-subtle);font-size:12px;">文件: {file_name}</p>'

    top_pairs = corr.get('top_pairs', [])
    if top_pairs:
        html += '<div style="margin-top:12px;"><b>最强相关对</b><div style="margin-top:8px;">'
        for col1, col2, c in top_pairs[:5]:
            color = '#10b981' if c > 0 else '#ef4444'
            html += (f'<div style="padding:6px 10px;background:rgba(99,102,241,0.05);'
                     f'border-radius:4px;margin-bottom:4px;font-size:12px;">{col1} ↔ {col2}: '
                     f'<span style="color:{color};font-weight:600;">{c}</span></div>')
        html += '</div></div>'

    causal = corr.get('causal_hints', [])
    if causal:
        html += '<div style="margin-top:12px;"><b>🔍 潜在因果</b><ul style="margin-top:8px;padding-left:20px;">'
        for hint in causal:
            html += f'<li style="font-size:12px;margin-bottom:4px;">{hint}</li>'
        html += '</ul></div>'

    html += '</div>'
    return html


def build_group_html(segments: dict, file_name: str) -> str:
    """分组统计报告 HTML：分组维度 + 极值组"""
    html = f'<div style="padding:16px;"><h4>🔀 分组统计</h4><p style="color:var(--ink-subtle);font-size:12px;">文件: {file_name}</p>'

    if segments.get('group_col'):
        html += f'<div style="margin-top:12px;"><b>分组维度</b>: {segments["group_col"]}</div>'
        if segments.get('group_count') is not None:
            html += f'<div style="margin-top:8px;"><b>分组数</b>: {segments["group_count"]}</div>'

        top = segments.get('top_group')
        bottom = segments.get('bottom_group')
        if top:
            html += (f'<div style="margin-top:8px;padding:8px 12px;background:rgba(16,185,129,0.1);'
                     f'border-radius:6px;font-size:12px;">最高: {top[0]} = {top[1]}</div>')
        if bottom:
            html += (f'<div style="margin-top:4px;padding:8px 12px;background:rgba(239,68,68,0.1);'
                     f'border-radius:6px;font-size:12px;">最低: {bottom[0]} = {bottom[1]}</div>')

    html += '</div>'
    return html
