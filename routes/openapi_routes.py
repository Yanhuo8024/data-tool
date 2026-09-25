#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
openapi_routes.py - OpenAPI/Swagger API 文档
"""

from flask import Blueprint, jsonify, render_template_string

openapi_bp = Blueprint('openapi', __name__)

# 简化的 OpenAPI 3.0 规范
OPENAPI_SPEC = {
    "openapi": "3.0.3",
    "info": {
        "title": "数据分析平台 API",
        "description": "数据采集、清洗、分析、建模、可视化、报告生成的统一 API",
        "version": "3.0.0",
        "contact": {"name": "Data Tool"}
    },
    "servers": [{"url": "/", "description": "本地服务器"}],
    "paths": {},
    "tags": [
        {"name": "文件管理", "description": "文件上传、下载、删除"},
        {"name": "数据清洗", "description": "数据清洗、缺失值处理"},
        {"name": "数据分析", "description": "描述统计、相关性、分组"},
        {"name": "可视化", "description": "图表生成、Plotly交互"},
        {"name": "机器学习", "description": "模型训练、预测、评估"},
        {"name": "金融数据", "description": "K线、回测、筛选"},
        {"name": "时序分析", "description": "Prophet、ARIMA、分解"},
        {"name": "报告", "description": "报告生成、模板管理"},
        {"name": "AI助手", "description": "自然语言对话接口"},
    ]
}

# API 端点定义
APIS = [
    # 文件管理
    {"path": "/api/files", "method": "get", "summary": "列出已上传文件", "tag": "文件管理"},
    {"path": "/api/upload", "method": "post", "summary": "上传文件", "tag": "文件管理"},
    {"path": "/api/files/<name>", "method": "delete", "summary": "删除文件", "tag": "文件管理"},
    {"path": "/api/download/<name>", "method": "get", "summary": "下载文件", "tag": "文件管理"},
    {"path": "/api/paginated/<name>", "method": "get", "summary": "分页获取数据", "tag": "文件管理"},
    
    # 数据清洗
    {"path": "/api/clean", "method": "post", "summary": "数据清洗", "tag": "数据清洗"},
    {"path": "/api/clean/missing-heatmap", "method": "get", "summary": "缺失值热力图", "tag": "数据清洗"},
    {"path": "/api/clean/undo", "method": "post", "summary": "撤销清洗操作", "tag": "数据清洗"},
    
    # 数据分析
    {"path": "/api/analyze", "method": "post", "summary": "数据分析", "tag": "数据分析"},
    {"path": "/api/describe", "method": "post", "summary": "描述性统计", "tag": "数据分析"},
    {"path": "/api/correlation", "method": "post", "summary": "相关性分析", "tag": "数据分析"},
    {"path": "/api/group", "method": "post", "summary": "分组聚合", "tag": "数据分析"},
    
    # 可视化
    {"path": "/api/chart/custom", "method": "post", "summary": "自定义图表", "tag": "可视化"},
    {"path": "/api/chart/plotly", "method": "post", "summary": "Plotly交互图表", "tag": "可视化"},
    
    # 机器学习
    {"path": "/api/ml/train", "method": "post", "summary": "训练模型", "tag": "机器学习"},
    {"path": "/api/ml/predict", "method": "post", "summary": "模型预测", "tag": "机器学习"},
    {"path": "/api/ml/models", "method": "get", "summary": "模型列表", "tag": "机器学习"},
    {"path": "/api/feature/auto", "method": "post", "summary": "自动特征工程", "tag": "机器学习"},
    
    # 金融数据
    {"path": "/api/finance/kline", "method": "get", "summary": "K线数据", "tag": "金融数据"},
    {"path": "/api/finance/backtest", "method": "post", "summary": "策略回测", "tag": "金融数据"},
    {"path": "/api/screener/screen", "method": "post", "summary": "股票筛选", "tag": "金融数据"},
    {"path": "/api/finance/viz", "method": "post", "summary": "金融可视化", "tag": "金融数据"},
    
    # 时序分析
    {"path": "/api/ts/prophet", "method": "post", "summary": "Prophet预测", "tag": "时序分析"},
    {"path": "/api/ts/arima", "method": "post", "summary": "ARIMA模型", "tag": "时序分析"},
    {"path": "/api/ts/decompose", "method": "post", "summary": "时序分解", "tag": "时序分析"},
    
    # 报告
    {"path": "/api/report/generate", "method": "post", "summary": "生成报告", "tag": "报告"},
    {"path": "/api/report/templates", "method": "get", "summary": "报告模板列表", "tag": "报告"},
    {"path": "/api/templates", "method": "get", "summary": "模板市场", "tag": "报告"},
    {"path": "/api/templates/use", "method": "post", "summary": "使用模板", "tag": "报告"},
    
    # AI助手
    {"path": "/api/assistant/chat", "method": "post", "summary": "AI对话", "tag": "AI助手"},
    {"path": "/api/assistant/history", "method": "get", "summary": "对话历史", "tag": "AI助手"},
    {"path": "/api/assistant/clear", "method": "post", "summary": "清空对话", "tag": "AI助手"},
    # LLM
    {"path": "/api/llm/test", "method": "post", "summary": "测试LLM连接", "tag": "AI助手"},
    {"path": "/api/llm/chat", "method": "post", "summary": "LLM对话", "tag": "AI助手"},
    # Agent
    {"path": "/api/agent/run", "method": "post", "summary": "运行Agent", "tag": "Agent"},
    {"path": "/api/agent/memory", "method": "get", "summary": "Agent记忆", "tag": "Agent"},
    {"path": "/api/agent/memory/clear", "method": "post", "summary": "清空记忆", "tag": "Agent"},
    {"path": "/api/agent/tools", "method": "get", "summary": "工具列表", "tag": "Agent"},
    # 特征工程
    {"path": "/api/feature/auto", "method": "post", "summary": "自动特征工程", "tag": "机器学习"},
    {"path": "/api/feature/generate", "method": "post", "summary": "生成特征", "tag": "机器学习"},
    {"path": "/api/feature/scale", "method": "post", "summary": "特征缩放", "tag": "机器学习"},
    # NLP
    {"path": "/api/nlp/analyze", "method": "post", "summary": "NLP分析", "tag": "AI助手"},
    {"path": "/api/nlp/wordcloud", "method": "post", "summary": "词云生成", "tag": "AI助手"},
    # RAG
    {"path": "/api/rag/upload", "method": "post", "summary": "上传文档", "tag": "AI助手"},
    {"path": "/api/rag/query", "method": "post", "summary": "RAG问答", "tag": "AI助手"},
    # SQL
    {"path": "/api/sql/execute", "method": "post", "summary": "SQL执行", "tag": "AI助手"},
    # 数据血缘
    {"path": "/api/lineage", "method": "post", "summary": "数据血缘", "tag": "数据分析"},
]


def _build_spec(app):
    """构建完整 OpenAPI 规范（自动扫描 app.url_map，覆盖全部 200+ API 路由）"""
    spec = dict(OPENAPI_SPEC)
    paths = {}

    # 手工补充的 summary（docstring 缺失时兜底）
    manual = {f"{a['method'].upper()} {a['path']}": a.get('summary', '')
              for a in APIS}

    # 路径前缀 → 标签（中文分组）
    TAG_BY_PREFIX = [
        ('/api/ml/models/versions', '机器学习'), ('/api/ml/models', '机器学习'),
        ('/api/ml', '机器学习'), ('/api/feature', '特征工程'),
        ('/api/finance', '金融数据'), ('/api/fixedincome', '固定收益'),
        ('/api/scorecard', '评分卡'), ('/api/screener', '股票筛选'),
        ('/api/db', '数据库直连'), ('/api/knowledge', '知识库'),
        ('/api/lineage', '数据血缘'), ('/api/stream', '实时推送'),
        ('/api/plugins', '插件'), ('/api/share', '分享'),
        ('/api/scheduler', '定时任务'), ('/api/dashboard', '仪表盘'),
        ('/api/did', 'DID计量'), ('/api/nlp', 'NLP'),
        ('/api/clean', '数据清洗'), ('/api/analyze', '数据分析'),
        ('/api/describe', '数据分析'), ('/api/correlation', '数据分析'),
        ('/api/group', '数据分析'), ('/api/chart', '可视化'),
        ('/api/report', '报告'), ('/api/agent', 'Agent'),
        ('/api/assistant', 'AI助手'), ('/api/llm', 'LLM'),
        ('/api/files', '文件管理'), ('/api/snapshot', '快照'),
        ('/api/ts', '时序分析'), ('/api/rag', 'RAG'),
        ('/api/upload', '文件管理'), ('/api/store', '存储'),
        ('/api/auth', '认证'), ('/api/security', '安全中心'),
        ('/api/monitoring', '监控'), ('/api/stability', '稳定性'),
        ('/api/scrape', '数据采集'), ('/api/table', '表格工具'),
        ('/api/screen', '筛选器'), ('/api/pipeline', 'Pipeline'),
        ('/api/version', '系统'), ('/api/cache', '缓存'),
        ('/api/export', '导出'),
    ]

    def tag_for(path):
        for prefix, tag in TAG_BY_PREFIX:
            if path.startswith(prefix):
                return tag
        return '其他'

    def summary_for(endpoint, method, path):
        view = app.view_functions.get(endpoint)
        doc = (view.__doc__ or '').strip() if view else ''
        if doc:
            return doc.split('\n')[0].strip()
        return manual.get(f"{method} {path}", '')

    rules = sorted(app.url_map.iter_rules(), key=lambda r: str(r))
    for rule in rules:
        path = str(rule)
        # 跳过静态资源、文档自身
        if (path.startswith('/static') or path.startswith('/api/openapi')
                or path in ('/docs',) or path == '/'):
            continue
        for method in rule.methods:
            m = method.lower()
            if m in ('head', 'options'):
                continue
            entry = paths.setdefault(path, {})
            entry[m] = {
                "summary": summary_for(rule.endpoint, m, path),
                "tags": [tag_for(path)],
                "responses": {
                    "200": {"description": "成功"},
                    "400": {"description": "参数错误"},
                    "500": {"description": "服务器错误"},
                },
            }
            if m == "post":
                entry[m]["requestBody"] = {
                    "content": {"application/json": {"schema": {"type": "object"}}}
                }
    spec["paths"] = paths
    return spec


# Swagger UI HTML 模板
SWAGGER_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <title>API 文档 - Data Tool</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
  <style>
    body { margin: 0; background: #0f1011; }
    .topbar { background: #0f1011; padding: 16px 32px; border-bottom: 1px solid rgba(255,255,255,0.06); }
    .topbar h1 { margin: 0; color: #f7f8f8; font-size: 18px; font-weight: 600; }
    .topbar a { color: #FF6B6B; text-decoration: none; font-size: 13px; }
    .swagger-ui .info .title { color: #f7f8f8 !important; }
    .swagger-ui .scheme-container { background: #0f1011 !important; }
  </style>
</head>
<body>
  <div class="topbar">
    <h1>📊 Data Tool API 文档</h1>
    <a href="/">← 返回应用</a>
  </div>
  <div id="swagger-ui"></div>
  <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script>
    SwaggerUIBundle({
      url: '/api/openapi.json',
      dom_id: '#swagger-ui',
      deepLinking: true,
      presets: [SwaggerUIBundle.presets.apis],
      layout: "BaseLayout"
    });
  </script>
</body>
</html>
"""


@openapi_bp.route('/api/openapi.json', methods=['GET'])
def api_openapi_json():
    """OpenAPI JSON 规范（自动扫描全部路由）"""
    from flask import current_app
    return jsonify(_build_spec(current_app))


@openapi_bp.route('/docs', methods=['GET'])
def api_docs():
    """Swagger UI 文档页面"""
    return render_template_string(SWAGGER_HTML)
