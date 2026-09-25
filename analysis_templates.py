#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analysis_templates.py — 分析模板市场（内置常见分析场景，一键复用）

模板字段：
  id       唯一标识
  name     模板名
  desc     说明
  category 分类
  icon     图标
  tab      目标标签页（switchToTab 用）
  fills    预填输入：[{"id": "控件ID", "value": "值"}]
  trigger  自动执行的前端函数调用（可空 = 仅跳转+预填+提示）
  hint     应用后的提示
"""
ANALYSIS_TEMPLATES = [
    {
        "id": "fund_drawdown",
        "name": "基金回撤分析",
        "desc": "输入基金代码一键拉取净值，计算年化收益 / 最大回撤 / 夏普 / Calmar / Sortino",
        "category": "基金分析",
        "icon": "📈",
        "tab": "tab-fund",
        "fills": [{"id": "fund-code", "value": "005827"}],
        "trigger": "fundCalc()",
        "hint": "已填入示例基金 005827（易方达蓝筹精选），正在拉取净值并计算指标…",
    },
    {
        "id": "fund_metrics",
        "name": "基金指标速查",
        "desc": "换一个基金代码快速查看其风险收益指标（可改代码后重新计算）",
        "category": "基金分析",
        "icon": "📊",
        "tab": "tab-fund",
        "fills": [{"id": "fund-code", "value": "110022"}],
        "trigger": "fundCalc()",
        "hint": "已填入 110022（易方达消费行业），正在计算…",
    },
    {
        "id": "bond_valuation",
        "name": "债券估值与久期",
        "desc": "输入票息/收益率/期限，一键计算债券价值、久期与凸性",
        "category": "固定收益",
        "icon": "🧾",
        "tab": "tab-fixedincome",
        "fills": [
            {"id": "fi-coupon", "value": "3.5"},
            {"id": "fi-ytm", "value": "2.8"},
            {"id": "fi-maturity", "value": "10"},
            {"id": "fi-freq", "value": "2"},
            {"id": "fi-face", "value": "100"},
            {"id": "fi-accrual", "value": "0"},
        ],
        "trigger": "fiCalcBond()",
        "hint": "已填入示例债券参数（票息 3.5%、期限 10 年），正在计算估值与久期凸性…",
    },
    {
        "id": "yield_curve",
        "name": "收益率曲线分析",
        "desc": "录入各期限收益率，绘制并解读收益率曲线形态",
        "category": "固定收益",
        "icon": "📉",
        "tab": "tab-fixedincome",
        "trigger": "",
        "hint": "请在「收益率曲线」卡片录入各期限收益率后点击计算（自动跳转完成）",
    },
    {
        "id": "scorecard_quick",
        "name": "信用评分卡快速建模",
        "desc": "WOE/IV 分箱 + 逻辑回归 + 评分刻度 + AUC/KS + PSI 全链路",
        "category": "算法建模",
        "icon": "🃏",
        "tab": "tab-scorecard",
        "fills": [
            {"id": "sc-method", "value": "chimerge"},
            {"id": "sc-maxbins", "value": "5"},
            {"id": "sc-base", "value": "600"},
            {"id": "sc-pdo", "value": "50"},
            {"id": "sc-odds", "value": "50"},
            {"id": "sc-monotonic", "value": "true"},
        ],
        "trigger": "",
        "hint": "请先在顶部选择数据文件与目标列（0/1 违约标记），然后点击「训练评分卡」",
    },
    {
        "id": "did_policy",
        "name": "DID 政策评估",
        "desc": "处理组/对照组 + 试点前后 → 基准回归 / 平行趋势 / 安慰剂检验",
        "category": "计量分析",
        "icon": "📊",
        "tab": "tab-did",
        "trigger": "",
        "hint": "请先选择面板数据文件（含个体/时间/处理组列），设置变量后点击「运行全量分析」",
    },
    {
        "id": "nlp_financial",
        "name": "NLP 财报情感分析",
        "desc": "粘贴财报/研报片段，一键分词 + 情感 + 主题建模 + 文本相似度",
        "category": "文本分析",
        "icon": "🔍",
        "tab": "tab-nlp",
        "fills": [
            {"id": "nlp-input", "value": "公司一季度营收增长稳健，现金流充裕，毛利率显著提升，新业务线放量。\n公司发布重磅新品，市场反响热烈，订单同比大幅增长，产能利用率提升。\n行业竞争加剧，价格战压力显现，公司毛利率承压，需关注成本控制。\n宏观经济复苏，消费回暖，龙头公司集中度提升，估值有望修复。\n货币政策维持宽松，信贷投放加快，银行资产质量改善，息差企稳。"},
        ],
        "trigger": "doNLP()",
        "hint": "已填入示例财报片段，正在执行分词/情感/LDA/相似度分析…",
    },
    {
        "id": "spearman_corr",
        "name": "特征相关性分析（Spearman）",
        "desc": "对数据文件各数值列做 Spearman 秩相关 + 热力图 + 显著性结论",
        "category": "算法建模",
        "icon": "🔗",
        "tab": "tab-ml",
        "trigger": "",
        "hint": "请在「Spearman 秩相关」卡片选择数据文件与列后点击计算",
    },
]
