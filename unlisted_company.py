# -*- coding: utf-8 -*-
"""未上市公司研报：六段式模板 · 烧钱速度 · 同业 PS 对比。

## 为什么需要（2026-10-09 用户要求）

未上市公司（月之暗面、智谱这类）与上市公司的分析框架**本质不同**：

- **没有市值、没有股价** → DCF/PE 大多失效，只能用「融资口径 + 同业 PS 锚点」讨论；
- **没有公开财报** → 烧钱速度（累计融资 / 年亏损 = 还能撑几年）是 **IPO 紧迫性**的核心指标；
- 框架值得**固化为模板**，避免每次从零想：行业 → 竞争 → 商业模式 → 政策技术 →
  风险催化 → 估值锚点讨论（用户确认智谱报告的框架不错）。

三件套：

1. `unlisted_template()` —— 六段式骨架，每段带「该回答什么 / 该取什么数据 / 去哪取」；
2. `burn_runway()` —— 烧钱速度与可支撑年数；
3. `peer_ps_table()` —— 同业 PS 区间对比，判断标的锚点的相对位置。

**纪律**：未上市公司数据几乎全部来自新闻（C 级）——所有结论必须标注证据等级，
C/D 级数字不得支撑核心结论（见 ``evidence_grade``）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# ==================== ① 六段式模板 ====================

SECTIONS: Tuple[Tuple[str, str, str], ...] = (
    ('行业', '赛道规模与增速、处于什么阶段（导入/爆发/出清）',
     '行业报告（B）≥ 新闻（C）；规模数字必须标口径与年份'),
    ('竞争', '头部玩家、份额格局、差异化在哪、护城河是否真实',
     '公司公告/招股书（A）≥ 权威终端（B）；份额要有多个来源交叉'),
    ('商业模式', '怎么赚钱（To B / To C / API 计费）、单位经济模型是否成立',
     '招股书原文（A）≥ 深度报道（C）；收入结构必须有来源'),
    ('政策技术', '监管边界（备案/牌照）、技术路线风险（代际更替）、算力供给',
     '监管文件（A）≥ 权威解读（C）；政策原文优先于解读'),
    ('风险催化', '下行风险与上行催化各列 2-3 条，**每条绑定可观察信号**',
     '综合各段；风险段落必须独立成节'),
    ('估值锚点', '同业 PS 区间 + 融资口径倒推 + 期权式讨论；**给区间不给单点**',
     '融资轮次（A，工商/公告）≥ 权威媒体（C）；PS 对比见 peer_ps_table'),
)


def unlisted_template(company: str, *, filled: Optional[Dict[str, str]] = None) -> str:
    """渲染六段式研报骨架。``filled`` 可传入已写好的各段内容（缺省给要点提示）。"""
    filled = filled or {}
    lines = [f'# {company} · 未上市公司研究（六段式）', '',
             '> 证据等级口径：**A** 招股书原文/财报接口 · **B** 权威终端 · '
             '**C** 公开新闻 · **D** 百科/转载。**C/D 级数字不得支撑核心结论**。', '']
    for i, (name, ask, src) in enumerate(SECTIONS, 1):
        lines.append(f'## {i}. {name}')
        lines.append('')
        body = str(filled.get(name) or '').strip()
        if body:
            lines.append(body)
        else:
            lines.append(f'**应回答**：{ask}')
            lines.append('')
            lines.append(f'**数据来源**：{src}')
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


# ==================== ② 烧钱速度 ====================


def burn_runway(total_funding: float, annual_loss: float, *,
                unit: str = '亿元') -> Dict[str, Any]:
    """烧钱速度：累计融资 ÷ 年亏损 = **可支撑年数**（IPO 紧迫性的核心）。

    两项输入都必须来自**已核验**数据（融资额常有「美元/人民币」口径混淆，
    建议先用 ``number_sanity.unit_conflict`` 检查文本）。
    """
    out = {'total_funding': total_funding, 'annual_loss': annual_loss,
           'unit': unit, 'years': None, 'verdict_cn': '', 'level': '',
           'ok': False, 'reason': ''}
    for name, v in (('累计融资', total_funding), ('年亏损', annual_loss)):
        if v in (None, ''):
            out['reason'] = f'缺少 {name}，无法测算烧钱速度'
            return out
    try:
        f, l = float(total_funding), float(annual_loss)
    except (TypeError, ValueError) as e:
        out['reason'] = f'入参非数值: {e}'
        return out
    if l <= 0:
        out['ok'] = True
        out['years'] = None
        out['level'] = 'profitable'
        out['verdict_cn'] = '年亏损 ≤ 0（已盈利或打平），无烧钱紧迫性问题。'
        return out
    years = round(f / l, 2)
    out['ok'] = True
    out['years'] = years
    if years < 2:
        out['level'] = 'critical'
        out['verdict_cn'] = (f'🔴 **极紧迫**：累计融资 {f:g} {unit} ÷ 年亏损 {l:g} {unit} '
                             f'≈ **{years} 年**烧完——IPO/新一轮融资窗口非常紧，'
                             f'估值谈判地位弱。')
    elif years < 3:
        out['level'] = 'tight'
        out['verdict_cn'] = (f'🟡 **偏紧**：约 **{years} 年**可支撑——'
                             f'须在未来 1-2 年内证明收入放量，否则被迫降价融资。')
    elif years < 5:
        out['level'] = 'manageable'
        out['verdict_cn'] = (f'🟢 **可控**：约 **{years} 年**可支撑——'
                             f'有空间调整节奏，但增长须跑在消耗前面。')
    else:
        out['level'] = 'comfortable'
        out['verdict_cn'] = (f'🟢 **从容**：约 **{years} 年**可支撑——'
                             f'资金不是主要约束，竞争格局才是。')
    return out


# ==================== ③ 同业 PS 对比表 ====================


def peer_ps_table(peer_rows: Sequence[Dict[str, Any]], *,
                  target_ps: Optional[float] = None,
                  target_name: str = '标的') -> Dict[str, Any]:
    """同业 PS 对比：区间、中位数、标的锚点落点。

    ``peer_rows`` 每项含 ``name`` / ``ps``（可选 ``note`` 标口径，如 TTM/FY）。
    未上市公司估值判断**必须**给同业 PS 区间——孤立一个倍数没有意义。
    """
    rows = []
    for r in (peer_rows or []):
        ps = _f((r or {}).get('ps'))
        if ps is None or ps <= 0:
            continue
        rows.append({'name': str((r or {}).get('name') or '?'),
                     'ps': round(ps, 2),
                     'note': str((r or {}).get('note') or '')})
    out: Dict[str, Any] = {'rows': rows, 'n': len(rows), 'low': None, 'high': None,
                           'median': None, 'target_ps': target_ps, 'position': None,
                           'position_cn': '', 'ok': len(rows) >= 2, 'reason': '',
                           'table_md': ''}
    if len(rows) < 2:
        out['reason'] = '同业样本不足 2 家，无法给出 PS 区间（不猜）'
        return out
    pss = sorted(r['ps'] for r in rows)
    lo, hi = pss[0], pss[-1]
    mid = pss[len(pss) // 2] if len(pss) % 2 else round((pss[len(pss)//2 - 1] + pss[len(pss)//2]) / 2, 2)
    out.update({'low': lo, 'high': hi, 'median': mid})
    if target_ps is not None:
        t = float(target_ps)
        if t < lo:
            out['position'] = 'below'
            out['position_cn'] = (f'锚点 {t:g} 倍**低于**同业区间 [{lo:g}, {hi:g}] —— '
                                  f'要么是被低估，要么有未被定价的弱点，须二选一解释。')
        elif t > hi:
            out['position'] = 'above'
            out['position_cn'] = (f'⚠️ 锚点 {t:g} 倍**高于**同业区间 [{lo:g}, {hi:g}] —— '
                                  f'溢价需要理由（增速/稀缺性），说不出就下调锚点。')
        else:
            out['position'] = 'within'
            out['position_cn'] = (f'锚点 {t:g} 倍落在同业区间 [{lo:g}, {hi:g}] 内'
                                  f'（中位 {mid:g}），属市场可解释范围。')
    out['table_md'] = _render_ps(rows, lo, hi, mid, target_ps, target_name)
    return out


def _render_ps(rows: List[Dict[str, Any]], lo: float, hi: float,
               mid: Optional[float], target_ps: Optional[float],
               target_name: str) -> str:
    lines = ['**同业 PS 对比**', '',
             '| 公司 | PS（倍） | 口径 |', '| --- | --- | --- |']
    for r in rows:
        lines.append(f"| {r['name']} | {r['ps']:g} | {r['note'] or '—'} |")
    if target_ps is not None:
        lines.append(f"| **{target_name}（锚点）** | **{target_ps:g}** | 本系统锚点 |")
    lines += ['', f'同业区间 **{lo:g} ~ {hi:g} 倍**'
              + (f'（中位 {mid:g}）' if mid is not None else '') + '。']
    return '\n'.join(lines)


def _f(v: Any) -> Optional[float]:
    try:
        return float(str(v).replace(',', '').replace('%', '').replace('倍', '').strip())
    except (TypeError, ValueError):
        return None
