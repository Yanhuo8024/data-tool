#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
search_routes.py — 全局搜索（Command+K / Ctrl+K 命令面板的数据源）

提供两类能力：
1. GET /api/search/index —— 一次性返回全量索引（标签页 + API），前端本地过滤实现零延迟搜索
2. GET /api/search?q=xxx —— 服务端过滤（备用）

索引构成：
- tabs：解析 templates/index.html 的导航结构（nav-cat 分组 + nbtn data-t），配关键词表
- apis：复用 openapi_routes._build_spec 自动扫描 app.url_map（200+ API），扁平化为 path/method/summary/tag
"""
import re
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from routes.openapi_routes import _build_spec

search_bp = Blueprint('search', __name__)

# tab 名称与搜索关键词（id -> [显示名, 关键词空格串]）
TAB_META = {
    'tab-overview': ['数据概览', '总览 仪表盘 首页 实时 行情 大盘'],
    'tab-templates': ['分析模板', '模板市场 场景 复用 一键 基金回撤 债券 久期 DID spearman'],
    'tab-report': ['报告', 'word excel pdf 导出 生成 摘要 报表'],
    'tab-multi-agent': ['多Agent', '多智能体 协作 任务分解 并行 agent'],
    'tab-scheduler': ['定时任务', '调度 cron 周期 依赖 自动执行 任务队列'],
    'tab-auth': ['登录', '认证 jwt api key 账号 密码 权限'],
    'tab-up': ['上传', '文件 导入 csv excel parquet 归档 快照 标签'],
    'tab-help': ['使用教程', '帮助 文档 api文档 演练 demo 引导 教程'],
    'tab-troubleshooting': ['故障排查', '排查 报错 修复 症状 原因 步骤 手册'],
    'tab-db': ['数据库直连', 'mysql postgres clickhouse sqlite 连接 智能sql 自然语言 脱敏'],
    'tab-scrape': ['爬取', '采集 抓取 网页 接口 爬虫'],
    'tab-finance': ['金融数据', 'akshare tushare baostock 股票 行情 k线 资金流 宏观 指数'],
    'tab-store': ['存储', '文件管理 血缘 分享 快照 版本 下载'],
    'tab-clean': ['清洗', '缺失值 去重 异常值 类型转换 标准化'],
    'tab-table': ['表格工具', 'excel 透视 编辑 转换 预览'],
    'tab-screen': ['筛选器', '筛选 过滤 条件 子集'],
    'tab-analyze': ['分析', '描述统计 分组聚合 相关性 duckdb 高性能 大文件'],
    'tab-finance-viz': ['可视化', '图表 k线 财务 绘图 echarts 交互'],
    'tab-ts': ['时序分析', '时间序列 预测 arima prophet 分解 平稳性'],
    'tab-did': ['DID计量', '双重差分 政策评估 因果 处理效应 平行趋势 placebo'],
    'tab-ht': ['假设检验', 't检验 anova 正态性 卡方 分布拟合'],
    'tab-fs': ['财务报表', '财务比率 报表分析 利润 资产负债表'],
    'tab-fixedincome': ['固定收益', '债券 收益率 久期 凸性 信用利差 oas 曲线'],
    'tab-fund': ['基金分析', '公募 净值 夏普 回撤 风险收益 基金'],
    'tab-ml': ['机器学习', '模型 训练 预测 分类 回归 聚类 特征 版本 插件'],
    'tab-scorecard': ['评分卡', '信用 风控 woe iv ks auc psi 分箱 单调 拒绝推断'],
    'tab-nlp': ['NLP分析', '文本 情感 主题 lda 相似度 分词 词云'],
    'tab-finance-model': ['金融建模', 'fama french 因子 回归 量化 金融'],
    'tab-pipeline': ['Pipeline', '流水线 流程 编排 节点 自动化'],
    'tab-nlq': ['自然语言查数', 'nlq 智能sql 中文提问 查询 预览 确认'],
    'tab-rag': ['RAG问答', '检索增强 问答 知识库 文档 召回'],
    'tab-chart': ['智能图表', '图表 生成 自然语言 可视化 交互'],
    'tab-security': ['安全中心', '审计 日志 脱敏 api key 权限 监控'],
    'tab-knowledge': ['知识点', '知识库 百科 检索 固定收益 专题'],
    'tab-knowledge-mgmt': ['知识库管理', '导入 markdown 文档管理 批量 rag 生效'],
    'tab-report-market': ['报告市场', '报告模板 市场 分享'],
}

_INDEX_CACHE = None


def _parse_tabs():
    """解析 templates/index.html 导航 → 标签页索引（分组取自 nav-cat）"""
    tpl = Path(__file__).resolve().parents[1] / 'templates' / 'index.html'
    try:
        html = tpl.read_text(encoding='utf-8')
    except OSError:
        return []
    tabs = []
    cat = '未分组'
    for line in html.splitlines():
        m_cat = re.search(r'nav-cat">([^<]+)<', line)
        if m_cat:
            cat = m_cat.group(1).strip()
            continue
        if 'nav-global' in line:
            cat = '全局'
            continue
        m_btn = re.search(r'data-t="(tab-[a-z0-9-]+)"[^>]*>', line)
        if m_btn:
            tid = m_btn.group(1)
            name, kw = TAB_META.get(tid, [tid, ''])
            tabs.append({'id': tid, 'name': name, 'category': cat, 'keywords': kw})
    return tabs


def _parse_apis():
    """OpenAPI spec → 扁平 API 索引"""
    try:
        spec = _build_spec(current_app)
    except Exception:
        return []
    apis = []
    for path, methods in spec.get('paths', {}).items():
        for method, info in methods.items():
            apis.append({
                'path': path,
                'method': method.upper(),
                'summary': info.get('summary', ''),
                'tag': (info.get('tags') or ['其他'])[0],
            })
    return apis


def _build_index():
    """构建并缓存全量索引（app 实例变化时重建）"""
    global _INDEX_CACHE
    key = id(current_app._get_current_object())
    if _INDEX_CACHE and _INDEX_CACHE.get('_app') == key:
        return _INDEX_CACHE
    index = {
        '_app': key,
        'tabs': _parse_tabs(),
        'apis': _parse_apis(),
    }
    _INDEX_CACHE = index
    return index


@search_bp.route('/api/search/index', methods=['GET'])
def search_index():
    """全局搜索全量索引（标签页 + API，前端本地过滤）"""
    idx = _build_index()
    return jsonify({'ok': True, 'tabs': idx['tabs'], 'apis': idx['apis'],
                    'count': {'tabs': len(idx['tabs']), 'apis': len(idx['apis'])}})


@search_bp.route('/api/search', methods=['GET'])
def search():
    """服务端搜索（q 参数；tabs 按名称/关键词/分组，apis 按路径/摘要/分组）"""
    q = (request.args.get('q') or '').strip().lower()
    if not q:
        return jsonify({'ok': True, 'tabs': [], 'apis': []})
    idx = _build_index()
    tab_hits = []
    for t in idx['tabs']:
        hay = ' '.join([t['name'], t['keywords'], t['category']]).lower()
        if q in hay:
            tab_hits.append(t)
    api_hits = []
    for a in idx['apis']:
        hay = ' '.join([a['path'], a['summary'], a['tag']]).lower()
        if q in hay:
            api_hits.append(a)
    # 路径前缀命中优先
    api_hits.sort(key=lambda a: (0 if a['path'].startswith('/' + q) else 1, a['path']))
    return jsonify({'ok': True, 'tabs': tab_hits[:30], 'apis': api_hits[:30]})
