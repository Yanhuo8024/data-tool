#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
intent_router.py - 意图分流中间层（解决"用户一句话直接进 NLQ/RAG/Agent"问题）

职责：
1. 在调用任何大模型 / 知识库 / Agent 之前，先用轻量规则做意图分类（不额外消耗云端 quota）
2. 预提取实体（时间、指标、标的、维度、数据文件），供下游复用
3. 金融/业务同义词映射（normalize_query），提升 RAG 与 NLQ 命中率
4. 低置信度时主动澄清，而不是硬猜
5. 回复角色（role）与深度（depth）判定，供下游按意图切换人设

设计原则：
- 纯规则、无网络请求：规避免费额度 429（分类一律不打 LLM，除非显式开启 _enable_llm_refine）
- 不接管控制流：只输出路由建议与元数据；如何路由仍由 ai_assistant 决定（保留全部既有回退）
- 与 ai_assistant 里的 IntentRecognizer（负责生成执行计划步骤）解耦，二者互补
"""

import re
import os
import json
import time
import logging
from typing import Dict, Any, List, Optional, Tuple
import threading

logger = logging.getLogger(__name__)


# ==================== 意图类别 ====================

INTENT_CATEGORIES = {
    "nlq":         "自然语言查数（用户想要数据/指标/具体数字）",
    "analysis":    "深度分析（用户要解读数据、找规律、做诊断、评估）",
    "visualization": "智能图表（用户想看某种图）",
    "knowledge":   "知识问答（概念解释、业务/金融/税务咨询）",
    "operation":   "操作指导（怎么用某个功能）",
    "troubleshoot": "故障排查（报错、异常、连不上、白屏）",
    "agent":       "复杂任务（多步骤协作、多目标分析）",
    "chat":        "闲聊/寒暄",
    # P0-4 新增：域外请求（此前"今天天气怎么样""帮我写个脚本"被强行归入 analysis/nlq）
    "out_of_scope": "域外请求（与数据/金融/统计无关的通用诉求）",
    # P0-4 新增：计算类此前只存在于 detect_special_intent 与 eval 特判中，
    # 分类器永远不可能返回它 → 纳入正式体系，"算一下…"才能真正走真实计算链路
    "computation": "真实计算（明确要求算出某个指标/算式的结果）",
}


# ==================== 意图体系治理（单一事实源） ====================
# 每个意图的自然语言定义、触发条件、排除边界、实体 Schema。
# 用途：① Layer3 精排提示词由此动态生成；② 新增业务时同步更新此处，
# 防止体系膨胀后定义漂移/边界冲突。实体 Schema 全局统一（同一实体名在不同意图中含义一致）。
INTENT_SPECS: Dict[str, Dict[str, str]] = {
    "nlq": {
        "desc": "自然语言查数：用户想要具体数据值、指标读数、统计结果",
        "triggers": "多少/几个/合计/总量/排名/前N/最新股价/市值/涨跌幅，或对已选数据问具体数值",
        "exclusions": "问概念或公式→knowledge；要画图→visualization；要解读评价→analysis",
        "entities": "indicator=指标名, time=时间范围, target=标的/维度, file=数据文件",
    },
    "analysis": {
        "desc": "深度分析：解读、诊断、归因、评估、对比、行业/公司研究",
        "triggers": "分析/解读/为什么/前景/竞争格局/值不值/怎么样(带对象)/缺失/异常值/DID",
        "exclusions": "只要一个数字→nlq；只要图→visualization；纯概念解释→knowledge",
        "entities": "target=分析对象(公司/行业/文件), indicator=关注指标, time=区间",
    },
    "visualization": {
        "desc": "智能图表：用户想看某种图形呈现",
        "triggers": "画/柱状/折线/饼图/散点/热力图/直方/可视化/plot/chart",
        "exclusions": "口头想了解趋势而不指定图→analysis；查数值→nlq",
        "entities": "col1=X列, col2=Y列, chart_type=图型, file=数据文件",
    },
    "knowledge": {
        "desc": "知识问答：概念解释、公式原理、业务/金融/税务咨询",
        "triggers": "什么是/是什么意思/怎么算(问公式)/介绍一下/科普/指的是",
        "exclusions": "要实际计算数值→computation；要分析数据→analysis；查数→nlq",
        "entities": "term=术语/概念名",
    },
    "operation": {
        "desc": "操作指导：怎么使用网站的某个功能",
        "triggers": "怎么上传/导入/导出/保存/分享/在哪/步骤/教程/按钮",
        "exclusions": "报错或异常→troubleshoot；功能产出数据问题→nlq/analysis",
        "entities": "feature=功能名",
    },
    "troubleshoot": {
        "desc": "故障排查：报错、异常、失败、打不开、卡住",
        "triggers": "报错/错误/失败/连不上/白屏/500/超时/闪退/错误码",
        "exclusions": "询问怎么用功能→operation",
        "entities": "error=报错信息, page=出错页面",
    },
    "agent": {
        "desc": "复杂任务：多步骤协作、多目标综合分析（如完整研究报告、全面尽调）",
        "triggers": "综合/全面/完整分析/研究报告/尽调/深度研究/多维度",
        "exclusions": "单一明确分析→analysis",
        "entities": "goal=任务目标, scope=范围, file=数据文件",
    },
    "chat": {
        "desc": "闲聊/寒暄：问候、致谢、告别等社交表达",
        "triggers": "你好/谢谢/在吗/拜拜/哈哈",
        "exclusions": "只要包含任何业务诉求就不算 chat",
        "entities": "无",
    },
    "out_of_scope": {
        "desc": "域外请求：与数据/金融/统计无关的通用诉求（天气、娱乐、写作、翻译、生活类）",
        "triggers": "天气/气温/新闻/笑话/星座/菜谱/几点/翻译一下/写作文",
        "exclusions": "只要同时含数据/金融/统计业务诉求（数据、文件、指标、股票等）就不算域外",
        "entities": "无",
    },
    "computation": {
        "desc": "真实计算：明确要求算出某个指标的具体结果（如夏普比率、年化收益、最大回撤）",
        "triggers": "算一下/帮我算算/计算一下/算出",
        "exclusions": "问公式原理→knowledge；已知数值的查询→nlq；解读结果→analysis",
        "entities": "indicator=待算指标, target=标的, file=数据文件",
    },
}


# ==================== 业务/金融同义词映射（4.2） ====================

FINANCIAL_SYNONYMS: Dict[str, List[str]] = {
    "回撤": ["最大回撤", "drawdown", "跌了多少", "亏了多少", "回撤多少"],
    "夏普比率": ["夏普", "sharpe", "风险收益比", "性价比"],
    "索提诺比率": ["索提诺", "sortino", "下行风险调整后收益"],
    "久期": ["duration", "利率敏感度", "能扛多少加息"],
    "凸性": ["convexity", "利率二阶"],
    "信用利差": ["信用价差", "spread", "利差走阔", "利差收窄"],
    "DID": ["双重差分", "政策评估", "试点效果", "因果推断", "准自然实验", "政策效应"],
    "PSI": ["稳定性", "漂移", "population stability", "psi指数"],
    "IV": ["信息价值", "information value"],
    "WOE": ["证据权重", "weight of evidence"],
    "平行趋势": ["parallel trend", "共同趋势"],
    "安慰剂检验": ["placebo", "安慰剂"],
    "夏普率": ["sharpe ratio"],
    "卡玛比率": ["卡玛", "calmar"],
    "国库券利率": ["无风险利率"],
}

# 同义词正则：优先匹配，避免把"分析""指标"这类常用词错误替换
_SYNONYM_RE_CACHE: Dict[str, re.Pattern] = {}


def normalize_query(text: str) -> str:
    """把口语化/英文表达映射为标准金融术语，提升 RAG/NLQ 命中率。

    P1 修复：原实现直接 `re.sub(同义词, 标准词)`，当同义词本身是标准词的子串时
    会二次展开（"夏普比率" → "夏普比率比率"）。改为两遍法：
      ① 先把文本中已出现的**标准术语**替换为占位符保护；
      ② 再做同义词 → 标准词替换；
      ③ 还原占位符。
    """
    if not text:
        return text
    slot_pat = '\u0001%s\u0001'
    protected: Dict[str, str] = {}
    # ① 保护已出现的标准术语（长词优先，避免"夏普率"抢占"夏普比率"）
    for idx, standard in enumerate(sorted(FINANCIAL_SYNONYMS, key=len, reverse=True)):
        if standard and len(standard) >= 2 and standard in text:
            ph = slot_pat % f'p{idx}'
            text = text.replace(standard, ph)
            protected[ph] = standard
    # ② 同义词 → 标准词
    n = 0
    for standard, syns in FINANCIAL_SYNONYMS.items():
        for syn in sorted(syns, key=len, reverse=True):
            if not syn or len(syn) < 2:
                continue
            if syn.lower() in text.lower():
                text = re.sub(re.escape(syn), standard, text, flags=re.IGNORECASE)
                n += 1
    # ③ 还原被保护的标准术语
    for ph, standard in protected.items():
        text = text.replace(ph, standard)
    # ④ 去掉替换后产生的相邻重复（"做个DID政策评估" → 保护"DID" + 替换"政策评估"
    #    → "做个DIDDID"；"夏普比率"→"夏普比率比率" 同源）
    for standard in sorted(FINANCIAL_SYNONYMS, key=len, reverse=True):
        if len(standard) >= 2 and (standard + standard) in text:
            text = re.sub(r'(?:' + re.escape(standard) + r'){2,}', standard, text)
    if n or protected:
        logger.debug("normalize_query: 替换 %d 处同义词 + 保护 %d 处标准词 -> %r",
                     n, len(protected), text)
    return text


# ==================== 实体预提取 ====================

# 时间表达（自然语言 → 业务口径提示；供澄清/下游 NLQ 复用）
TIME_WORDS = ["最近", "近7天", "近30天", "近90天", "本月", "上月", "本季度", "上一季度", "今年", "去年", "年初", "年末", "今天", "昨天", "本周", "上周"]

# 常见财经费控指标
INDICATOR_WORDS = ["收益", "收益率", "夏普", "回撤", "波动率", "久期", "利差", "净值", "规模", "份额",
                   "市盈率", "PE", "市净率", "PB", "ROE", "ROA", "毛利率", "净利率", "负债率", "营收",
                   "利润", "现金流", "销售额", "销量", "库存", "周转率", "评分", "IV", "PSI", "WOE",
                   "户数", "参保", "试点", "treatment", "消费", "支出",
                   # ===== 金融增强指标 =====
                   "年化", "超额收益", "alpha", "beta", "卡玛", "索提诺", "凸性", "到期收益率",
                   "票息", "费率", "股息率", "eps", "peg", "dcf", "内在价值", "估值", "仓位",
                   "tracking", "信息比率", "treynor", "最大回撤", "var", "下行风险",
                   "违约", "逾期", "bad", "好客户", "坏客户", "利率", "汇率", "gdp", "cpi", "m2"]

# 标的/维度词
TARGET_WORDS = ["基金", "债券", "股票", "指数", "组合", "省份", "城市", "行业", "账户", "批次", "名称", "代码", "ticker", "symbol", "净额",
                # ===== 金融增强标的 =====
                "ETF", "QDII", "LOF", "期货", "期权", "国债", "信用债", "利率债", "板块",
                "腾讯", "茅台", "宁德时代", "比亚迪", "平安", "招行", "中概股", "美股", "A股"]


def extract_entities(text: str) -> Dict[str, str]:
    """预提取业务实体：时间 / 指标 / 标的 / 数据文件"""
    entities: Dict[str, List[str]] = {}
    low = text.lower()

    # 时间
    _time = [t for t in TIME_WORDS if t in text]
    if _time:
        entities["time"] = _time

    # 指标
    _ind = [i for i in INDICATOR_WORDS if i.lower() in low]
    if _ind:
        entities["indicator"] = _ind

    # 标的（证券代码 / 公司名）
    _tgt = []
    m = re.search(r'HK\s?(\d{4})', text, re.I)
    if m:
        _tgt.append('HK' + m.group(1))
    m = re.search(r'\b(\d{6})\b', text)
    if m:
        _tgt.append(m.group(1))
    _tgt += [t for t in TARGET_WORDS if t in text]
    if _tgt:
        entities["target"] = list(dict.fromkeys(_tgt))

    # 数据文件
    m = re.search(r'([^"\'\s]+\.(?:csv|xlsx|xls|parquet|txt))', text, re.I)
    if m:
        entities["file"] = m.group(1)

    return entities


# ==================== 意图分类器（规则版） ====================

# 每个意图的关键词权重表（命中越多得分越高）
_INTENT_KEYWORDS: Dict[str, List[Tuple[str, float]]] = {
    "nlq": [
        ("多少", 1.0), ("几个", 0.8), ("总量", 0.9), ("合计", 0.9), ("是多少", 1.0),
        ("查一下", 0.7), ("查询", 0.8), ("数据是", 0.8), ("值", 0.3), ("个数", 0.7),
        ("select", 0.9), ("sql", 0.9), ("表", 0.3), ("最高", 0.6), ("最低", 0.6),
        ("排名", 0.7), ("前10", 0.9), ("top", 0.9),
        # ===== 数据/指标查数增强 =====
        ("市值", 0.5), ("股价", 0.5), ("营收", 0.4), ("点位", 0.5), ("涨跌幅", 0.5),
        # ===== P1 断词变体（口语量词与"多少"不共享汉字）=====
        ("几只", 0.8), ("多少条", 0.9), ("多少行", 0.9), ("几条", 0.8),
        ("总额", 0.9), ("总金额", 0.9), ("求和", 0.7), ("汇总", 0.7),
        ("前五", 0.9), ("前三", 0.9), ("前十", 0.9), ("排前", 0.7), ("成交量", 0.8),
    ],
    "analysis": [
        ("分析", 1.0), ("解读", 0.9), ("洞察", 0.8), ("规律", 0.8), ("诊断", 0.8),
        ("评估", 0.7), ("怎么看", 0.7), ("如何看", 0.7), ("表现", 0.6), ("走势", 0.5),
        ("关系", 0.5), ("相关性", 0.8), ("原因", 0.6), ("为什么", 0.7), ("探究", 0.8),
        ("研究", 0.7), ("探索", 0.7), ("看看", 0.5), ("帮助我理解", 0.8),
        ("模型", 0.6), ("训练", 0.5), ("准不准", 0.9), ("效果", 0.5),
        # ===== 行业/公司研究增强 =====
        ("前景", 0.8), ("竞争格局", 0.9), ("格局", 0.5), ("护城河", 0.8), ("竞争力", 0.7),
        ("市场份额", 0.6), ("市占率", 0.6), ("投资机会", 0.7), ("机会", 0.4),
        ("怎么样", 0.4), ("好不好", 0.6), ("哪个好", 0.8), ("值不值", 0.7), ("值得买", 0.7),
        ("买不买", 0.7), ("点评", 0.6),
        # ===== 数据质量/计量增强 =====
        ("缺失", 0.6), ("异常值", 0.7), ("数据质量", 0.8), ("完整率", 0.6), ("离群", 0.6),
        ("did", 0.8), ("双重差分", 0.9), ("政策评估", 0.9), ("政策效应", 0.8), ("因果", 0.6),
        ("归因分析", 0.8),
        # ===== P1 断词变体（"这份数据的质量如何"与"数据质量"不共享字符串）=====
        ("数据的质量", 0.8), ("数据的问题", 0.7), ("质量如何", 0.7), ("完整度", 0.6),
        # ===== 清洗/建模（工具的核心功能，也用于"先清洗数据再建模"这类多步诉求）=====
        ("清洗数据", 0.8), ("数据清洗", 0.8), ("数据建模", 0.8), ("建模", 0.7), ("去重", 0.6),
        # ===== 数据可信度问询（"这些数据靠谱吗"）=====
        ("靠谱", 0.7), ("可信", 0.6),
    ],
    "visualization": [
        ("画", 0.9), ("图", 0.5), ("图表", 0.9), ("柱状", 1.0), ("折线", 1.0), ("饼图", 1.0),
        ("散点", 1.0), ("热力图", 1.0), ("直方", 1.0), ("可视化", 1.0), ("plot", 0.9),
        ("chart", 0.9), ("graph", 0.9), ("展示", 0.5), ("看趋势图", 1.0),
        # ===== P1 断词变体（"用图看下" / "画出来"）=====
        ("画出来", 0.9), ("画一张", 0.9), ("画个图", 1.0),
    ],
    "knowledge": [
        ("什么是", 1.0), ("是什么", 1.0), ("概念", 0.9), ("解释", 0.8), ("介绍", 0.7),
        ("含义", 0.9), ("什么意思", 1.0), ("定义", 0.9), ("科普", 0.8), ("了解", 0.4),
        ("怎么算", 1.0), ("如何算", 1.0), ("怎么计算", 1.0), ("计算公式", 0.9), ("如何计算", 0.9),
        ("指的是", 0.8), ("怎么理解", 0.9),
        # ===== P1 断词变体（对比型概念问答）=====
        ("区别", 0.7), ("不一样", 0.6),
    ],
    "operation": [
        ("怎么用", 1.0), ("如何使用", 1.0), ("怎么上传", 1.0), ("怎么导入", 1.0),
        ("怎么下载", 0.9), ("如何上传", 1.0), ("上传文件", 1.0), ("上传数据", 0.9),
        ("在哪", 0.7), ("步骤", 0.8), ("教程", 0.9), ("如何操作", 1.0),
        ("入门", 0.8), ("在哪里", 0.7), ("功能", 0.4), ("按钮", 0.8),
        ("哪里上传", 1.0), ("怎么用", 0.8),
        # ===== 导出/保存增强 =====
        ("导出", 1.0), ("另存为", 0.9), ("怎么保存", 1.0), ("如何保存", 1.0), ("怎么分享", 0.9),
        # ===== P1 断词变体（口语问法）=====
        ("怎么弄", 0.8), ("咋用", 0.9), ("怎么搞", 0.8), ("在哪儿", 0.7),
        # 实测漏判："怎么把图表保存下来" 只命中 visualization 的"图表" → 补保存/下载变体
        ("保存", 0.9), ("存下来", 0.9), ("下载", 0.8), ("怎么把", 0.7),
        # 实测漏判："那个图表功能在哪" 被"图表"抢走；"怎么连接数据库" 无触发词
        ("功能在哪", 1.0), ("在哪", 0.9), ("怎么连接", 1.0), ("连接数据库", 0.9),
    ],
    "troubleshoot": [
        ("报错", 1.0), ("错误", 1.0), ("异常", 0.9), ("失败", 0.8), ("连不上", 1.0),
        ("打不开", 1.0), ("白屏", 1.0), ("bug", 0.9), ("500", 0.7), ("无法", 0.6),
        ("超时", 0.8), ("卡住", 0.8), ("闪退", 0.9), ("错误码", 0.9),
        # ===== P1 断词变体（口语报障：没有"报错/失败"字面）=====
        ("有问题", 0.8), ("没反应", 0.8), ("不显示", 0.9), ("显示不出来", 0.9),
        ("加载不出", 0.9), ("用不了", 0.9), ("没法用", 0.9), ("不对劲", 0.7),
    ],
    "agent": [
        ("综合", 0.7), ("全面", 0.8), ("整体", 0.6), ("统筹", 0.7), ("多维度", 0.7),
        ("完整分析", 1.0), ("研究报告", 0.8), ("尽调", 0.8), ("深度研究", 1.0),
        # 实测漏判："根据这份数据写个报告" L1 全零（"报告"不是关键词，只参与 depth 判定）
        ("写个报告", 0.9), ("写份报告", 0.9), ("写一份报告", 1.0), ("生成报告", 0.9),
        ("出一份报告", 0.9), ("报告生成", 0.8),
    ],
    "chat": [
        ("你好", 1.0), ("您好", 1.0), ("hi", 0.9), ("hello", 0.9), ("嗨", 0.9), ("哈喽", 0.9),
        ("谢谢", 0.8), ("在吗", 0.9), ("辛苦了", 0.7), ("拜拜", 0.9), ("再见", 0.9), ("哈哈", 0.7),
        # ===== P1 断词变体（社交寒暄的口语变体）=====
        ("在不在", 0.9), ("在么", 0.9), ("在嘛", 0.9), ("哈啰", 0.9), ("早上好", 0.9),
        ("下午好", 0.9), ("晚上好", 0.9), ("辛苦了", 0.8),
    ],
}

# 意图到存量 action 的映射（与 ai_assistant.IntentRecognizer 对齐，便于路由）
INTENT_TO_ACTION: Dict[str, str] = {
    "analysis":    "analyze_data",
    "visualization": "create_chart",
    "nlq":         "nlq",
    "knowledge":   "knowledge",
    "operation":   "help",
    "troubleshoot": "help",
    "agent":       "analyze_data",
    "chat":        "chat",
    # 域外 → 本地礼貌回应（走 chat 链路的域外分支，不联网、不硬答）
    "out_of_scope": "chat",
    # 计算类 → 本地真实计算分支位于 chat 链路内（ai_assistant._chat_guard_reply），
    # 因此 action 必须是 chat，否则永远进不到 real_compute 分支。
    "computation": "chat",
}


# ==================== 判定确定性：显式优先级 + 关键词边界 ====================

# 同分时的确定性 tie-break 顺序（索引越小优先级越高）。
# 背景：旧实现用 `set()` 迭代建融合分字典，同分时 top1 取决于 CPython 哈希随机化，
# 同一句话在不同进程会得到不同意图（"基金" 可在 nlq/analysis/agent 间漂移）。
_INTENT_PRIORITY: List[str] = [
    "troubleshoot",   # 报障优先于功能咨询（"上传文件报错了"要排障，不要教程）
    "operation",
    "knowledge",
    "visualization",
    "nlq",
    "analysis",
    "agent",
    "computation",    # 计算类通常自带强信号；同分时不应压过 nlq/analysis（"基金"四路平局时）
    "out_of_scope",   # 域外只在专用检测器（1.2 分）命中时才有实际分；同分（L2 0.5 档）不应压过 chat
    "chat",           # 最低：同分时绝不判闲聊
]

# 中文关键词的语境否决表：命中位置的前/后缀落入这些字时视为"同形不同义"，不计分。
# 例："值" 不应命中 "价值/值得/数值"；"异常/错误/失败" 不应命中 "异常值/错误率/失败率"。
_KW_NEGATIVE: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "值":   {"prefix": ("价", "得", "增", "贬", "数", "产", "面", "净")},
    "是什么": {"suffix": ("原因", "导致", "引起", "造成", "回事")},
    "异常": {"suffix": ("值",)},
    "错误": {"suffix": ("率",)},
    "失败": {"suffix": ("率",)},
    # 实测污染："可视化一下这个数据" 命中 "个数"（个|数据）；"这个数据" 的 "个" 是量词
    # 而非"计数"诉求。用**前缀**区分：这个/那个/一个 → 量词；几个/多数 → 计数。
    "个数": {"prefix": ("这", "那", "哪", "一", "每", "单", "整", "份", "些", "两", "半")},
    # "图书馆" 里的"图"不是图表诉求
    "图":   {"prefix": ("蓝", "宏", "意", "试", "草", "地", "雄", "构", "企"), "suffix": ("书",)},
    "表":   {"prefix": ("发", "代", "外", "报", "图", "仪", "手", "目"), "suffix": ("示", "情", "演", "达")},
}

# 关键词匹配专用：去掉虚词，让"这份数据的质量如何"能命中"数据质量"。
_MATCH_STRIP = ("的", "了", "呢")
_ASCII_KW_RE = re.compile(r'[a-z0-9_.\-]+')
_STRIP_TRANS = str.maketrans('', '', ''.join(_MATCH_STRIP))


def _match_text(text: str) -> str:
    """关键词匹配专用文本：仅去虚词，不改原 query（实体抽取仍用原文）。"""
    if not text:
        return ''
    return text.translate(_STRIP_TRANS)


def kw_hit(kw: str, text: str) -> bool:
    """关键词是否命中（带边界与语境约束，防子串污染）。

    背景：旧实现裸用 `kw in text`，导致
      "hi"⊂"which"、"500"⊂"2020/5000"、"值"⊂"价值"、"是什么"⊂"是什么原因"、
      "表"⊂"发表" 等误命中，直接污染意图分数。
    """
    if not kw or not text:
        return False
    k = _match_text(kw).lower()
    low = _match_text(text).lower()
    if not k or k not in low:
        return False
    # ① 纯数字：两侧不得再是数字（"500" 不应命中 "2020/5000"）
    if k.isdigit():
        return re.search(r'(?<!\d)' + re.escape(k) + r'(?!\d)', low) is not None
    # ② 英文词：要求词边界，容忍复数 s（"chart"/"charts"；"hi" 不再命中 "which"）
    if _ASCII_KW_RE.fullmatch(k):
        return re.search(r'(?<![a-z0-9_])' + re.escape(k) + r's?(?![a-z0-9_])', low) is not None
    # ③ 中文：按语境否决表过滤
    neg = _KW_NEGATIVE.get(k)
    if not neg:
        return True
    pres = neg.get("prefix", ())
    sufs = neg.get("suffix", ())
    for m in re.finditer(re.escape(k), low):
        i, j = m.start(), m.end()
        if pres and low[max(0, i - 1):i].endswith(pres):
            continue
        if sufs and low[j:j + 4].startswith(sufs):
            continue
        return True
    return False


def _intent_priority(name: str) -> int:
    try:
        return _INTENT_PRIORITY.index(name)
    except ValueError:
        return len(_INTENT_PRIORITY)


def _rank_scores(scores: Dict[str, float]) -> List[Tuple[str, float]]:
    """确定性排序：分数降序，同分按显式优先级。取代裸 sorted（受 dict 插入序影响）。"""
    return sorted(scores.items(), key=lambda x: (-x[1], _intent_priority(x[0])))


def _ordered_intents(*dicts) -> List[str]:
    """确定性意图遍历顺序：先按 INTENT_CATEGORIES 定义序，再补外部键（字典序）。"""
    out = list(INTENT_CATEGORIES)
    extra: List[str] = []
    for d in dicts:
        if not d:
            continue
        for k in d:
            if k not in INTENT_CATEGORIES and k not in extra:
                extra.append(k)
    return out + sorted(extra)


# ---- 强信号优先规则（P0-3）：硬故障词命中 → 排障意图决定性加权 ----
# 场景："上传文件报错了" 同时命中 operation 的 "上传文件" 与 troubleshoot 的 "报错"，
# 后者才是用户真实诉求。此前靠分数偶然性，现改为显式规则。
_HARD_TROUBLE_KW: Tuple[str, ...] = (
    "报错", "错误", "白屏", "连不上", "闪退", "打不开", "错误码", "崩溃",
    "失败", "异常", "超时", "卡住", "无法", "没反应", "加载不出", "有问题", "不对劲",
)
_HARD_TROUBLE_BOOST = 1.2
# 计算类信号权重：需 ≥ 1.2 才能让 confidence（0.55+score*0.15）越过
# INTENT_CONF_FLOORS['computation']=0.70 的下限，避免每次都被迫澄清。
_COMPUTATION_SIGNAL = 1.2


def _dominant_signal_boost(text: str, scores: Dict[str, float]) -> Dict[str, float]:
    """命中硬故障词 → troubleshoot 决定性加权（不再被 "上传文件/导出" 等功能词抢走）。"""
    if any(kw_hit(k, text) for k in _HARD_TROUBLE_KW):
        scores["troubleshoot"] = scores.get("troubleshoot", 0.0) + _HARD_TROUBLE_BOOST
    return scores


# ---- 域外检测（P0-4）：与数据/金融/统计无关的通用诉求 ----
_OUT_OF_SCOPE_KW: Tuple[str, ...] = (
    # 生活/天气
    "天气", "气温", "下雨", "下雪", "台风", "空气质量", "多少度", "穿什么", "温度",
    # 娱乐/社交话题
    "笑话", "讲个故事", "讲故事", "唱歌", "猜谜", "星座", "运势", "算命", "占卜", "塔罗",
    "彩票", "开奖", "双色球", "电影", "电视剧", "综艺", "小说", "游戏攻略", "菜谱", "做饭",
    "旅游攻略", "减肥", "健身计划",
    # 与业务无关的写作/编码
    # 注：这里用「名词」而非「动词+名词」做词条——中文插入量词很常见
    # （"写作文" vs "帮我写个作文"/"写一篇作文"），裸子串匹配只有名词粒度才不漏。
    "作文", "简历", "演讲稿", "请假条",
    "翻译一下", "写首诗", "写代码", "写脚本", "写程序", "编程题", "脚本", "python脚本",
    # 时间/常识
    "几点", "星期几", "今天几号",
)
# 域内否决词：只要出现，说明仍在本工具业务范围内，不做域外判定
_OOS_VETO: Tuple[str, ...] = (
    "数据", "文件", "表格", "数据集", "csv", "excel", "字段", "列", "行", "样本",
    "指标", "统计", "分析", "清洗", "建模", "图表", "报告", "回归", "相关性",
    "股票", "基金", "债券", "金融", "保险", "行情", "估值", "风险", "组合",
    "销量", "销售", "客户", "用户", "营收", "利润", "成本", "收入", "支出", "消费",
    "转化", "留存", "人群", "评分", "模型",
    # 计数量词：「统计请假条数量」「一共有多少条」是查数，不是写作文（2026-09-18 漏报修复）
    "数量", "个数", "多少个", "多少条", "多少笔", "多少人", "几条", "几笔", "几人", "计数",
)
_OOS_SCORE = 1.2


def _out_of_scope_score(text: str) -> float:
    """域外请求分（>0 表示命中）；出现任何域内业务词则否决，避免误伤。"""
    low = text.lower()
    if any(v in low for v in _OOS_VETO):
        return 0.0
    return _OOS_SCORE if any(k in low for k in _OUT_OF_SCOPE_KW) else 0.0


# ---- 无信号输入的处理（P0-3b）：不再一律硬猜闲聊 ----
# 旧实现：无任何关键词命中且长度 ≤ 8 → chat=0.8（> 澄清阈值 0.55）→ 永不澄清，
# 于是 "它呢""指标""我的数据有问题" 全部被当闲聊。
_ACK_WORDS: Tuple[str, ...] = (
    "嗯", "嗯嗯", "好的", "好", "收到", "明白", "明白了", "了解", "ok", "行", "可以",
    "是的", "对", "对的", "没事", "感谢", "多谢", "谢了", "不客气", "辛苦", "辛苦啦",
    "谢谢", "谢谢啦", "拜拜", "再见", "晚安", "早", "早上好", "哈喽", "嗨", "哈哈",
)
_ACK_SET = {w.lower() for w in _ACK_WORDS}
_ACK_SCORE = 0.9          # 明确应答语：判闲聊，不澄清
_NOSIGNAL_CHAT_SCORE = 0.3  # 无信号：低于澄清阈值 → 触发澄清而不是硬猜
_ACK_MAX_LEN = 6

# 混合意图：寒暄与业务诉求共存时，业务诉求优先（"你好，帮我看看这个数据"）
_CHAT_DEMOTE = 0.25


def _is_acknowledgement(text: str) -> bool:
    """是否是明确的社交应答语（可以安全判闲聊、无需澄清）。"""
    t = text.strip().strip("。！!~～.，,、？? \t\n")
    if not t or len(t) > _ACK_MAX_LEN:
        return False
    return t.lower() in _ACK_SET


CLARIFICATION_THRESHOLD = 0.55      # 最高分意图低于该值 → 需要澄清
MARGIN_THRESHOLD = 0.18             # 前两名得分差距小于该值 → 意图不明，需要澄清

# ---- 难例回流（持续训练闭环）：低置信/澄清路由落盘，eval 动态并入评测集 ----
_CASES_PATH = 'output/bad_cases/intent_cases.jsonl'
_CASES_MAX = 200
# P1④ 难例回流写文件并发保护锁（多会话/多线程下避免 JSONL 交错写坏）
_CASES_LOCK = threading.Lock()
# P2 数据卫生：测试环境默认不落盘。此前 pytest 跑处理链路会把用例
# （如 "请继续"）写进生产闭环文件，再被 eval 动态并入评测集 → 评测自身被污染。
_CASES_REFLOW_ENABLED = os.environ.get('TESTING', '').strip().lower() not in ('1', 'true', 'yes')


def set_case_reflow_enabled(enabled: bool) -> None:
    """显式开关难例回流（诊断脚本/临时隔离用）。"""
    global _CASES_REFLOW_ENABLED
    _CASES_REFLOW_ENABLED = bool(enabled)


def record_intent_case(text: str, predicted: str, confidence: float, meta: Dict = None) -> None:
    """生产难例回流：低置信/触发澄清的请求记入 JSONL（去重、限量），供评测集动态并入。

    这是意图识别的"在线训练"数据来源——线上踩到的难例自动成为回归用例。"""
    if not text or not text.strip():
        return
    if not _CASES_REFLOW_ENABLED:
        return
    try:
        with _CASES_LOCK:
            os.makedirs(os.path.dirname(_CASES_PATH), exist_ok=True)
            existing = set()
            if os.path.exists(_CASES_PATH):
                try:
                    with open(_CASES_PATH, encoding='utf-8') as f:
                        for line in f.readlines()[-_CASES_MAX * 2:]:
                            try:
                                existing.add(json.loads(line).get('q'))
                            except Exception:
                                continue
                except OSError:
                    pass
            q = text.strip()[:120]
            if q in existing:
                return
            item = {'q': q, 'pred': predicted, 'confidence': round(confidence, 3),
                    'ts': time.strftime('%Y-%m-%d %H:%M:%S'), 'meta': meta or {}}
            with open(_CASES_PATH, 'a', encoding='utf-8') as f:
                f.write(json.dumps(item, ensure_ascii=False) + '\n')
    except Exception:
        pass  # 回流失败绝不影响主流程


def classify_intent(text: str, history: Optional[List[dict]] = None) -> Dict[str, Any]:
    """
    规则版意图分类（不走 LLM，零额外 quota）。
    返回:
      intent, confidence, entities, needs_clarification, missing_info, scores, alternatives
    """
    normalized = normalize_query(text)
    entities = extract_entities(normalized)

    # ---- Layer1：关键词规则（边界/语境受控，不再是裸子串匹配）----
    # 同时匹配「原文」与「同义词归一化文本」：normalize_query 会把业务短语替换成
    # 规范术语（如 "政策评估"→"DID"），若只看归一化文本会丢掉原文里的规则信号。
    scores: Dict[str, float] = {k: 0.0 for k in INTENT_CATEGORIES}
    for intent, kws in _INTENT_KEYWORDS.items():
        total = 0.0
        for kw, w in kws:
            if kw_hit(kw, normalized) or (normalized != text and kw_hit(kw, text)):
                total += w
        scores[intent] = total

    # Layer1.4 强信号优先：硬故障词命中 → troubleshoot 决定性加权
    try:
        scores = _dominant_signal_boost(normalized, scores)
    except Exception as _e:
        logger.warning('强信号优先规则失败（已降级到基础规则）: %s', _e)

    # Layer1.45 计算类信号：此前该意图只能由 eval 特判产生，分类器永不返回 → 正式接线
    try:
        if detect_special_intent(normalized) == 'computation':
            scores['computation'] = scores.get('computation', 0.0) + _COMPUTATION_SIGNAL
    except Exception as _e:
        logger.debug('计算类信号判定失败（忽略）: %s', _e)

    # Layer1.48 域外请求：命中文档/金融/统计无关诉求
    try:
        _oos = _out_of_scope_score(normalized)
        if _oos > 0:
            scores['out_of_scope'] = scores.get('out_of_scope', 0.0) + _oos
    except Exception as _e:
        logger.debug('域外判定失败（忽略）: %s', _e)

    # Layer1.5：实体感知增强——出现公司/股票/基金实体时按问询方向加权
    # （解决"宁德时代怎么样"这类"纯实体+口语问询"被误判为 chat 的盲区）
    try:
        scores = _entity_intent_boost(normalized, scores)
    except Exception as _e:
        logger.warning('意图实体增强失败（已降级到基础规则）: %s', _e)

    # Layer1.55：指标取值问询——"指标名 + 查看式措辞（无分析语气）" → 查数
    # （修复边界用例「帮我看看收益率」被判 analysis：它要的是数值，不是解读）
    try:
        scores = _indicator_lookup_boost(normalized, entities, scores)
    except Exception as _e:
        logger.warning('指标取值问询增强失败（已降级到基础规则）: %s', _e)

    # Layer1.56：时间限定查数——"最近30天的成交量"这类"时间 + 数据名词"问法
    try:
        scores = _time_query_boost(entities, scores)
    except Exception as _e:
        logger.warning('时间限定查数增强失败（已降级到基础规则）: %s', _e)

    # Layer1.6：DST 多轮上下文联动——短句续接继承上一轮意图
    # （上一轮在做基金分析，用户回"再看看它的回撤"应延续 analysis/nlq，而非判成 chat）
    try:
        scores = _continuation_boost(text, history, scores)
    except Exception as _e:
        logger.warning('意图 DST 续接增强失败（已降级到基础规则）: %s', _e)

    # Layer1.7 混合意图：寒暄 + 业务诉求共存 → 业务优先
    # （"你好，帮我看看这个数据" 此前被判 chat，业务诉求被寒暄词吞掉）
    if scores.get('chat', 0.0) > 0:
        _non_chat_top = max((v for k, v in scores.items() if k != 'chat'), default=0.0)
        if _non_chat_top > 0:
            scores['chat'] = scores['chat'] * _CHAT_DEMOTE

    # Layer2：示例库相似度修正意图分数（③，有界微调）。失败则保持 Layer1 分数。
    # 记住 Layer1 是否完全无信号：无信号输入的兜底必须据此判定，
    # 否则 Layer2 的"静默预算"会先把分数抬起来，使兜底分支永不触发（"好的"被判需澄清）。
    _l1_silent = not any(v > 0 for v in scores.values())
    try:
        scores = classify_intent_layer2(normalized, scores)
    except Exception as _e:
        logger.warning('意图示例库相似度修正失败（已保持 Layer1 分数）: %s', _e)

    # ---- Layer1.9 无信号兜底：不再一律硬猜闲聊 ----
    # （此前 "它呢""指标" 命中 chat=0.8 > 澄清阈值 → 永不澄清；现明确区分
    #   "社交应答语"（判闲聊不澄清）与"无法理解"（低分 → 触发澄清））
    if _l1_silent:
        if _is_acknowledgement(text):
            scores = {k: 0.0 for k in scores}
            scores['chat'] = _ACK_SCORE
        elif not any(v > 0 for v in scores.values()):
            scores['chat'] = _NOSIGNAL_CHAT_SCORE

    ranking = _rank_scores(scores)
    top_intent, top_score = ranking[0]
    second_score = ranking[1][1] if len(ranking) > 1 else 0.0

    if top_score <= 0:
        return {
            "intent": "chat", "confidence": 0.5,
            "entities": entities, "needs_clarification": True,
            "missing_info": ["未能理解这条消息，请补充你想做什么"],
            "scores": scores, "normalized": normalized,
            "alternatives": [], "action": INTENT_TO_ACTION.get("chat", "chat"),
        }

    confidence = min(0.99, 0.55 + top_score * 0.15)
    needs_clarification = False
    missing_info: List[str] = []

    # 低置信度 / 意图含混 且不是明确闲聊 → 需要澄清
    if top_score < CLARIFICATION_THRESHOLD:
        needs_clarification = True
        missing_info.append("消息不够明确，无法确定具体意图")
    elif top_intent != "chat" and (top_score - second_score) < MARGIN_THRESHOLD:
        needs_clarification = True
        missing_info.append(f"在「{top_intent}」与「{ranking[1][0]}」之间不确定")

    # 特定意图缺关键实体 → 提示
    if top_intent in ("analysis", "nlq", "visualization") and entities.get("file"):
        # 已带文件：OK
        pass
    if top_intent in ("nlq", "analysis") and not entities.get("indicator") and len(normalized) < 20:
        if "多少" in normalized or "指标" in normalized:
            missing_info.append("未提取到具体指标/维度的信息")

    return {
        "intent": top_intent,
        "confidence": round(confidence, 3),
        "entities": entities,
        "needs_clarification": needs_clarification,
        "missing_info": missing_info,
        "scores": scores,
        "normalized": normalized,
        "action": INTENT_TO_ACTION.get(top_intent, "chat"),
        # 混合意图：输出前 3 候选，供上层在"意图接近"时展示/二次选择（P1）
        "alternatives": [{"intent": i, "score": round(s, 3)}
                         for i, s in ranking[:3] if s > 0],
    }


# ==================== 三层混合路由的 Layer2/Layer3（③）====================

# Layer2：每个意图的示例库（供相似度匹配；也是 TF-IDF 的"训练数据"，扩充即训练）
INTENT_EXAMPLES: Dict[str, List[str]] = {
    "nlq": [
        "查一下上个月的销售额", "帮我看看最近的收益是多少", "这个基金的年化收益率是多少",
        "总共有多少个省份", "排名前10的股票", "最近30天的成交量",
        "看看茅台最新市值", "腾讯最新股价多少", "查一下上个月销量前十的产品",
        "这份数据一共多少行", "销售额总量是多少", "最近一周的活跃用户数",
    ],
    "analysis": [
        "帮我分析一下这个数据", "为什么最大回撤这么大", "解读一下这只基金的业绩",
        "探究销量下滑的原因", "评估一下这个策略的表现",
        "宁德时代怎么样", "比亚迪和宁德时代哪个好", "五粮液最近走势怎么样",
        "电器行业前景怎么样", "游戏行业竞争格局如何", "这个板块有投资机会吗",
        "这份数据缺失严重吗", "数据里有多少异常值", "分析一下数据质量",
        "做个DID政策评估", "跑一下双重差分回归", "分析政策试点的因果效应",
        "全面分析电器行业", "分析一下白酒行业的竞争格局",
    ],
    "visualization": [
        "画个柱状图", "用折线图展示趋势", "做一张散点图", "热力图看一下相关性", "画个饼图",
        "帮我把销量画成折线图", "画一张营收对比柱状图", "用直方图看分布",
    ],
    "knowledge": [
        "什么是久期", "解释一下夏普比率", "介绍一下 DID", "PSI是什么意思", "科普一下凸性",
        "夏普比率怎么算的", "WOE和IV有什么区别", "市盈率指的是什么",
    ],
    "operation": [
        "怎么上传文件", "如何导入数据", "在哪里生成图表", "功能怎么用", "清洗数据在哪操作",
        "怎么导出报告", "怎么把图表保存下来", "报告怎么分享给同事", "怎么把结果另存为",
    ],
    "troubleshoot": [
        "系统报错了", "页面白屏怎么办", "连不上数据库", "500错误", "为什么闪退",
        "上传文件失败了", "图表生成卡住了",
    ],
    "agent": [
        "做一份完整的研究报告", "综合分析这只基金值不值得买", "全面的尽调", "整体评估这个组合",
        "全面分析电器行业", "多维度分析这家公司", "统筹分析整个投资组合",
    ],
    "chat": [
        "你好", "谢谢", "在吗", "辛苦啦", "嗨",
    ],
    "computation": [
        "算一下这个基金的总收益率", "帮我算算最大回撤", "计算一下这笔投资的年化收益率",
        "算一下这个组合的夏普比率", "帮我算出这只基金的波动率",
    ],
    "out_of_scope": [
        "今天天气怎么样", "讲个笑话", "帮我写个作文", "现在几点了", "推荐一部电影",
    ],
}

# Layer3：意图边界 few-shot 判例（写死判定规则，防止模糊意图被误判）
INTENT_BOUNDARY_CASES: List[Dict[str, str]] = [
    {"query": "帮我看看收益率", "label": "nlq", "reason": "要数据值→查数，不是知识问答"},
    {"query": "收益率是什么意思", "label": "knowledge", "reason": "问概念定义→知识问答"},
    {"query": "帮我把收益画出来", "label": "visualization", "reason": "要图→可视化"},
    {"query": "为什么收益率这么低", "label": "analysis", "reason": "问原因/归因→分析"},
    {"query": "算一下这个基金的夏普比率", "label": "computation", "reason": "明确要计算指标→计算类"},
    {"query": "夏普比率怎么算的", "label": "knowledge", "reason": "问计算原理/公式→知识问答"},
    {"query": "帮我建个模型预测销量", "label": "model_build", "reason": "要建模训练→建模"},
    {"query": "这个模型训练出来准不准", "label": "analysis", "reason": "评估结果→分析"},
    {"query": "今天天气怎么样", "label": "out_of_scope", "reason": "与数据/金融/统计无关→域外"},
    {"query": "分析一下天气对销量的影响", "label": "analysis", "reason": "含业务诉求（销量）→归因分析，非域外"},
    {"query": "上传文件报错了", "label": "troubleshoot", "reason": "含硬故障词→排障，不是操作教程"},
    {"query": "怎么上传文件", "label": "operation", "reason": "问功能用法→操作指导"},
    {"query": "帮我算算这个组合的夏普比率", "label": "computation", "reason": "明确要算出指标值→计算类"},
]


def _example_sim_scores(text: str) -> Dict[str, float]:
    """Layer2：对每个意图的示例库做相似度，返回 {intent: 分数}（0-1）"""
    sims = {}
    low = text.lower()
    try:
        # 优先用可用的向量/字符相似度（embedding 有缓存则用，否则字符 n-gram 相似）
        for intent, exs in INTENT_EXAMPLES.items():
            best = 0.0
            for ex in exs:
                s = _char_similarity(low, ex.lower())
                if s > best:
                    best = s
            sims[intent] = best
    except Exception:
        pass
    return sims


def _char_similarity(a: str, b: str) -> float:
    """轻量字符/双字重叠相似度（无需模型，0-1）"""
    if not a or not b:
        return 0.0
    a2 = set(a[i:i + 2] for i in range(len(a) - 1))
    b2 = set(b[i:i + 2] for i in range(len(b) - 1))
    inter = a2 & b2
    if not inter:
        return 0.0
    return len(inter) / max(1, min(len(a2), len(b2)))


# Layer2 TF-IDF 缓存：(vectorizer, 示例矩阵, 对齐的意图列表)
_tfidf_cache = None


def _tfidf_example_sims(text: str) -> Dict[str, float]:
    """Layer2 统计学习增强：示例库上拟合 TF-IDF 字符 n-gram，余弦相似度取各意图最优。

    比纯字符重叠泛化更好（可捕捉"格局如何"≈"竞争格局分析"这类组合关系）；
    sklearn 不可用或异常时返回空 dict（回退字符相似度）。"""
    global _tfidf_cache
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity
        if _tfidf_cache is None:
            examples, intents = [], []
            for intent, ex_list in INTENT_EXAMPLES.items():
                for e in ex_list:
                    examples.append(e)
                    intents.append(intent)
            vec = TfidfVectorizer(analyzer='char', ngram_range=(1, 2), lowercase=True)
            mat = vec.fit_transform(examples)
            _tfidf_cache = (vec, mat, intents)
        vec, mat, intents = _tfidf_cache
        sims = cosine_similarity(vec.transform([text]), mat)[0]
        best: Dict[str, float] = {}
        for intent, s in zip(intents, sims):
            best[intent] = max(best.get(intent, 0.0), float(s))
        return best
    except Exception:
        return {}


def _entity_intent_boost(text: str, scores: Dict[str, float]) -> Dict[str, float]:
    """Layer1.5 实体感知：消息含公司/股票/基金实体时，按问询方向给意图加权。

    - 分析向问询（怎么样/前景/值不值/哪个好…）→ analysis +
    - 查数向问询（多少/市值/股价/数量/个数…）→ nlq +
    实体识别复用 finance_intent（纯规则、零额外开销），异常时静默跳过。"""
    try:
        from finance_intent import extract_financial_instruments
        ents = extract_financial_instruments(text)
    except Exception:
        return scores
    if not ents:
        return scores
    low = text.lower()
    if any(k in low for k in _ENTITY_ANALYSIS_KW):
        scores['analysis'] = scores.get('analysis', 0) + 1.5
    if any(k in low for k in _ENTITY_NLQ_KW):
        scores['nlq'] = scores.get('nlq', 0) + 1.0
    return scores


# 实体出现时的分析向/查数向问询词
_ENTITY_ANALYSIS_KW = ('怎么样', '如何', '前景', '走势', '好不好', '哪个好', '对比', '值不值',
                       '值得买', '买不买', '能不能买', '分析', '评估', '看看', '研究', '判断',
                       '点评', '风险', '机会', '潜力', '竞争力', '会涨', '会跌', '抄底', '建仓')
_ENTITY_NLQ_KW = ('多少', '几个', '股价', '市值', '点位', '排名', '涨跌', '涨幅', '营收', '最新价')

# ---- Layer1.55 指标取值问询：指标名 + 查看式措辞（且无分析语气）→ nlq ----
# 边界用例「帮我看看收益率」要的是数据值而非解读；与「为什么收益率这么低」（归因）区分。
_LOOKUP_KW = ('看看', '看一下', '瞧瞧', '查一下', '多少', '给我看', '是多少', '几')
_ANALYSIS_TONE_KW = ('分析', '解读', '为什么', '为何', '原因', '评估', '诊断', '怎么样', '如何看',
                     '前景', '对比', '哪个好', '值不值', '值得买', '买不买', '归因', '趋势',
                     '走势', '规律', '点评', '是不是', '合理吗', '怎么样', '看好', '风险',
                     '机会', '判断', '评价', '理解', '洞察')
_INDICATOR_LOOKUP_BOOST = 0.9


def _indicator_lookup_boost(text: str, entities: Dict, scores: Dict[str, float]) -> Dict[str, float]:
    """出现具体指标名 + 查看式措辞 + 无分析语气 → 判定为查数（nlq）。"""
    if not entities.get('indicator'):
        return scores
    low = text.lower()
    if not any(k in low for k in _LOOKUP_KW):
        return scores
    if any(k in low for k in _ANALYSIS_TONE_KW):
        return scores
    scores['nlq'] = scores.get('nlq', 0.0) + _INDICATOR_LOOKUP_BOOST
    return scores


# ---- Layer1.56 时间限定查数：出现时间范围且无其他意图信号 → nlq ----
# 覆盖"最近30天的成交量"这类"时间 + 数据名词"但指标名不在词表内的问法。
_TIME_QUERY_BOOST = 0.9


def _time_query_boost(entities: Dict, scores: Dict[str, float]) -> Dict[str, float]:
    if not entities.get('time'):
        return scores
    if any(k != 'nlq' and v > 0 for k, v in scores.items()):
        return scores
    scores['nlq'] = scores.get('nlq', 0.0) + _TIME_QUERY_BOOST
    return scores


# ---- Layer1.6 DST 多轮联动：短句续接继承上一轮意图 ----
_CONTINUATION_HINTS = ('再', '也', '还', '呢', '继续', '然后', '接下来', '顺便', '那它', '它')
_CONTINUABLE_INTENTS = ('analysis', 'nlq', 'visualization', 'computation', 'agent')


def _continuation_boost(text: str, history: Optional[List[dict]],
                        scores: Dict[str, float]) -> Dict[str, float]:
    """对话状态跟踪（DST）的最小实现：上一轮在做某任务、本轮为无强信号的短句续接
    （如"再看看回撤""然后呢"）时，继承上一轮意图，消除省略/指代歧义。"""
    if not history:
        return scores
    last_intent = None
    for t in reversed(history[-3:]):
        if t.get('intent'):
            last_intent = t['intent']
            break
    if last_intent not in _CONTINUABLE_INTENTS:
        return scores
    stripped = text.strip()
    if not stripped or len(stripped) > 14:
        return scores
    top_kw = max(scores.values()) if scores else 0.0
    # 强信号（关键词已明确意图）时不覆盖；弱信号/无信号短句才继承
    if top_kw >= 0.6:
        return scores
    if any(h in stripped for h in _CONTINUATION_HINTS) or top_kw == 0.0:
        scores[last_intent] = scores.get(last_intent, 0) + 1.2
    return scores


# Layer2 融合预算（P0-2 修复"分层量纲失控"）
#   旧实现 `kw + 2.0*ex`：L2 可加最多 2.0，而 L1 关键词权重仅 0.3~1.0，
#   后置层因此能整层推翻前置层（实测翻盘率 10.9%、后置层独裁 19.6%）。
#   现改为**与 L1 分档绑定的有界微调**：
_L2_SIM_FLOOR = 0.25     # 相似度低于该值视为噪声，不计分
_L2_SIM_GAIN = 2.0       # 相似度 → 贡献分放大系数（仅用于预算内取舍）
_L2_TIE_EPS = 0.10       # 与 L1 领先者分差 ≤ 该带宽 → L2 参与微调（平局决胜）
_L2_TIE_ADD = 1.0        # 带宽内最大 L2 加成
_L2_SILENT_ADD = 0.5     # L1 完全无信号时的最大 L2 加成（由 L2 定胜负）
#   注：0.5 < CLARIFICATION_THRESHOLD(0.55) → L1 全零的输入即便 L2 能选出候选，
#   也会正确触发澄清，而不是被硬猜成某个意图。

# 不接受 L2 微调的意图：这两个类别必须由**专用检测器**产生，不能被示例相似度"凭空发明"。
# 实测反例："它现在贵不贵" 因与示例「现在几点了」字符相近，被 L2 判成 out_of_scope。
_L2_EXCLUDED = ('out_of_scope', 'computation')


def classify_intent_layer2(text: str, keyword_scores: Dict[str, float]) -> Dict[str, float]:
    """Layer2：示例相似度作为**有界微调**叠加在关键词分（Layer1）之上。

    三档预算（保证 L2 只能"平局决胜/填空"，不能推翻 L1 的明确领先者）：
      · L1 已明确领先（与最高分差 > _L2_TIE_EPS）→ L2 不加分；
      · L1 平局或接近 → 在 _L2_TIE_ADD 预算内决胜；
      · L1 全零 → 在 _L2_SILENT_ADD 预算内定胜负
        （保住"这份数据的质量如何"这类断词差异，而不是退化成闲聊）。
    返回 {intent: 融合分数}。"""
    sims = _example_sim_scores(text)
    tfidf_sims = _tfidf_example_sims(text)
    if not sims and not tfidf_sims:
        return dict(keyword_scores)
    top_kw = max(keyword_scores.values()) if keyword_scores else 0.0
    # 字符重叠与 TF-IDF 相似度取较大者（TF-IDF 泛化更好，字符重叠兜底）
    exs = {i: max(sims.get(i, 0.0), tfidf_sims.get(i, 0.0))
           for i in _ordered_intents(keyword_scores, sims, tfidf_sims)}
    best_ex = max(exs.values()) if exs else 0.0
    merged: Dict[str, float] = {}
    for intent, ex in exs.items():
        kw = keyword_scores.get(intent, 0.0)
        if intent in _L2_EXCLUDED:
            # 域外/计算类只认专用检测器（_out_of_scope_score / detect_special_intent）
            merged[intent] = kw
            continue
        if top_kw <= 0:
            budget = _L2_SILENT_ADD
        elif abs(kw - top_kw) <= _L2_TIE_EPS:
            budget = _L2_TIE_ADD
        else:
            budget = 0.0          # L1 明确领先者之外 → L2 不给分（不得翻盘）
        add = 0.0
        if budget > 0 and best_ex > 0 and ex > _L2_SIM_FLOOR:
            # 预算内按相似度**占比**分配：保留 L2 内部的相对排序，
            # 避免"封顶并列"（旧写法 min(budget, 2*ex) 会让多个意图同时顶到上限而打分相同）
            add = budget * min(1.0, ex / best_ex)
        merged[intent] = kw + add
    return merged


def _extract_first_json(content: str) -> Optional[dict]:
    """从 LLM 输出中提取第一个完整 JSON 对象（容忍 ```json 围栏与前后缀文字）"""
    if not content:
        return None
    content = content.strip().strip('`')
    if content.startswith('json'):
        content = content[4:].strip()
    m = re.search(r'\{[^{}]*\}', content, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def refine_with_llm(text: str, candidates: List[str], llm_config: dict = None,
                    history: Optional[List[dict]] = None) -> Optional[Dict[str, Any]]:
    """Layer3 LLM 精排：仅在前两层歧义时调用（省成本），只在 Top-N 候选内做最终判断。

    Prompt 工程：意图体系(INTENT_SPECS)动态生成定义/触发/排除 + 边界判例 few-shot +
    多轮上下文提示 + 负面约束 + 强制 JSON 输出；temperature=0；解析失败重试一次，
    仍失败返回 None（回退前两层结果，绝不阻断主流程）。"""
    if not candidates or not llm_config:
        return None
    cand = [c for c in candidates if c in INTENT_SPECS][:4]
    if not cand:
        return None
    specs = "\n".join(
        f"- {c}：{INTENT_SPECS[c]['desc']}｜触发：{INTENT_SPECS[c]['triggers']}｜排除：{INTENT_SPECS[c]['exclusions']}"
        for c in cand)
    boundary_fs = "\n".join(
        f'- 「{c["query"]}」→ {c["label"]}' for c in INTENT_BOUNDARY_CASES)
    hist_note = ''
    if history:
        last = next((h.get('intent') for h in reversed(history[-3:]) if h.get('intent')), None)
        if last:
            hist_note = f"（上一轮意图为「{last}」，短句续接时优先延续同一任务）\n"
    prompt = (
        "你是意图精排器。只在下列候选意图中做最终判断，严格只输出一行 JSON：\n"
        '{"intent": "<候选之一>", "confidence": 0~1, "rationale": "不超过20字的依据", "needs_clarification": false}\n\n'
        f"候选意图（定义｜触发｜排除）：\n{specs}\n\n"
        f"{hist_note}"
        "负面约束：不要输出候选之外的意图；不要把查询类问题误判为操作类；"
        "不要因含礼貌用语而判为 chat；置信度低于 0.6 时 needs_clarification 置为 true。\n"
        f"边界判例：\n{boundary_fs}\n\n"
        f"用户输入：{text}\n只输出 JSON："
    )
    for attempt in range(2):
        try:
            from llm_client import chat
            resp = chat([{"role": "user", "content": prompt}], llm_config,
                        max_tokens=150, temperature=0)
            content = (resp.get("content") or "").strip() if isinstance(resp, dict) else str(resp).strip()
            data = _extract_first_json(content)
            if data and data.get('intent') in cand:
                try:
                    conf = float(data.get('confidence', 0.5))
                except (TypeError, ValueError):
                    conf = 0.5
                conf = max(0.0, min(1.0, conf))
                return {
                    'intent': data['intent'],
                    'confidence': conf,
                    'rationale': str(data.get('rationale') or '')[:60],
                    'needs_clarification': bool(data.get('needs_clarification')) or conf < 0.6,
                }
        except Exception as e:
            logger.debug("Layer3 精排失败(%d/2): %s", attempt + 1, e)
    return None


# ---- 后置校验：意图合法性 + 高风险意图置信度下限 ----
# 高风险意图（多步执行/消耗额度/触发真实计算）需更高置信度，不达标时澄清而非硬走流程
INTENT_CONF_FLOORS: Dict[str, float] = {
    'agent': 0.75,        # 多步 Agent 消耗多轮 LLM 额度
    'computation': 0.70,  # 触发后端真实计算
}


def validate_intent(text: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """后置校验层：① 意图合法性（未知意图回退 chat+澄清）；② 高风险意图置信度下限
    （agent/computation 低于下限 → 标记澄清而非直接执行）；③ 保留实体槽位缺失信息。"""
    intent = result.get('intent')
    if intent not in INTENT_CATEGORIES:
        result.update({'intent': 'chat', 'confidence': 0.5,
                       'needs_clarification': True, 'missing_info': ['意图未识别']})
        return result
    floor = INTENT_CONF_FLOORS.get(intent)
    if floor and result.get('confidence', 1.0) < floor:
        result['needs_clarification'] = True
        result.setdefault('missing_info', []).append(
            f'「{intent}」为高风险意图且置信度不足（<{floor}），建议澄清后执行')
    return result


# ==================== 澄清策略（2.2） ====================

_CLARIFY_TEMPLATES: Dict[str, str] = {
    "analysis": "好的，为了更精准地帮您分析，请确认：\n1. 您指的是哪个数据集？（上传的文件 / 数据库连接 / 金融数据）\n2. 重点看哪些指标或维度？\n3. 希望做描述统计、趋势分析，还是某类专项检验？",
    "nlq": "我来帮您查数据，请进一步确认：\n1. 查哪个数据集/表格？\n2. 要的是『总量』还是『具体指标值』？\n3. 时间范围是多少？（最近7天 / 本月 / 本季度？）",
    "visualization": "想给您画图，请确认：\n1. 用哪个数据集的哪些列？（X轴 / Y轴）\n2. 想用什么图？（折线 / 柱状 / 散点 / 饼图）\n3. 是否需要按某列分组或筛选？",
    "operation": "您说的是哪个功能呢？我可以按这几类给您指引：\n1. 📊 数据分析（描述统计/相关/分组）\n2. 📈 可视化与图表\n3. 🧹 数据清洗\n4. 🤖 机器学习建模\n5. 📚 金融/税务知识问答",
    "troubleshoot": "遇到问题可以先告诉我：\n1. 报错信息或截图里的关键文字是什么？\n2. 是在哪个功能/页面出现的？\n3. 大概什么操作会触发？我再帮您排查。",
    "agent": "这个任务我可以联动多个分析模块完成，请补充：\n1. 数据文件是什么？\n2. 想得到什么结论（如：因果评估 / 组合表现 / 风险诊断）？\n3. 有没有偏好的分析维度？",
    "chat": "我不太确定你的意思，是想：\n1. 📊 分析某份数据？\n2. 🔍 查某个数据指标？\n3. 📚 咨询金融/税务知识？\n4. ⚙️ 了解功能怎么用？告诉我多一点，我好帮您。",
    "knowledge": "想了解哪个概念或知识点呢？可以告诉我具体的术语（如『久期』『夏普比率』『DID』『PSI』），我给出准确的解释。",
    "computation": "要算什么指标呢？请告诉我：\n1. 基于哪份数据 / 哪个标的？\n2. 指标名（如夏普比率 / 最大回撤 / 年化收益率）？\n3. 需要的话补充时间区间。",
    "out_of_scope": "这个问题超出我的能力范围了 😅 我是数据与金融分析助手，可以帮你：\n1. 📊 分析数据文件\n2. 🔍 查指标 / 行情 / 基金净值\n3. 📚 金融与统计知识\n4. 📈 画图 / 清洗 / 建模 / DID 因果",
}


def clarification_questions(intent: str) -> str:
    """根据意图返回引导式追问文案"""
    return _CLARIFY_TEMPLATES.get(intent, _CLARIFY_TEMPLATES["chat"])


# ==================== 回复角色与深度（5.1 / 5.2） ====================

ROLE_PROMPTS: Dict[str, str] = {
    "nlq": "你是严谨的数据管家，回答要简洁精确，直接给出数字与来源，避免过度解读。",
    "analysis": "你是一位有10年经验的投研分析师，说话直接、有观点、不模棱两可；先给结论再列依据，关键数字用**加粗**。",
    "visualization": "你是数据可视化向导，先确认图表选型（X/Y/分组）再给出可执行建议，必要时直接建议调用的图表接口。",
    "knowledge": "你是知识面广、讲人话、有判断力的金融/财税顾问，把概念讲清楚并给出通俗类比。",
    "operation": "你是耐心的技术支持，回答要分步骤、可执行，逐步引导，别一次堆太多。",
    "troubleshoot": "你是细致的技术支持，先定位根因再给修复步骤，语气耐心、给出可复现的验证方法。",
    "agent": "你是统筹型的首席分析师，擅长拆解目标、联动多个分析模块，输出结构化结论与后续建议。",
    "chat": "你是亲和的数据助手，口语化、简短、适度用 emoji。",
    "computation": "你是严谨的计算器：只呈现后端真实计算结果，禁止自行推算或估算；结果附口径说明。",
    "out_of_scope": "你是数据与金融分析助手，礼貌说明能力边界，并把话题引导回数据/金融/统计领域。",
}

_DEPTH_CONCISE_KW = ("简要", "一句话", "快速", "大概", "简单说说", "别太长", "short", "concise")
# 注意："报告"一词单独出现不应判为深度（如"报告怎么导出"是功能咨询），
# 只有"研究报告/分析报告/生成报告"这类**要求产出报告**的说法才判 deep（P1 精修）。
_DEPTH_DEEP_KW = ("详细", "深入", "为什么", "怎么看", "分析一下", "全面", "深度",
                  "研究报告", "分析报告", "深度报告", "生成报告", "完整分析", "尽调",
                  "deep", "详细说明")


def determine_depth(text: str, history: Optional[List[dict]] = None) -> str:
    """
    判断用户要简要回答还是深度分析：
      - 命中简要用词 → concise
      - 命中深入用词 / 连续追问同一话题 → deep
      - 否则 balanced
    """
    low = text.lower()
    if any(k in low for k in _DEPTH_CONCISE_KW):
        return "concise"
    if any(k in low for k in _DEPTH_DEEP_KW):
        return "deep"
    # 连续追问同一话题 → 默认深入
    if history and len(history) >= 2:
        last_intent = str(history[-1].get("intent") or history[-1].get("intent") or "")
        prev_intent = str(history[-2].get("intent") or history[-2].get("intent") or "")
        if last_intent and last_intent == prev_intent:
            return "deep"
    return "balanced"


def role_system_prompt(intent: str, depth: str) -> str:
    """按意图与深度生成人设 + 长度约束片段（追加到现有 system prompt）"""
    role = ROLE_PROMPTS.get(intent, ROLE_PROMPTS["chat"])
    length = {
        "concise": "回答务必**简洁**，3 句话以内直击要点。",
        "deep": "回答可适度展开，结构清晰、逻辑严密，关键数字加粗。",
        "balanced": "回答简洁有力，先结论后依据。",
    }.get(depth, "")
    return f"{role}\n{length}"


# ==================== 便捷一站式路由 ====================

def route(text: str, history: Optional[List[dict]] = None,
          llm_config: Optional[dict] = None) -> Dict[str, Any]:
    """一站式入口：Layer1/2 分类 → （歧义时）Layer3 LLM 精排 → 后置校验 →
    澄清判定 + 深度 + 角色 + 实体 + 金融子意图，供 ai_assistant 接入。

    Layer3 仅在歧义（需澄清）且有 llm_config 时调用，平时零额外成本。"""
    classified = classify_intent(text, history)

    # ---- Layer3 LLM 精排：只在歧义时触发，Top-N 候选内做最终判断 ----
    # 候选排序用 _rank_scores（显式优先级），避免同分时受 dict 插入序影响。
    ranked = _rank_scores(classified.get('scores') or {})
    try:
        top2 = [c for c in ranked[:2] if c[1] > 0]
        if classified.get('needs_clarification') and top2 and llm_config:
            refined = refine_with_llm(text, [c[0] for c in top2], llm_config, history)
            if refined:
                classified['intent'] = refined['intent']
                classified['confidence'] = refined['confidence']
                classified['action'] = INTENT_TO_ACTION.get(refined['intent'], 'chat')
                classified['needs_clarification'] = refined['needs_clarification']
                classified['layer3_rationale'] = refined['rationale']
    except Exception as e:
        logger.debug("Layer3 精排跳过: %s", e)

    # ---- 后置校验：意图合法性 + 高风险意图置信度下限 ----
    classified = validate_intent(text, classified)

    depth = determine_depth(text, history)
    fin = _detect_finance_safe(text)
    # 难例回流：低置信/澄清请求落盘，成为后续评测回归用例（训练闭环）
    if classified.get('needs_clarification') or classified.get('confidence', 1.0) < 0.62:
        record_intent_case(text, classified['intent'], classified.get('confidence', 0.0),
                           meta={'layer3': classified.get('layer3_rationale')})
    return {
        "intent": classified["intent"],
        "action": classified["action"],
        "confidence": classified["confidence"],
        "entities": classified["entities"],
        "needs_clarification": classified["needs_clarification"],
        "missing_info": classified["missing_info"],
        "depth": depth,
        "role_prompt": role_system_prompt(classified["intent"], depth),
        "normalized": classified["normalized"],
        "clarification": clarification_questions(classified["intent"]) if classified["needs_clarification"] else "",
        # 混合意图：前 3 候选（供前端/上层在意图接近时展示，或做二次选择）
        "alternatives": classified.get("alternatives") or [],
        # Layer3 精排依据（仅歧义触发时有值；前端可展示"为什么这么判断"）
        "layer3_rationale": classified.get("layer3_rationale"),
        # 金融子意图增强（finance_intent 识别，供下游走金融分析/数据/护栏链路）
        "finance": fin,
        "financial_instruments": _extract_instruments_safe(text),
    }


def _detect_finance_safe(text: str) -> dict:
    """安全调用金融子意图识别；失败返回空（不阻断路由）"""
    try:
        from finance_intent import detect_finance
        return detect_finance(text)
    except Exception as e:
        logger.debug("金融子意图识别失败（忽略）: %s", e)
        return {"is_finance": False, "subintent": None, "confidence": 0.0, "hits": {}, "keywords": []}


def _extract_instruments_safe(text: str) -> list:
    """安全调用金融标的抽取；失败返回空"""
    try:
        from finance_intent import extract_financial_instruments, extract_financial_direction
        out = extract_financial_instruments(text)
        _dir = extract_financial_direction(text)
        return out + ([("$direction:" + _dir)] if _dir else [])
    except Exception as e:
        logger.debug("金融标的抽取失败（忽略）: %s", e)
        return []


# ==================== 三意图专用路由（E）：诊断 / 预测 / 操作 ====================

_PREDICTION_KW = ("明天会", "明天涨", "明天跌", "会涨吗", "会跌吗", "未来", "走势预测", "能涨", "能不能涨",
                  "预测一下", "预期", "后市", "接下来", "会不会涨", "会如何", "预判")
_DIAGNOSIS_KW = ("为什么", "为何", "啥原因", "是什么原因", "怎么会", "导致", "归因", "怎么回事", "因何", "为何会")
_OPERATION_COMPUTE_KW = ("帮我算", "计算一下", "算一下", "算算", "帮我算算", "请算", "计算出", "帮我计算")


def detect_special_intent(text: str) -> str:
    """返回专用路由标签：prediction / diagnosis / computation / generic"""
    low = text.lower()
    if any(k in low for k in _PREDICTION_KW):
        return "prediction"
    if any(k in low for k in _DIAGNOSIS_KW):
        return "diagnosis"
    if any(k in low for k in _OPERATION_COMPUTE_KW):
        return "computation"
    return "generic"


PREDICTION_GUARD = """# ⚠️ 预测风险护栏（必须遵守）
金融市场短期走势受极多不可控因素影响，**无法可靠预测**。请务必：
1. 先明确告知"短期方向不可预测，任何预测都只是情景推演而非事实"。
2. 再用**情景分析**替代点预测：给出乐观/中性/悲观三档情景及各自假设条件。
3. 不给予任何"确定性收益承诺"。
4. 附上合规表述"本分析仅供研究参考，不构成投资建议；基金过往业绩不预示未来表现"。"""


# ==================== ④ Query 改写 + 指代消解 =================

_PRONOUNS = ("它", "它的", "这个", "这个的", "该", "该指标", "其")


def _resolve_coreference(query: str, history: List[dict]) -> str:
    """指代消解：把"它的夏普比率呢"还原为「XX基金的夏普比率」。
    仅从历史最近提及的实体还原（不跨会话），启发式、零 LLM 依赖。"""
    if not history:
        return query
    low = query.lower()
    if not any(p in low for p in _PRONOUNS):
        return query
    last_entities: Dict[str, list] = {}
    for turn in reversed(history[-3:]):
        ents = turn.get("entities") or {}
        if ents.get("indicator") or ents.get("target"):
            last_entities = ents
            break
    if not last_entities:
        return query
    # 优先用上一轮最近的指标；无指标用标的
    anchor = None
    if last_entities.get("indicator"):
        anchor = last_entities["indicator"][-1]
    elif last_entities.get("target"):
        anchor = last_entities["target"][-1]
    if not anchor:
        return query
    for p in _PRONOUNS:
        if p != "它" or p in low:
            query = query.replace("它的", f"{anchor}的").replace("它", anchor).replace("这个", anchor).replace("该指标", f"{anchor}指标")
    return query.strip()


def rewrite_query(query: str, history: Optional[List[dict]] = None, current_file: str = None) -> str:
    """Query 改写：指代消解 + 口语规范化 + 文件补全（④）"""
    q = _resolve_coreference(query, history or [])
    if current_file and ("文件" in q or "这份数据" in q):
        q = q.replace("这个文件", current_file).replace("这份数据", current_file).replace("该文件", current_file)
    # 口语化简化（提升检索/识别命中）
    q = q.replace("最近跌成狗了", "最近回撤很大").replace("咋样", "怎么样").replace("跌成啥样了", "回撤多大")
    return q


if __name__ == "__main__":  # 自测：python intent_router.py
    for s in ["最近30天收益率最高的基金", "什么是久期？", "帮我分析一下这个数据", "画个柱状图",
              "系统报错了怎么办", "怎么上传文件", "你好", "帮我查一下上个月的销售额", "分析腾讯值得买吗"]:
        print(json.dumps({"q": s, **route(s)}, ensure_ascii=False))
    print("[rewrite] ", rewrite_query("它的夏普比率呢？", [{"entities": {"indicator": ["夏普比率"], "target": ["基金"]}}]))