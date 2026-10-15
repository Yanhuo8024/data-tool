#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
finance_intent.py - 金融子意图识别层（专门增强金融领域的意图细分）

在通用意图(analysis/nlq/knowledge...)之上，进一步判定"用户这次到底想干嘛"，
让下游能精准路由到对应的金融分析链路 / 金融数据接口 / 金融知识 / 风险护栏。

设计原则：
- 纯规则、无网络请求：不额外消耗云端 quota（与 intent_router 一致）
- 不接管控制流：只输出金融子意图标签 + 置信度 + 命中的金融实体，供 ai_assistant / intent_router 复用
- 失败静默降级：任何异常返回 is_finance=False

子意图覆盖（金融领域常见请求）：
  stock_valuation    估值/定价（值不值、贵不贵、PE/PB/DCF、高估低估）
  risk_assessment    风险评估（回撤/波动/风险收益比/VaR/下行风险/最大回撤）
  fund_performance   收益/业绩分析（收益率/年化/跑赢/净增/业绩比较）
  market_quote       行情/市场（指数/涨跌/点位/板块/实时价格）
  macro_policy       宏观/政策（利率/汇率/货币政策/CPI/降准降息/财政）
  financial_statement 财报/财务分析（营收/利润/资产负债/现金流/三大报表）
  quant_model        量化/建模（模型/因子/回测/Alpha/Beta/机器学习选股）
  stock_screening    选股/选基（筛选/排名/符合条件/Top/选股）
  fixed_income       固收/债（债券/久期/利差/收益率曲线/票息/到期收益率）
  derivatives        衍生品（期权/期货/互换/对冲/套利）
  credit_scoring     信用/风控建模（评分卡/WOE/IV/PSI/违约/逾期/催收）
  asset_allocation   资产配置/组合（配置/仓位/组合/再平衡/分散）
  policy_effect      DID/政策效应评估（双重差分/试点/政策效果/因果/准自然）
  tax_compliance     税务/合规（税/所得税/增值税/申报/合规/监管）
"""

import re
import logging
from typing import Dict, Any, List

logger = logging.getLogger(__name__)


# ==================== 金融子意图关键词库 ====================
# 每个子意图给出"强特征词"（高权重，独占性）与"弱特征词"（需结合上下文）
_FIN_SUBINTENTS: Dict[str, Dict[str, List[Any]]] = {
    "stock_valuation": {
        "strong": ["估值", "值不值", "值得买", "贵不贵", "高估", "低估", "市盈率", "市净率",
                    "pe", "pb", "peg", "dcf", "内在价值", "合理价位", "估值贵", "估值低",
                    "股价合理", "股价贵", "股价便宜", "股价高吗", "股价低吗", "贵吗", "便宜吗"],
        "weak": ["股息率", "每股收益", "eps", "市值", "便宜", "合理", "股价", "价位", "价格合理"],
    },
    "risk_assessment": {
        "strong": ["回撤", "最大回撤", "波动率", "夏普", "sharpe", "下行风险", "var", "风险价值",
                    "风险收益", "卡玛", "calmar", "索提诺", "sortino", "波动", "标准差"],
        "weak": ["风险", "稳健", "高风险", "低风险", "震荡"],
    },
    "fund_performance": {
        "strong": ["收益率", "年化", "收益", "跑赢", "业绩", "回报", "净值", "超额收益", "alpha",
                    "净增", "涨幅", "盈利多少", "赚了多少", "涨了多少"],
        "weak": ["表现", "成绩", "净值涨"],
    },
    "market_quote": {
        "strong": ["行情", "指数", "大盘", "点位数", "实时价", "最新价", "涨幅", "跌停", "涨停",
                    "板块", "北向", "沪深300", "上证", "深证", "恒生", "纳指", "标普", "道指"],
        "weak": ["今天怎么样", "现在多少", "收盘", "开盘", "多少点"],
    },
    "macro_policy": {
        "strong": ["货币政策", "利率", "加息", "降息", "降准", "汇率", "cpi", "ppi", "gdp", "m2",
                    "社融", "通胀", "央行", "财政", "国债收益率", "十年期", "lpr", "mlf", "逆回购"],
        "weak": ["宏观", "经济", "宽松", "紧缩"],
    },
    "financial_statement": {
        "strong": ["营收", "利润", "毛利率", "净利率", "资产负债", "现金流", "三大报表", "财报",
                    "roe", "roa", "营业成本", "净利润", "营收增长", "负债率", "财务比率"],
        "weak": ["盈利", "赚钱吗", "亏钱", "每股收益"],
    },
    "quant_model": {
        "strong": ["回测", "因子", "alpha", "beta", "机器学习选股", "模型", "特征", "训练预测",
                    "tensorflow", "xgboost", "多因子", "对冲策略", "alpha策略"],
        "weak": ["预测", "建模", "量化"],
    },
    "stock_screening": {
        "strong": ["选股", "选基", "筛选", "排名", "符合条件", "top", "前几", "排行榜", "相似标的",
                    "挑几只", "怎么挑选", "综合排名"],
        "weak": ["找出", "哪些", "清单"],
    },
    "fixed_income": {
        "strong": ["债券", "久期", "利差", "收益率曲线", "票息", "到期收益率", "信用债", "利率债",
                    "convexity", "凸性", "国债", "企业债", "持仓债"],
        "weak": ["票面", "到期", "收益率水平"],
    },
    "derivatives": {
        "strong": ["期权", "期货", "互换", "对冲", "套利", "股指期货", "看涨期权", "看跌期权",
                    "delta", "gamma", "保证金", "平仓", "交割"],
        "weak": ["杠杆"],
    },
    "credit_scoring": {
        "strong": ["评分卡", "woe", "iv", "psi", "违约", "逾期", "催收", "信用评分", "反欺诈",
                    "审批", "批核", "评分模型", "风控模型", "好客户", "坏客户", "bad rate"],
        "weak": ["信用", "信贷", "贷款审批", "风险评分"],
    },
    "asset_allocation": {
        "strong": ["资产配置", "组合", "仓位", "再平衡", "分散", "配比", "股债配", "定投组合", "调仓"],
        "weak": ["怎么买", "配置比例", "组合怎么看"],
    },
    "policy_effect": {
        "strong": ["did", "双重差分", "政策评估", "试点效果", "政策效应", "准自然", "因果推断",
                    "平行趋势", "安慰剂", "处理组", "对照组", "政策前后"],
        "weak": ["影响", "评价政策", "效果如何"],
    },
    "tax_compliance": {
        "strong": ["税", "个税", "企业所得税", "增值税", "申报", "减免", "抵扣", "退税", "印花税",
                    "房产税", "契税", "发票", "税务"],
        "weak": ["合规", "申报流程"],
    },
}

# 金融领域"全局强标志词"：命中即认为属于金融领域（用于 is_finance 判定）
_FIN_ANCHOR = ("基金", "股票", "债券", "汇率", "利率", "收益", "回撤", "净值", "持仓", "份额",
               "股市", "股价", "市盈", "市净", "标的", "板块", "指数", "夏普", "久期", "利差",
               "估值", "期权", "期货", "国债", "ETF", "QDII", "LOF", "A股", "港美", "美股",
               "行业")

# 行业板块后缀：识别"XX行业 / XX板块 / XX赛道 / XX产业" → 行业研究分析（如 电器行业、白酒板块、半导体赛道）
_INDUSTRY_SUFFIX = ("行业", "板块", "赛道", "产业")
# 行业关注词：表明是要分析该行业/板块，而非泛泛提及
_INDUSTRY_CONCERN = ("分析", "怎么样", "如何", "走势", "前景", "行情", "表现", "龙头", "概念",
                     "看好", "逻辑", "机会", "投资", "关注", "看看", "怎么样", "未来", "涨",
                     "趋势", "概况", "解", "介绍")

# 通用词（非独占，避免"分析一下"这种被误判为金融）
_FIN_COMMON = ("分析", "看看", "评估", "怎么样", "如何", "多少", "查一下", "预测", "表现")


def _hit_score(text: str) -> Dict[str, float]:
    """统计每个金融子意图的命中分数（strong=2.0，weak=0.8）"""
    low = text.lower()
    scores: Dict[str, float] = {}
    for sub, kws in _FIN_SUBINTENTS.items():
        s = 0.0
        for kw in kws["strong"]:
            if kw.lower() in low:
                s += 2.0
        for kw in kws["weak"]:
            if kw.lower() in low:
                s += 0.8
        if s:
            scores[sub] = s
    return scores


def detect_finance(text: str) -> Dict[str, Any]:
    """
    识别金融子意图。
    返回:
      is_finance: bool          是否属于金融领域请求
      subintent: str|None       金融子意图标签（最高分，命中不足时 None）
      confidence: float         子意图置信度 (0~1)
      hits: Dict[str,float]     所有命中的子意图及分数
      keywords: List[str]       命中的金融标志词
    """
    if not text:
        return {"is_finance": False, "subintent": None, "confidence": 0.0,
                "hits": {}, "keywords": []}
    low = text.lower()
    scores = _hit_score(text)

    # 关键词语集合（用于净化展示/下游提示）
    anchors = [k for k in _FIN_ANCHOR if k.lower() in low]

    # == 行业板块识别：形如"XX行业/板块/赛道 [+ 关注词]" → 行业行情分析 ==
    # 例：电器行业、白酒板块、半导体赛道、医药板块怎么样
    industry_hit = None
    for suf in _INDUSTRY_SUFFIX:
        idx = low.find(suf)
        if idx > 0:
            prefix = text[:idx].strip().replace("帮我", "").replace("请", "").replace("分析", "")
            # 前缀应为具体行业名（长度适中、非通用建议词）
            if prefix and len(prefix) >= 2 and len(prefix) <= 6:
                concerned = any(c in low for c in _INDUSTRY_CONCERN) or idx <= len(text) - len(suf)
                if concerned:
                    industry_hit = prefix
                    break
    if industry_hit:
        # 行业/板块研究 → sector_analysis（行业研究员角色 + 无文件也可用联网/技能分析），
        # 不再挂在 market_quote 下（那是指数/大盘行情口径）
        scores["sector_analysis"] = scores.get("sector_analysis", 0) + 4.0
        anchors.append("行业")
        anchors.append(industry_hit)

    # 判定是否金融：命中强标志词 或 有子意图得分
    has_anchor = bool(anchors)
    if not scores and not has_anchor:
        return {"is_finance": False, "subintent": None, "confidence": 0.0,
                "hits": {}, "keywords": []}

    # 子意图打分：最高者胜出
    ranking = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    subintent, top = (ranking[0] if ranking else (None, 0.0))
    second = ranking[1][1] if len(ranking) > 1 else 0.0

    # 置信度：有明确强词 + 明显领先 → 高置信；否则需结合金融锚词给中置信
    if top >= 4.0 and (top - second) >= 1.0:
        conf = min(0.97, 0.7 + top * 0.05)
    elif top >= 2.0:
        conf = 0.75
    elif has_anchor:
        conf = 0.6  # 有金融锚词但子意图不明确
    else:
        conf = 0.5

    return {
        "is_finance": True,
        "subintent": subintent,
        "confidence": round(conf, 3),
        "hits": scores,
        "keywords": anchors[:10],
    }


# ==================== 金融实体抽取增强 ====================

# 常见 A 股/港股/美股标的名（用于"腾讯值不值得买"这类无代码标的名识别）
_STOCK_NAMES = ("腾讯", "茅台", "宁德时代", "比亚迪", "平安", "招行", "工商银行", "平安银行",
                "中石油", "中石化", "五粮液", "美的", "格力", "隆基", "药明", "恒瑞", "海康",
                "京东方", "万科", "华为", "阿里", "百度", "美团", "京东", "特斯拉", "苹果",
                "微软", "谷歌", "英伟达", "亚马逊", "中概股",
                # ===== 与 ai_assistant._STOCK_ALIASES 保持一致的常见标的 =====
                "兆易创新", "兆易", "汇顶科技", "韦尔股份", "澜起科技", "澜起", "北京君正",
                "北方华创", "中微公司", "华虹公司", "华虹半导体", "长电科技", "通富微电",
                "三安光电", "紫光国微", "圣邦股份", "士兰微", "卓胜微", "闻泰科技",
                "海光信息", "龙芯中科", "中科曙光", "寒武纪", "科大讯飞", "金山办公",
                "中兴通讯", "中国中免", "通威股份", "阳光电源", "海尔智家", "伊利股份",
                "中国移动", "建设银行", "农业银行", "中国银行", "国电南瑞", "大华股份",
                # ===== 港股/中概科技（此前缺失，导致「小米汽车是否盈利」实体识别为空→被判闲聊）=====
                "小米", "小米集团", "网易", "快手", "拼多多", "携程", "哔哩哔哩", "滴滴",
                "理想", "蔚来", "小鹏", "零跑", "舜宇光学", "中芯国际", "联想", "比亚迪电子")

# 公司名 + 业务线/主体后缀（如「小米汽车」「小米集团」）：名单外的标的也能被识别
_SEG_NAME_RE = re.compile(r'([\u4e00-\u9fa5]{2,4})(?:集团|汽车|科技|股份|公司|控股|电子|电器|银行|证券|医药|能源|地产|生物|半导体)')
# 明显不是公司名的指代前缀，避免「这家汽车公司」被抽成实体「这家」
_NON_NAME_PREFIX = ('这家', '那家', '一家', '两家', '这个', '那个', '什么', '某些', '某个',
                    '其他', '上述', '以上', '下面', '上面', '所有', '各种')


def extract_financial_instruments(text: str) -> List[str]:
    """
    抽取金融标的/代码：
      - 6 位 A股/基金代码（600/601/000/002/300/159/510/163 等）
      - 港股 5 位（90000 域名不用，报价代码如 00700）
      - 美股字母表（大小写 ticker 如 AAPL）
      - 常见中英文标的名
    返回去重列表；失败返回空。
    """
    out: List[str] = []
    low = text.lower()
    try:
        # A股/场内基金 6 位
        for m in re.finditer(r'(?<![\d.])(\d{6})(?![\d.])', text):
            code = m.group(1)
            # 基金/指数/股常见前缀，降低误catch（如随机6位数）
            if code.startswith(('5', '6', '0', '1', '3')):
                out.append(code)
        # 港股 5 位（以 0 开头，如 00700）
        for m in re.finditer(r'(?<!\d)[01]\d{4}(?!\d)', text):
            out.append(m.group(0))
        # 标的名
        out += [n for n in _STOCK_NAMES if n in text or n.lower() in low]
        # 名单外的「公司名+主体后缀」（如「小米汽车」「某某集团」）
        for m in _SEG_NAME_RE.finditer(text):
            name = m.group(1)
            if not any(name.startswith(p) for p in _NON_NAME_PREFIX):
                out.append(name)
    except Exception:
        pass
    return list(dict.fromkeys(out))


def extract_financial_direction(text: str) -> str:
    """
    识别用户关心的"方向/口径"：上涨/下跌/对比/排行/均值/总量
    供下游 NLQ/分析选择聚合方式。未识别返回空串。
    """
    low = text.lower()
    if any(k in low for k in ("谁","哪个","比","对比","跑赢","高于","低于","vs")):
        return "comparison"
    if any(k in low for k in ("最高","最多","最大","排行","排名","top")):
        return "top"
    if any(k in low for k in ("涨","升","上涨","走高")):
        return "up"
    if any(k in low for k in ("跌","降","下行","走低")):
        return "down"
    if any(k in low for k in ("平均","均值","中位")):
        return "average"
    if any(k in low for k in ("合计","总量","总共","总计")):
        return "sum"
    return ""


if __name__ == "__main__":
    _samples = [
        "腾讯值不值得买", "帮我算一下这只基金的夏普比率", "最近回撤为什么这么大",
        "上证指数现在多少点", "降息对债市有什么影响", "看下茅台的ROE和毛利率",
        "帮我筛选低估值高股息股票", "训练一个评分卡模型预测逾期", "做一份DID政策评估",
        "我和朋友聊天", "今天天气怎么样",
    ]
    for s in _samples:
        r = detect_finance(s)
        print(repr(s), "->", r["is_finance"], r["subintent"], r["confidence"])