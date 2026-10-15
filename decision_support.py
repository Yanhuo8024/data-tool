# -*- coding: utf-8 -*-
"""决策支持：**技术面与基本面联动** + **行业跟踪变量清单**。

两件事
------
1. **技术面不能孤立展示**。原来报告里技术面是独立一节（近 5 日 −7.32%、MA20 下方…），
   读者拿到一堆数字却不知道"所以呢"。这里给一句**联动判断**：
   技术面与基本面同向 / 背离，分别意味着什么，以及要注意什么风险。
2. **行业变量清单**。看一家公司不能只看它自己的报表。以汽车玻璃为例，
   真正驱动业绩的是**单车玻璃价值量、新能源渗透率、纯碱与天然气价格、海外关税**……
   把"该盯什么"显式列出来，读者才知道去哪里找原因。

零项目依赖（仅标准库，另复用 `peer_pool` 的行业键）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

try:                                   # 复用行业键，保持与同业池一致
    from peer_pool import classify_industry, industry_name
except Exception:                      # pragma: no cover - 极端情况下退化为不识别
    def classify_industry(*_a, **_k):
        return None

    def industry_name(_k):
        return '未识别'


# ==================== ① 技术面 × 基本面 ====================

TECH_CN = {'strong': '偏强', 'weak': '偏弱', 'neutral': '中性'}
FUND_CN = {'cheap': '估值偏低', 'fair': '估值合理', 'expensive': '估值偏高'}

# 组合矩阵：(技术面, 基本面) → (立场, 判断, 风险)
_COMBINATIONS: Dict[tuple, Dict[str, str]] = {
    ('strong', 'cheap'): {
        'stance': '同向偏多（但需确认不是"便宜有便宜的道理"）',
        'text': '技术与估值**同向**：价格走出上升结构，同时估值处于偏低区间。',
        'risk': '低价可能是市场对某个尚未反映在财报里的风险定价（如订单流失、政策变化），'
                '需先排除"价值陷阱"再谈机会；且短期涨幅已大时追高风险上升。',
    },
    ('strong', 'fair'): {
        'stance': '同向（趋势为主，估值不构成支撑）',
        'text': '技术与估值**大体同向**：价格走势偏强，估值在合理区间。',
        'risk': '合理估值不提供安全垫，一旦趋势走坏缺少基本面缓冲，需设好止损参照。',
    },
    ('strong', 'expensive'): {
        'stance': '**背离**：情绪驱动为主，警惕回调',
        'text': '技术与估值**背离**：价格偏强但估值已偏高，上涨更多由情绪/资金推动。',
        'risk': '背离状态下回撤往往又快又深；若无新的基本面证据（如业绩超预期），'
                '不宜把趋势当作估值合理性依据。',
    },
    ('weak', 'cheap'): {
        'stance': '**背离**：可能错杀，但需等右侧信号',
        'text': '技术与估值**背离**：估值偏低但价格走势偏弱。',
        'risk': '"低估 ≠ 会涨"。价格弱势说明市场尚未认可该估值，'
                '左侧介入需承受继续下跌的时间成本；宜等趋势企稳信号。',
    },
    ('weak', 'fair'): {
        'stance': '同向偏弱（等待明确信号）',
        'text': '技术与估值**同向偏弱**：价格走势偏弱，估值缺乏明显吸引力。',
        'risk': '缺少催化因素时，弱势可能延续；需等待基本面或资金面出现变化。',
    },
    ('weak', 'expensive'): {
        'stance': '**双弱**：风险最高的组合',
        'text': '技术与估值**双弱**：价格走弱且估值偏高。',
        'risk': '估值回归与趋势下行可能叠加，属风险最高的组合，'
                '应优先关注下行风险而非寻找反弹理由。',
    },
    ('neutral', 'cheap'): {
        'stance': '震荡（估值有支撑，缺催化）',
        'text': '技术面中性、估值偏低：价格尚未选择方向，但估值提供一定安全垫。',
        'risk': '缺乏催化时可能长期横盘，资金效率低；需等待基本面或资金面出现变化。',
    },
    ('neutral', 'fair'): {
        'stance': '中性（观望）',
        'text': '技术与估值均处中性区间：当前既无明显趋势，也无明显估值偏离。',
        'risk': '此类状态下的结论最容易受噪声影响，建议明确后续跟踪指标再判断。',
    },
    ('neutral', 'expensive'): {
        'stance': '估值偏高的震荡（谨慎）',
        'text': '技术面中性但估值偏高：价格未继续走强，估值已透支部分预期。',
        'risk': '估值高而趋势停滞，往往意味着性价比在下降；需警惕基本面不及预期时的快速重估。',
    },
}


def classify_tech(technical: Optional[Dict[str, Any]]) -> str:
    """从技术面数据（收盘价 / MA / 近期涨跌）判断强弱。数据不足返回 neutral。"""
    t = technical or {}
    price = _f(t.get('price') or t.get('close'))
    ma = _f(t.get('ma20') or t.get('ma'))
    m1 = _f(t.get('month_change') or t.get('chg_20d'))
    m5 = _f(t.get('week_change') or t.get('chg_5d'))
    votes = 0
    total = 0
    if price is not None and ma is not None:
        total += 1
        votes += 1 if price > ma else -1
    for v in (m1, m5):
        if v is not None:
            total += 1
            votes += 1 if v > 0 else (-1 if v < 0 else 0)
    if total == 0:
        return 'neutral'
    avg = votes / total
    if avg > 0.3:
        return 'strong'
    if avg < -0.3:
        return 'weak'
    return 'neutral'


def classify_fundamental(*, verdict: Optional[str] = None,
                         pe: Optional[float] = None,
                         pe_peer: Optional[float] = None,
                         growth: Optional[float] = None) -> str:
    """从估值结论 / PE 相对位置 / 增速判断基本面档位。"""
    v = str(verdict or '').lower()
    if v in ('undervalued', 'cheap', '低估'):
        return 'cheap'
    if v in ('overvalued', 'expensive', '高估'):
        return 'expensive'
    if pe is not None and pe_peer:
        try:
            ratio = float(pe) / float(pe_peer)
            if ratio < 0.85:
                return 'cheap'
            if ratio > 1.15:
                return 'expensive'
        except (TypeError, ValueError, ZeroDivisionError):
            logger.debug('PE 相对位置比较失败: %s / %s', pe, pe_peer)
    if growth is not None:
        try:
            g = float(growth)
            if g >= 25:
                return 'cheap'
            if g <= 0:
                return 'expensive'
        except (TypeError, ValueError):
            logger.debug('增速解析失败: %s', growth)
    return 'fair'


def link(technical: Optional[Dict[str, Any]], fundamental: Optional[Dict[str, Any]],
         *, horizon: str = 'mid') -> Dict[str, Any]:
    """生成技术面 × 基本面联动判断。"""
    tech_state = classify_tech(technical)
    fund_state = classify_fundamental(**(fundamental or {}))
    combo = _COMBINATIONS.get((tech_state, fund_state)) or {}
    horizon_note = {
        'short': '短期（<1 年）看趋势与资金，技术面权重更高。',
        'long': '长期（>3 年）看现金流与估值，基本面权重更高，短期技术弱势可能是布局窗口。',
    }.get(horizon, '中期（1–3 年）需技术面与基本面相互印证。')
    # 背离只在两种组合成立：价强估贵（情绪驱动）、价弱估低（可能错杀但趋势未认可）
    direction = 'divergent' if (tech_state, fund_state) in (
        ('strong', 'expensive'), ('weak', 'cheap')) else 'aligned'
    text = (
        f"**综合判断（技术面 × 基本面）**\n"
        f"- 技术面：{TECH_CN[tech_state]}（依据：价格与 MA20 关系、近 5 日/近月涨跌）\n"
        f"- 基本面：{FUND_CN[fund_state]}\n"
        f"- 结论：{combo.get('text', '数据不足，暂不给出联动判断。')}"
        f"{(' **注意：两者背离。**' if direction == 'divergent' else '')}\n"
        f"- 口径提示：{horizon_note}"
    )
    if combo.get('risk'):
        text += f"\n- 需关注风险：{combo['risk']}"
    return {'tech_state': tech_state, 'tech_cn': TECH_CN[tech_state],
            'fund_state': fund_state, 'fund_cn': FUND_CN[fund_state],
            'stance': combo.get('stance', '数据不足'), 'direction': direction,
            'text': text, 'risks': combo.get('risk', '')}


# ==================== ② 行业跟踪变量 ====================

# 键与 peer_pool.INDUSTRY_POOLS 保持一致
INDUSTRY_VARIABLES: Dict[str, Dict[str, Any]] = {
    'auto_glass': {
        'name': '汽车玻璃 / 汽车零部件',
        'variables': [
            {'name': '单车玻璃价值量', 'unit': '元/辆',
             'why': '决定"量价"里的价——面积增大与天幕/抬头显示等高附加值玻璃渗透直接抬升单价',
             'source': '公司年报「主要产品产能与销量」、券商行业深度'},
            {'name': '新能源渗透率', 'unit': '%',
             'why': '新能源车天幕玻璃渗透率显著高于燃油车，是单车价值量的核心驱动',
             'source': '中汽协/乘联会月度数据'},
            {'name': '纯碱价格', 'unit': '元/吨',
             'why': '浮法玻璃主原料，直接冲击毛利率；价格上行周期会明显压缩毛利',
             'source': '隆众/卓创资讯、期货主力合约'},
            {'name': '天然气 / 重油价格', 'unit': '元/立方米、元/吨',
             'why': '熔窑燃料成本占比高，能源价格上涨直接吃掉毛利',
             'source': '发改委门站价、地方燃气公司公告'},
            {'name': '海外关税与贸易政策', 'unit': '—',
             'why': '出口与海外基地盈利受关税、反倾销调查直接影响',
             'source': '商务部公告、海外基地所在国贸易救济部门'},
            {'name': '汽车产量 / 销量', 'unit': '万辆',
             'why': '下游景气度决定配套出货量',
             'source': '中汽协产销快讯、乘联会周度零售'},
            {'name': '汇率（出口占比）', 'unit': '—',
             'why': '海外收入占比高时，本币升值形成汇兑损失与折算压力',
             'source': '公司外汇风险披露 + 央行中间价'},
        ],
    },
    'nev': {
        'name': '新能源整车',
        'variables': [
            {'name': '月度交付/销量', 'unit': '辆', 'why': '规模效应直接决定单车毛利能否转正',
             'source': '各车企月度交付公告'},
            {'name': '单车平均售价（ASP）', 'unit': '万元/辆', 'why': '价格战下 ASP 下滑会同时打击毛利与规模效应',
             'source': '财报分部披露'},
            {'name': '电池级碳酸锂价格', 'unit': '万元/吨', 'why': '电池为最大单一成本项',
             'source': '上海钢联/百川盈孚'},
            {'name': '补贴与购置税政策', 'unit': '—', 'why': '政策退坡直接影响终端需求节奏',
             'source': '财政部/工信部公告'},
        ],
    },
    'baijiu': {
        'name': '白酒',
        'variables': [
            {'name': '批价（一批价）', 'unit': '元/瓶', 'why': '批价是渠道健康度的先行指标',
             'source': '今日酒价/渠道调研'},
            {'name': '渠道库存', 'unit': '月', 'why': '库存高企会导致后续压货困难、价格松动',
             'source': '渠道调研、券商草根调研'},
            {'name': '吨价与产品结构', 'unit': '万元/吨', 'why': '高端占比提升是毛利核心驱动',
             'source': '年报分产品收入'},
        ],
    },
    'bank': {
        'name': '银行',
        'variables': [
            {'name': '净息差（NIM）', 'unit': '%', 'why': '息差是银行营收的核心变量',
             'source': '季报/年报'},
            {'name': '不良率与关注类占比', 'unit': '%', 'why': '资产质量决定信用成本',
             'source': '季报/年报、监管披露'},
            {'name': 'LPR 与存款挂牌利率', 'unit': '%', 'why': '直接影响资产端与负债端定价',
             'source': '央行、全国银行间同业拆借中心'},
        ],
    },
    'internet': {
        'name': '互联网平台',
        'variables': [
            {'name': 'DAU / MAU 与用户时长', 'unit': '亿人、分钟', 'why': '流量基本盘决定变现天花板',
             'source': '财报运营数据'},
            {'name': 'ARPU / 广告加载率', 'unit': '元、%', 'why': '变现效率的核心变量',
             'source': '财报、第三方监测'},
            {'name': '监管政策', 'unit': '—', 'why': '平台经济监管直接影响业务边界',
             'source': '监管公告'},
        ],
    },
}


def variables_for(*hints: Optional[str]) -> Dict[str, Any]:
    """按行业给出应跟踪的变量清单。识别不出时返回通用清单 + 说明（**不硬套行业**）。"""
    key = classify_industry(*hints)
    if key and key in INDUSTRY_VARIABLES:
        spec = INDUSTRY_VARIABLES[key]
        return {'industry': key, 'industry_cn': spec['name'],
                'variables': [dict(v) for v in spec['variables']],
                'generic': False,
                'note': f"以下为「{spec['name']}」行业的关键跟踪变量"}
    return {
        'industry': None, 'industry_cn': industry_name(key), 'generic': True,
        'variables': [
            {'name': '营收与净利同比', 'unit': '%', 'why': '经营的总体结果',
             'source': '定期报告'},
            {'name': '毛利率与费用率', 'unit': '%', 'why': '判断利润变动来自价格/成本还是费用',
             'source': '利润表及附注'},
            {'name': '经营性现金流/净利润', 'unit': '倍', 'why': '利润的质量（是否收到钱）',
             'source': '现金流量表'},
            {'name': '行业景气指标（产量/价格/开工率）', 'unit': '—',
             'why': '公司业绩的上游驱动', 'source': '行业协会/统计局'},
        ],
        'note': '⚠️ 未识别到具体行业，给出**通用变量清单**；'
                '请补充行业字段，系统才能列出该行业特有的驱动变量',
    }


def render_variables(result: Dict[str, Any]) -> str:
    """渲染行业变量清单（Markdown 表）。"""
    rows = result.get('variables') or []
    lines = [f"**行业跟踪变量**（{result.get('industry_cn')}）", '',
             f"> {result.get('note', '')}", '',
             '| 变量 | 单位 | 为什么关键 | 数据来源 |', '|---|---|---|---|']
    for v in rows:
        lines.append(f"| {v['name']} | {v.get('unit', '—')} | {v['why']} | {v.get('source', '—')} |")
    return '\n'.join(lines)


def _f(v: Any) -> Optional[float]:
    try:
        return None if v in (None, '') else float(v)
    except (TypeError, ValueError):
        return None
