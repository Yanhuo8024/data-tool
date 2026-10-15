# -*- coding: utf-8 -*-
"""risk_checks_finance.py — 金融内容专项风控检查（确定性规则）

从 `risk_review.py` 拆出，原因有二：
  ① `risk_review.py` 受 400 行棘轮约束，新增 4 类金融检查无处安放；
  ② 这些规则**只对投研/估值类文本有意义**，与通用合规规则（收益承诺、荐股）职责不同。

## 修复的真实故障（2026-10-08）

**免责声明误报**：报告里明明写了「本报告…**不构成任何形式的投资建议**…**股市有风险**，
投资需谨慎」，风控却报「缺少免责声明」。

根因：原实现是**精确子串匹配**固定短语表（`'不构成投资建议'` / `'投资有风险'`），
而实际文本在中间插了字（"任何**形式的**"）或换了搭配（"**股市**有风险"）→ 匹配失败。
本模块改用**弹性正则**，容忍插入语与近义搭配，从根本上消除这类误报。

## 新增检查（用户点名的关键项）

| 检查 | 命中示例 | 级别 |
|---|---|---|
| `currency_unit` | 同时出现「港元」与「人民币」却未给汇率口径 | mid |
| `data_staleness` | 报告期距今超过 2 年 | mid |
| `growth_extrapolation` | 单年同比 >50% 被当作长期/永续增速 | mid |
| `extreme_valuation` | 出现 >100% 的估值偏差/空间且直接下低估/高估结论 | high |
| `missing_risk_section` | 涉金融却无「风险提示」段落 | mid |
"""
import datetime
import logging
import re
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# ==================== 免责声明：弹性匹配 ====================
# 关键设计：不写死完整短语，而是允许**插入语**与**近义搭配**。
# 例：'不构成任何形式的投资建议' / '不构成投资建议' / '不构成买卖要约' 都应命中。
_DISCLAIMER_PATTERNS = (
    r'不构成.{0,14}?投资(建议|依据|参考|决策|意见)',
    r'不构成.{0,14}?(买卖|交易|要约|承诺)',
    r'不作为.{0,14}?(投资|买卖|交易)',
    r'(股市|市场|投资|证券|基金|理财|入市|交易)有风险',
    r'投资(需|须|要)谨慎',
    r'(风险|盈亏|损失|后果)(自担|自负)',
    r'仅供参考',
    r'不(保证|承诺)(收益|本金|盈利|回报)',
    r'仅(供|作).{0,8}参考',
    r'据此操作.{0,10}风险',
)
_DISCLAIMER_RE = re.compile('|'.join(_DISCLAIMER_PATTERNS))

_FINANCE_TOPIC = (
    '估值', '市盈率', 'PE', 'PB', 'ROE', '收益率', '净值', '基金', '债券', '股票',
    '组合', '配置', '理财', '行情', '财报', '净利润', '现金流', '回撤', '夏普',
    '内在价值', '折现', 'DCF', '目标价', '买入', '增持',
)


def find_disclaimer(text: str):
    """在**全文**范围内找免责声明，返回命中的原文字段（未命中返回 None）。

    注意是全文扫描而非只看末段 —— 免责声明可能出现在页眉、附录或表注中。
    """
    if not text:
        return None
    m = _DISCLAIMER_RE.search(str(text))
    return m.group(0) if m else None


def check_disclaimer_flexible(text: str, has_finance_topic: bool = None) -> Optional[Dict[str, Any]]:
    """规则：涉金融结论但缺免责声明（弹性匹配版，替代原精确子串匹配）。"""
    if not text or len(str(text)) < 60:
        return None
    body = str(text)
    if has_finance_topic is None:
        has_finance_topic = any(k in body for k in _FINANCE_TOPIC)
    if not has_finance_topic:
        return None
    if find_disclaimer(body):
        return None
    return {
        'rule': 'missing_disclaimer', 'label': '缺少免责声明', 'level': 'mid', 'hits': [],
        'advice': '涉金融内容的结论须附风险提示（如「不构成投资建议，市场有风险」）。',
    }


# ==================== 币种 / 单位一致性 ====================
_HKD_MARK = re.compile(r'(港元|港币|HK\$|HKD)', re.I)
_CNY_MARK = re.compile(r'(人民币|RMB|CNY)')
_FX_NOTE = re.compile(r'(汇率|折算|换汇|兑人民币|按\s*[\d.]+\s*的?\s*汇率)')
_CUR_AMOUNT = re.compile(r'(?:每股|现价|股价|市值|净利润|营收|收入|内在价值|目标价)')
_CUR_VALUE = re.compile(r'(?:每股|现价|股价|市值|内在价值|目标价)[^。；\n]{0,12}?([\d,]+(?:\.\d+)?)\s*(亿|万)?\s*(元|港元|港币|美元)')


def check_currency_consistency(text: str) -> Optional[Dict[str, Any]]:
    """同一段分析里混用港元与人民币，且未给汇率口径。"""
    body = str(text or '')
    hk, cny = _HKD_MARK.search(body), _CNY_MARK.search(body)
    if not (hk and cny):
        return None
    if _FX_NOTE.search(body):
        return None
    return {
        'rule': 'currency_unit', 'label': '币种口径不一致', 'level': 'mid',
        'hits': [f'同时出现「{hk.group(0)}」与「{cny.group(0)}」，未注明汇率口径'],
        'advice': '港股常见「股价按港元、财报按人民币」。请先统一币种并标注汇率与日期；'
                  '金额还需标明单位（元/亿元/万元），每股与总量口径不可混用。',
    }


# ==================== 数据时效 ====================
_YEAR_IN_PERIOD = re.compile(r'(?:报告期|财报|数据基准|截至|截止)[^。；\n]{0,16}?(20\d{2})')
_ANY_YEAR = re.compile(r'(20\d{2})\s*年')


def check_data_staleness(text: str, now_year: int = None, max_age: int = 2) -> Optional[Dict[str, Any]]:
    """报告期距今超过 max_age 年 → 提示数据可能过期。"""
    body = str(text or '')
    years = [int(y) for y in _YEAR_IN_PERIOD.findall(body)] or \
            [int(y) for y in _ANY_YEAR.findall(body)]
    if not years:
        return None
    newest = max(years)
    cur = now_year or datetime.date.today().year
    if cur - newest <= max_age:
        return None
    return {
        'rule': 'data_staleness', 'label': '数据可能过期', 'level': 'mid',
        'hits': [f'最新报告期为 {newest} 年，距今 {cur - newest} 年（阈值 {max_age} 年）'],
        'advice': f'请改用最近一期财报数据，并注明报告期与抓取时间（当前为 {cur} 年）。',
    }


# ==================== 单年高增速外推 ====================
# 关键：必须区分「事实陈述」与「前瞻假设」。
#   「2025年净利润同比+76.02%」   → 事实，不该报；
#   「预计未来5年增长率76.02%」   → 外推，必须报。
# 早期版本把 `\d+年` 当长期信号，导致含"2025年"的事实句全部误报（实测）。
_GROWTH_KW = re.compile(r'(增长率|增速|复合增长|CAGR|g\s*=)')
_HORIZON_KW = re.compile(r'(未来|预计|预测|假设|假定|长期|永续|持续|外推|远期|目标|今后|接下来)')
_GROWTH_NUM = re.compile(r'(\d+(?:\.\d+)?)\s*%')
_SEG_SPLIT = re.compile(r'[。；;\n]+')


def check_growth_extrapolation(text: str, threshold: float = 50.0) -> Optional[Dict[str, Any]]:
    """把单年高增速当作长期/永续假设 —— 终值会指数膨胀（本案 106.77% 的直接后果）。

    只对**前瞻性**语句生效：同时要求「增速关键词 + 前瞻词 + 超阈数字」。
    单纯陈述历史同比不报（那是事实，不是假设）。
    """
    body = str(text or '')
    for seg in _SEG_SPLIT.split(body):
        seg = seg.strip()
        if not seg or not _GROWTH_KW.search(seg) or not _HORIZON_KW.search(seg):
            continue
        nums = [float(n) for n in _GROWTH_NUM.findall(seg)]
        hit = [n for n in nums if n > threshold]
        if hit:
            return {
                'rule': 'growth_extrapolation', 'label': '单年高增速外推', 'level': 'mid',
                'hits': [f'「{seg[:60]}」把 {hit[0]:.2f}% 的增速用于长期假设'],
                'advice': f'单年同比常含低基数修复/一次性因素，不宜直接外推。'
                          f'建议按 {threshold:.0f}% 上限截断，或改用 3 年 CAGR，'
                          f'并给出乐观/中性/悲观三档区间。',
            }
    return None


# ==================== 估值结论极端 ====================
_DEVIATION = re.compile(r'(偏差|空间|溢价|折价)[^。；\n]{0,12}?([+-]?\d+(?:\.\d+)?)\s*%')
_DIRECTION = re.compile(r'(低估|高估|值得买|建议买入|建议卖出|严重低估|显著高估)')
_CAVEAT = re.compile(r'(人工复核|需复核|假设极端|仅供参考|不构成|模型异常|估值冻结)')


def check_extreme_valuation(text: str, threshold: float = 100.0) -> Optional[Dict[str, Any]]:
    """出现极端估值偏差且直接给方向性结论、无免责/复核提示。"""
    body = str(text or '')
    devs = [(m.group(1), float(m.group(2))) for m in _DEVIATION.finditer(body)]
    extreme = [(k, v) for k, v in devs if abs(v) > threshold]
    if not extreme:
        return None
    if not _DIRECTION.search(body) or _CAVEAT.search(body):
        return None
    k, v = extreme[0]
    return {
        'rule': 'extreme_valuation', 'label': '估值结论极端且未加限定', 'level': 'high',
        'hits': [f'{k} {v:+.1f}%（超 ±{threshold:.0f}%）却直接给出低估/高估判断'],
        'advice': '偏差超过阈值时应先排查币种、单位、增长假设与基数口径；'
                  '结论只能输出「假设极端，需人工复核」，不得给出低估/高估。',
    }


# ==================== 风险提示段落（多信号识别） ====================
# 🚨 第二次误报后重写（2026-10-09 用户要求「不能只匹配关键词」）：
# 旧实现 `_RISK_SECTION = (风险提示|风险因素|主要风险|风险揭示|风险说明|投资风险)`
# 是**枚举完整标题**，「风险与催化」「风险挑战」这类组合不在表里 → 报告明明有
# 风险段落却报「缺少风险提示」。新逻辑改为**三通道任一命中即认为存在**：
#   ① 标题级：标题行（#/序号开头）里**含有「风险」二字** —— 不再枚举完整标题；
#   ② 内容级：全文风险信号词密度达标（信号词散落各段也认）；
#   ③ 引导级：「存在以下风险 / 面临风险」等引导短语。
# 误报方向宁紧勿松：漏报只会少提示一次，误报会反复打扰用户。

# ① 标题行：Markdown 标题 或 中文序号/数字序号/括号序号 开头的行，行内含「风险」
_RISK_HEADING = re.compile(
    r'^\s*(?:#{1,6}\s+'                       # ## 风险提示
    r'|[一二三四五六七八九十\d]+\s*[、.．：:]\s*'   # 四、风险与催化
    r'|[(（]\s*[一二三四五六七八九十\d]+\s*[)）]\s*)'  # （三）主要风险
    r'[^\n]*风险[^\n]*$', re.M)

# ② 内容信号词（出现次数 ≥3 即认定全文有风险论述）
_RISK_SIGNALS: Tuple[str, ...] = (
    '波动', '下行', '不及预期', '低于预期', '竞争加剧', '监管', '政策变化', '政策风险',
    '减值', '流动性', '回撤', '亏损', '价格战', '产能过剩', '诉讼', '违约', '退市',
    '补贴退坡', '地缘', '汇率', '估值过高', '透支', '瓶颈', '断供', '依赖',
    '客户集中', '放缓', '萎缩', '压力', '不确定性', '风险',
)

# ③ 引导短语
_RISK_LEAD = re.compile(
    r'(存在(?:以下|如下)风险|面临(?:的)?(?:主要)?风险|风险(?:提示|揭示)(?:如下)?|'
    r'以下风险(?:需要|值得)关注|相关风险|主要风险(?:在于|包括))')


def has_risk_section(body: str) -> Tuple[bool, str]:
    """多信号判定报告是否含风险段落。返回 ``(是否存在, 命中通道说明)``。"""
    if _RISK_HEADING.search(body):
        m = _RISK_HEADING.search(body).group().strip()
        return True, f'标题级命中：「{m[:30]}」'
    hits = sum(body.count(w) for w in _RISK_SIGNALS)
    if hits >= 3:
        return True, f'内容级命中：风险信号词出现 {hits} 次'
    if _RISK_LEAD.search(body):
        m = _RISK_LEAD.search(body).group().strip()
        return True, f'引导级命中：「{m[:30]}」'
    return False, ''


def check_missing_risk_section(text: str, has_finance_topic: bool = None) -> Optional[Dict[str, Any]]:
    """涉金融/估值的长文却没有独立的风险段落（多信号识别，见 has_risk_section）。"""
    body = str(text or '')
    if len(body) < 300:
        return None
    if has_finance_topic is None:
        has_finance_topic = any(k in body for k in _FINANCE_TOPIC)
    if not has_finance_topic:
        return None
    present, hit = has_risk_section(body)
    if present:
        return None
    return {
        'rule': 'missing_risk_section', 'label': '缺少风险提示段落', 'level': 'mid',
        'hits': ['三通道均未命中：无含「风险」的标题行、风险信号词 <3 次、无引导短语'],
        'advice': '投研类长文应含独立「风险提示」段落（标题如「风险提示」「风险与催化」'
                  '「风险因素」均可），至少覆盖：估值假设风险、数据时效风险、业务与竞争风险。',
    }


# 供 risk_review.review() 遍历：只放函数，正文由调用方在运行时注入
# （此前误写成 (fn, ()) 元组，导致 fn(*args) 缺参 → 全部检查静默跳过）
FINANCE_CHECKS = (
    check_currency_consistency,
    check_data_staleness,
    check_growth_extrapolation,
    check_extreme_valuation,
    check_missing_risk_section,
)