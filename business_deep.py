# -*- coding: utf-8 -*-
"""商用车基本盘深挖 + 技术面与事件联动。

## 一、商用车基本盘（2026-10-09 用户要求）

乘用车故事讲得再好，**基本盘决定安全边际**。用户点名要看四项：

| 维度 | 为什么重要 |
|---|---|
| **轻卡销量趋势** | 商用车主业的景气度直接决定现金流底盘 |
| **以旧换新拉动** | 政策是「增量」还是「透支未来需求」，性质完全不同 |
| **出口占比** | 出口毛利与国内不同，且能对冲国内周期 |
| **排放标准影响** | 国六/国七切换会引发**抢装 → 透支**的典型节奏 |

**每一项都是「有数据就展示、没有就说缺什么」**，不允许因为缺一项就整段不提。

## 二、技术面与事件联动（用户要求）

技术面单独看只是均线位置，**跟事件节点挂钩才有决策意义**。用户点名
「MA20 / 近月涨幅」× 「尊界上市节点」：

    「事件前已经在 MA20 上方并且涨了一个月」 → 预期多半已 price in，落地即兑现风险
    「事件前跌破 MA20 且走弱」               → 预期偏弱，若事件超预期则弹性大

本模块把这两维做成 **3×2 矩阵**（技术强/中/弱 × 事件临近/已落地），每格给出情景化判断。

零项目依赖（仅标准库）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)


# ==================== 通用：嵌套取值 ====================


def _norm(k: Any) -> str:
    return str(k).strip().lower().replace('_', '').replace(' ', '').replace('-', '')


def _dig(obj: Any, aliases: Iterable[str], *, max_depth: int = 5) -> Optional[Any]:
    """按别名**精确**（归一化后）在嵌套结构里取第一个非空值。"""
    target = {_norm(a) for a in aliases}
    queue = [(obj, 0)]
    while queue:
        cur, d = queue.pop(0)
        if d > max_depth:
            continue
        if isinstance(cur, dict):
            for k, v in cur.items():
                if _norm(k) in target and v not in (None, '', '—', '-'):
                    return v
                if isinstance(v, (dict, list)):
                    queue.append((v, d + 1))
        elif isinstance(cur, list):
            for v in cur[:20]:
                if isinstance(v, (dict, list)):
                    queue.append((v, d + 1))
    return None


def _f(v: Any) -> Optional[float]:
    try:
        return float(str(v).replace(',', '').replace('%', '').strip())
    except (TypeError, ValueError):
        return None


# ==================== 一、商用车基本盘 ====================


def _trend(series: Optional[Sequence[float]]) -> Dict[str, Any]:
    """由销量序列判断趋势方向（用首尾比较，避免过度拟合）。"""
    vals = [v for v in (_f(x) for x in (series or [])) if v is not None]
    if len(vals) < 2:
        return {'direction': 'unknown', 'text': '销量序列不足（至少需 2 期）'}
    first, last = vals[0], vals[-1]
    chg = (last - first) / abs(first) * 100.0 if first else None
    if chg is None:
        return {'direction': 'unknown', 'text': '基期为 0，无法算变化'}
    if chg > 3:
        d, cn = 'up', f'上行（{chg:+.1f}%）'
    elif chg < -3:
        d, cn = 'down', f'下行（{chg:+.1f}%）'
    else:
        d, cn = 'flat', f'基本走平（{chg:+.1f}%）'
    return {'direction': d, 'change_pct': round(chg, 2), 'first': first, 'last': last,
            'text': f'轻卡销量自 {first:g} → {last:g}，{cn}'}


def commercial_vehicle(data: Any) -> Dict[str, Any]:
    """商用车基本盘四项体检（轻卡趋势 / 以旧换新 / 出口占比 / 排放标准）。

    只展示取到的项；缺的进 ``gaps`` 并给出**该去哪取**。**不编造任何数字。**
    """
    d = data or {}
    gaps: List[str] = []

    series = _dig(d, ('light_truckseries', '轻卡销量序列', '轻卡月度销量', 'lighttrucksales'))
    if series is None and isinstance(_dig(d, ('轻卡', 'lighttruck', '商用车')), list):
        series = _dig(d, ('轻卡', 'lighttruck', '商用车'))
    tr = _trend(series) if series is not None else {'direction': 'unknown',
                                                    'text': '轻卡销量序列未获取'}
    if series is None:
        gaps.append('轻卡销量序列 → 月度产销快报 / 中汽协数据')

    yoy = _f(_dig(d, ('lighttruckyoy', '轻卡同比', '轻卡销量同比')))
    if yoy is None and tr.get('change_pct') is None:
        gaps.append('轻卡同比 → 同上')

    trade_in = _f(_dig(d, ('tradeinboost', '以旧换新拉动', '以旧换新增量', 'tradeinyoy')))
    if trade_in is None:
        gaps.append('以旧换新拉动幅度 → 商务部/地方细则 + 公司公告口径')

    export_share = _f(_dig(d, ('exportshare', '出口占比', '海外占比', 'exports')))
    if export_share is None:
        gaps.append('出口占比 → 年报「分地区收入」或月度出口数据')

    emission = _dig(d, ('emissionstandard', '排放标准', '国六', '国七', 'emission'))

    lines = ['', '#### 🚚 商用车基本盘', '']
    lines.append(f"**① 轻卡销量趋势**：{tr.get('text')}"
                 + (f"；同比 {yoy:+.1f}%" if yoy is not None else ''))
    if trade_in is not None:
        judge = ('偏**增量**（政策创造新增需求）' if trade_in > 0 else '需警惕需求透支')
        lines.append(f'**② 以旧换新拉动**：约 {trade_in:+.1f}% → 判断为 {judge}。'
                     f'⚠️ 以旧换新本质是**需求前置**，须区分「真增量」与「提前释放」。')
    else:
        lines.append('**② 以旧换新拉动**：未获取。')
    if export_share is not None:
        tag = ('出口占比高，可对冲国内周期' if export_share >= 20 else '出口占比不高，国内周期影响更大')
        lines.append(f'**③ 出口占比**：{export_share:.1f}% → {tag}。')
    else:
        lines.append('**③ 出口占比**：未获取。')
    if emission is not None:
        lines.append(f'**④ 排放标准**：{emission} → ⚠️ 标准切换期通常呈'
                     f'「**抢装 → 透支**」节奏，需盯紧切换时点前后的销量跳变。')
    else:
        lines.append('**④ 排放标准**：未获取。')

    if gaps:
        lines += ['', '> **数据缺口**（不臆测）：' + '；'.join(gaps) + '。']

    return {
        'trend': tr, 'yoy_pct': yoy, 'trade_in_boost': trade_in,
        'export_share_pct': export_share, 'emission': emission,
        'gaps': gaps, 'text': '\n'.join(lines),
        'variables': ['轻卡月度销量', '以旧换新细则与落地节奏', '出口量与出口占比',
                      '排放标准切换时点', '单车均价与毛利率'],
    }


# ==================== 二、技术面 × 事件联动 ====================

_STRONG, _NEUTRAL, _WEAK = 'strong', 'neutral', 'weak'
_UPCOMING, _LANDED = 'upcoming', 'landed'

_COMBO: Dict[tuple, Dict[str, str]] = {
    (_STRONG, _UPCOMING): {
        'stance': '**预期已较充分**',
        'text': '价格在 MA20 上方且近月走强，说明市场**已经在为事件定价**。'
                '事件落地时更可能是「利好兑现」，而非重新定价。',
        'risk': '⚠️ 事件不及预期时的**回撤风险更大**，因为乐观预期已计入价格。',
    },
    (_STRONG, _LANDED): {
        'stance': '**兑现后看持续性**',
        'text': '事件已落地且价格仍强，说明市场认可其**持续兑现能力**（而非一次性情绪）。',
        'risk': '需验证后续经营数据能否跟上，否则强势难以维持。',
    },
    (_NEUTRAL, _UPCOMING): {
        'stance': '**方向待事件催化**',
        'text': '价格在 MA20 附近、涨跌不明，市场处于**观望状态**，事件是主要的方向触发点。',
        'risk': '双向弹性都大，不宜在事件前重仓押注方向。',
    },
    (_NEUTRAL, _LANDED): {
        'stance': '**消化中**',
        'text': '事件落地后价格未明确选择方向，市场仍在**消化信息**。',
        'risk': '缺乏趋势，需等价格给出明确信号。',
    },
    (_WEAK, _UPCOMING): {
        'stance': '**预期偏低，弹性更大**',
        'text': '价格跌破 MA20 且近月走弱，说明**预期已经偏低**。若事件超预期，向上弹性更大；'
                '但也可能反映市场已提前知道不利信息。',
        'risk': '⚠️ 弱势 ≠ 便宜，需区分「错杀」与「先知先觉」。',
    },
    (_WEAK, _LANDED): {
        'stance': '**利空未出尽或兑现失败**',
        'text': '事件已落地但价格仍弱，说明市场认为**利好不足以扭转基本面**，或存在未反映的负面因素。',
        'risk': '在趋势明确反转前不宜抄底。',
    },
}


def classify_tech(technical: Any) -> Dict[str, Any]:
    """技术面分档：价 vs MA20、近月涨幅（3 档），并给出依据。"""
    t = technical or {}
    price = _f(_dig(t, ('price', 'close', '收盘', '最新价')))
    ma20 = _f(_dig(t, ('ma20', 'ma_20', '20日均线', 'ma20价')))
    month = _f(_dig(t, ('month_change', 'monthchange', '近月涨幅', '近20日涨幅', '月涨幅')))
    if price is None or ma20 is None:
        return {'state': 'unknown', 'price': price, 'ma20': ma20, 'month_change': month,
                'text': '缺价格或 MA20，无法判断技术面强弱'}
    above = price >= ma20
    votes = 1 if above else -1
    if month is not None:
        votes += 1 if month > 0 else (-1 if month < 0 else 0)
    state = _STRONG if votes >= 2 else (_WEAK if votes <= -2 else _NEUTRAL)
    return {
        'state': state, 'price': price, 'ma20': ma20, 'above_ma20': above,
        'month_change': month,
        'text': (f"收盘 {price:g} 元{'高于' if above else '低于'} MA20（{ma20:g}）"
                 + (f"，近月 {month:+.2f}%" if month is not None else '，近月涨幅未获取')),
    }


def classify_stage(events: Any) -> str:
    """事件阶段：临近 / 已落地（都没有则 unknown）。"""
    evs = events if isinstance(events, list) else ([events] if events else [])
    stages = set()
    for e in evs:
        if isinstance(e, dict):
            s = str(e.get('stage') or e.get('status') or '').strip().lower()
        else:
            s = str(e or '').strip().lower()
        if s in ('upcoming', 'pending', '临近', 'future', 'ahead', 'before'):
            stages.add(_UPCOMING)
        elif s in ('landed', 'done', '已落地', 'launched', 'released', 'after', 'post'):
            stages.add(_LANDED)
    if _UPCOMING in stages:
        return _UPCOMING
    if _LANDED in stages:
        return _LANDED
    return 'unknown'


def tech_event_link(technical: Any, events: Any = None, *,
                    event_name: str = '关键事件') -> Dict[str, Any]:
    """技术面 × 事件节点 → 情景化判断（而非孤立展示均线位置）。"""
    tech = classify_tech(technical)
    stage = classify_stage(events)
    stage_cn = {'upcoming': '临近', 'landed': '已落地', 'unknown': '未标注'}[stage]
    if tech['state'] == 'unknown' or stage == 'unknown':
        return {
            'tech': tech, 'stage': stage, 'stage_cn': stage_cn, 'combo': None,
            'text': (f'**{event_name}**：技术面或事件阶段信息不足，'
                     f'只能给出「{tech.get("text")}」的事实陈述，不做情景判断。'),
            'missing': [k for k, v in (('价格', tech.get('price')), ('MA20', tech.get('ma20')),
                                       ('事件阶段', None if stage != 'unknown' else ''))
                        if v in (None, '')],
        }
    combo = _COMBO[(_NEUTRAL if tech['state'] == 'unknown' else tech['state'], stage)]
    text = (f"**{event_name}（{stage_cn}）** × 技术面（{tech['text']}）→ "
            f"{combo['stance']}：{combo['text']}\n\n{combo['risk']}")
    return {'tech': tech, 'stage': stage, 'stage_cn': stage_cn, 'combo': combo,
            'text': text, 'missing': []}


def render_link(result: Dict[str, Any]) -> str:
    """报告用整段。"""
    return '\n'.join(['', '#### 📈 技术面 × 事件联动', '', result.get('text') or ''])
