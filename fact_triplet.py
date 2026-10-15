# -*- coding: utf-8 -*-
"""核验展示升级：**三元组** + 正文内联标注 + 证据等级。

1. **三元组**：报告原文表述 → 工具返回值 → 来源工具（含 A/B/C/D 证据等级列，
   C/D 级不得支撑核心结论，详见 ``evidence_grade``）。
2. **正文内联标注**：未核验数字在正文出现处就地标 ⚠️，不只末尾汇总。
3. 上下文提取按**完整句子**（2026-10-10 P0：不再固定窗口截断产生错乱片段）。

仅标准库；证据等级惰性引入，不可用时退化为 U，不中断渲染。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Set

logger = logging.getLogger(__name__)

# 未核验标记
MARK = ' ⚠️'
# 数字 token（含千分位、小数、百分号）
_NUM_RE = re.compile(r'(?<![\d.])-?\d+(?:,\d{3})*(?:\.\d+)?\s*%?')
# 中文数量单位，用于把「1340 亿元」这类整段抓出来做上下文
_UNIT_RE = re.compile(r'^\s*(?:亿|万|百万|千万|元|港元|美元|%|倍|个|辆|吨|家)?')


# ==================== 三元组 ====================


def locate_context(text: str, token: str, *, window: int = 22,
                   max_hits: int = 2) -> List[str]:
    """在报告正文里定位某个数字，返回它所在的**完整句子**（2026-10-10 P0 重写）。

    旧实现固定左右 22 字符截取，表格/长句场景产出「格式错乱片段」；
    现按句读（。！？!?；;\n）切句取整句，超长句以 token 为中心向两侧扩到 ≤160 字符。
    """
    body = str(text or '')
    tok = str(token or '').strip()
    if not body or not tok:
        return []
    pat = re.compile(r'(?<![\d.])' + re.escape(tok) + r'(?![\d])')
    out: List[str] = []
    for m in pat.finditer(body):
        out.append(_sentence_of(body, m.start(), m.end()))
        if len(out) >= max_hits:
            break
    return out


def _sentence_of(body: str, start: int, end: int, *,
                 cap: int = 160) -> str:
    """取 token 所在完整句子；超过 cap 时以 token 为中心向两侧扩展截断。"""
    left = max((body.rfind(ch, 0, start) for ch in '。！？!?；;\n'), default=-1)
    right_candidates = [body.find(ch, end) for ch in '。！？!?；;\n']
    right_candidates = [r for r in right_candidates if r != -1]
    right = min(right_candidates) if right_candidates else len(body)
    sent = body[left + 1: right].strip() or body[start:end]
    if len(sent) <= cap:
        return sent
    half = cap // 2
    mid = start - (left + 1)               # token 在句内的偏移
    e = min(len(sent), mid + half)
    s = max(0, e - cap)
    return '…' + sent[s:e].strip() + '…'


def grade_of_source(source: str) -> str:
    """来源的证据等级 A/B/C/D（用户口径：A 财报原文/接口、B 权威终端、C 新闻、D 百科）。

    惰性引入 ``evidence_grade``：等级判定不可用不应中断核验渲染，退化为 ``U``。
    """
    try:
        from evidence_grade import grade_of
        return grade_of(source)['grade']
    except Exception as e:                   # 判定失败不该影响核验表产出
        logger.debug('证据等级判定失败（%s）: %s', source, e)
        return 'U'


def build_triplets(final_text: str,
                   verified: Iterable[Dict[str, Any]],
                   *, sources: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
    """把「已核验数字」升级成三元组（+ 证据等级）。

    ``verified`` 每项形如 ``{'value','source','real'}``；
    返回每项追加 ``quote``（报告原文表述）、``tool_value``（工具返回）、``source_name``、
    ``grade``（证据等级 A/B/C/D）。
    """
    fallback = '、'.join(dict.fromkeys([s for s in (sources or []) if s])) or '本轮工具返回'
    out: List[Dict[str, Any]] = []
    for item in verified or []:
        val = item.get('value')
        src = item.get('source') or fallback
        quotes = locate_context(final_text, str(val))
        out.append({
            'quote': quotes[0] if quotes else f'（正文中未定位到 `{val}` 的上下文）',
            'tool_value': val,
            'source_name': src,
            'grade': item.get('grade') or grade_of_source(src),
            'extra_quotes': quotes[1:],
            'complete': bool(quotes),        # 三元组是否齐全（定位不到就不算完整）
        })
    return out


def render_triplets(triplets: List[Dict[str, Any]], *, limit: int = 12,
                    title: str = '已核验（可作为结论依据）') -> str:
    """渲染三元组区块（含**证据等级**列）。"""
    if not triplets:
        return ''
    lines = [f'**{title}**', '',
             '| 报告原文表述 | 工具返回 | 来源 | 证据等级 |', '|---|---|---|---|']
    for t in triplets[:limit]:
        quote = str(t.get('quote') or '').replace('|', '\\|')
        g = t.get('grade') or grade_of_source(t.get('source_name') or '')
        lines.append(f"| {quote} | `{t.get('tool_value')}` | {t.get('source_name')} | {g} |")
    rest = len(triplets) - limit
    if rest > 0:
        lines.append(f'| …另有 {rest} 处已核验数字 | | | |')
    lines.append('')
    lines.append('> 证据等级：**A** 财报原文/接口 · **B** 权威终端 · **C** 公开新闻 · '
                 '**D** 百科及其他。**C/D 级数字不得支撑核心结论**。')
    return '\n'.join(lines)


# ==================== 正文内联标注 ====================


# ==================== 数字黑名单（2026-10-09 用户要求） ====================
# 这些 token **不该进财务核验**：法规编号（18C、第 X 条）、产品型号（GLM-4、SU7、
# S800、MA20）、股票代码（600660、000550、HK1810、600660.SH）。
# 必须做「上下文判定」而非 token 自身判定 —— `_NUM_TOKEN_RE` 只会切出混合串里的
# **数字部分**（GLM-4 → '4'、S800 → '800'），不看前后邻字符根本认不出它是型号尾巴。

_MIXED_NEIGHBOR = re.compile(r'[A-Za-z\-]')
_CODE_PREFIXED = re.compile(r'\bHK\d{4,5}\b|\b\d{6}\.(?:SH|SZ|BJ|HK)', re.I)
_CODE_SUFFIXED = re.compile(r'^\.(?:SH|SZ|BJ|HK)', re.I)
_CODE_CTX_WORDS: Tuple[str, ...] = ('代码', '股票', '证券', '港股', 'A股', '美股',
                                    '上市', '(', '（', '标的')


def is_noise_token(tok: str, context: str = '', pos: int = -1) -> bool:
    """判定某数字 token 是否属于**不该进财务核验**的黑名单。

    ``context`` + ``pos`` 提供该 token 在原文中的位置（供前后邻字符判定）。
    黑名单：法规编号 / 产品型号 / 股票代码；正常财务数字一律返回 ``False``。
    """
    t = str(tok or '').strip().strip('.,;、）)：:')
    if not t:
        return True
    ctx = str(context or '')
    i = pos if (ctx and 0 <= pos < len(ctx)) else -1
    bare = t.replace(',', '')
    if i >= 0:
        pre = ctx[max(0, i - 2):i]
        post = ctx[i + len(t): i + len(t) + 2]
        # ① 数字紧邻字母/连字符（前或后）→ 型号/编号混合串的一部分：
        #    GLM-4 的 '4'、S800 的 '800'、MA20 的 '20'、18C 的 '18'、2025A 的 '2025'
        if _MIXED_NEIGHBOR.search(pre[-1:]) or _MIXED_NEIGHBOR.search(post[:1]):
            return True
        # ② 法条：「第 18 条」的 18（前两字符内出现「第」且本身是纯数字）
        if '第' in pre and re.fullmatch(r'\d+', bare):
            return True
    # ③ 股票代码形态
    if _CODE_PREFIXED.search(t):
        return True
    if re.fullmatch(r'\d{6}', bare) and not bare.startswith(('19', '20')):
        if bare.startswith('0'):          # 000550 这类 A 股代码（正文数字极少 0 开头）
            return True
        if i >= 0:
            pre8 = ctx[max(0, i - 8):i]
            post4 = ctx[i + len(t): i + len(t) + 4]
            if any(k in pre8 for k in _CODE_CTX_WORDS) or _CODE_SUFFIXED.search(post4):
                return True
    return False


def extract_numbers(text: str) -> Set[str]:
    """抽取文本中的数字 token（去掉空白与尾随单位，便于统一比对）。"""
    out: Set[str] = set()
    for m in _NUM_RE.finditer(str(text or '')):
        tok = m.group(0).strip()
        if tok:
            out.add(tok)
    return out


def annotate_inline(text: str, unverified_values: Iterable[Any], *,
                    mark: str = MARK, max_marks_per_token: int = 3) -> Dict[str, Any]:
    """在正文中给**未核验**数字就地加 ⚠️。

    返回 ``{'text','marked','skipped_in_code'}``。规则：
    - 只标未核验值，且用词边界匹配（`1.15` 不会命中 `1.150`）；
    - 已有 ⚠️ 的不重复标；
    - **代码围栏 ``` 内的内容不改**（那是原始数据/代码，不该插标记）。
    """
    body = str(text or '')
    if not body:
        return {'text': body, 'marked': 0, 'skipped_in_code': 0}

    values = [str(v).strip() for v in (unverified_values or []) if str(v).strip()]
    # 长 token 先处理，避免 `154` 抢先命中 `154.6`
    values = sorted(set(values), key=len, reverse=True)

    segments = _split_code_fences(body)
    marked = 0
    skipped = 0
    new_segments: List[tuple] = []
    for seg, is_code in segments:
        if is_code:
            skipped += len(values)
            new_segments.append((seg, True))
            continue
        new_seg = seg
        for v in values:
            # 负向断言：① 前不能是数字或小数点（防 `1.15` 命中 `1.150`）；
            #           ② 同一行内紧随其后的 10 个字符内已有 ⚠️ 则跳过 ——
            #              否则「1340 亿元 ⚠️」这类**单位后跟标记**的写法会被重复标注。
            pat = re.compile(r'(?<![\d.])' + re.escape(v) + r'(?![\d.])(?!.{0,10}⚠️)')
            cnt = {'n': 0}

            def _sub(m, _c=cnt):
                if _c['n'] >= max_marks_per_token:
                    return m.group(0)
                _c['n'] += 1
                return m.group(0) + mark

            new_seg = pat.sub(_sub, new_seg)
            marked += cnt['n']
        new_segments.append((new_seg, False))
    return {'text': ''.join(s for s, _ in new_segments), 'marked': marked,
            'skipped_in_code': skipped}


def _split_code_fences(text: str) -> List[tuple]:
    """按 ``` 围栏切分，返回 [(片段, 是否代码块)]。"""
    parts = text.split('```')
    out: List[tuple] = []
    for i, p in enumerate(parts):
        out.append((p, i % 2 == 1))
    return out


# ==================== 汇总渲染 ====================


def render_unverified(unverified: Iterable[Dict[str, Any]], *, limit: int = 12,
                      inline_marked: bool = True) -> str:
    """渲染未核验清单；``inline_marked=True`` 时提示"正文已就地标注"。"""
    rows = list(unverified or [])
    if not rows:
        return ''
    lines = ['**未核验（不得视为真实数据）**', '']
    if inline_marked:
        lines += ['> 下列数字**已在正文出现处就地标注 ⚠️**，请勿作为结论依据。', '']
    for it in rows[:limit]:
        val = it.get('value')
        reason = it.get('reason') or '未在本轮工具真实返回中找到'
        lines.append(f'- `{val}` ⚠️ — {reason}')
    if len(rows) > limit:
        lines.append(f'- …另有 {len(rows) - limit} 处未核验数字')
    return '\n'.join(lines)


def build_report(final_text: str, stats: Dict[str, Any], *,
                 annotate: bool = True) -> Dict[str, Any]:
    """一站式：三元组 + 内联标注 + 未核验清单。**不修改 stats 中的数字口径**。

    返回 ``{'text','triplets','unverified','marked'})``。
    """
    stats = stats or {}
    verified = stats.get('verified_data') or []
    unverified = stats.get('unverified_data') or []
    sources = stats.get('sources') or []
    # 🚨 衍生数字（2026-10-09 用户要求）：由**已核验输入**算出的值（如 240~300 亿 ÷ 7 亿
    # → 34~43 倍 PS）不属于「未核验」——把它们从未核验清单剔除，单列「本系统计算」节。
    derived = stats.get('derived_data') or []
    derived_texts = {str(d.get('value_text')).strip() for d in derived if d.get('value_text')}
    for d in derived:
        for k in ('value_low', 'value_high'):
            if d.get(k) is not None:
                derived_texts.add(str(d[k]))
                derived_texts.add(f"{d[k]:g}")
    if derived_texts:
        unverified = [u for u in unverified
                      if not _matches_derived(u.get('value'), derived_texts)]
    triplets = build_triplets(final_text, verified, sources=sources)
    text = str(final_text or '')
    marked = 0
    if annotate and unverified:
        res = annotate_inline(text, [u.get('value') for u in unverified])
        text, marked = res['text'], res['marked']
    return {'text': text, 'triplets': triplets, 'unverified': unverified,
            'derived': derived,
            'marked': marked, 'note': render_note(stats, triplets, unverified, marked)}


def _matches_derived(value: Any, derived_texts: Set[str]) -> bool:
    """未核验值是否实为衍生数字。

    三层匹配：① 文本相等/包含；② **数值近似**（容差 2%）—— 正文常写约数
    （34.3），系统算的是精确值（34.29），必须按数值比对才能对上。
    """
    v = str(value or '').strip().replace(',', '')
    if not v:
        return False
    fv = _to_f_loose(v)
    for dt in derived_texts:
        dt_n = str(dt).strip().replace(',', '')
        if not dt_n:
            continue
        if v == dt_n or v in dt_n or dt_n in v:
            return True
        if fv is not None:
            fd = _to_f_loose(dt_n)
            if fd is not None and fd != 0 and abs(fv - fd) / abs(fd) <= 0.02:
                return True
    return False


def _to_f_loose(v: str) -> Optional[float]:
    """宽松转 float（容忍尾随单位/百分号/倍），失败返回 None。"""
    try:
        return float(re.sub(r'[%倍美元元万亿点\s]', '', str(v or '')).strip())
    except (TypeError, ValueError):
        return None


def _render_derived(derived: List[Dict[str, Any]]) -> str:
    """渲染「本系统计算」节：由已核验输入推导的数字，**不算未核验**（2026-10-09）。"""
    lines = ['**本系统计算（由已核验输入推导，非外部数据）**', '']
    for d in derived[:8]:
        val = (d.get('value_text')
               or f"{d.get('value_low')}~{d.get('value_high')}")
        unit = str(d.get('out_unit') or '')
        suffix = '' if (not unit or unit in str(val)) else f' {unit}'
        lines.append(f"- `{val}`{suffix} — {d.get('label') or '衍生值'} = "
                     f"{d.get('formula') or '—'}（{d.get('note') or '输入已核验'}）")
    return '\n'.join(lines)


def _tight_metric(quote: str, value: Any, *, width: int = 10) -> str:
    """把 quote 收窄到**只围绕该数字**的一小段。

    否则一段话里含多个指标（如同时出现「毛利率」与「销量」），会让口径校验串味。
    """
    s = str(quote or '')
    v = str(value or '')
    i = s.find(v)
    if i < 0:
        return s
    return s[max(0, i - width): i + len(v) + width]


def _evidence_check(triplets: List[Dict[str, Any]]) -> str:
    """证据等级审计：核心结论是否被 C/D 级来源支撑（P2：自动标记「结论置信度低」）。"""
    try:
        from evidence_grade import audit as _audit, render_verdict as _verdict
    except Exception as e:                   # 审计不可用不影响核验区渲染
        logger.debug('证据等级审计不可用: %s', e)
        return ''
    rows = [{'metric': _tight_metric(t.get('quote'), t.get('tool_value')),
             'value': t.get('tool_value'), 'source': t.get('source_name') or ''}
            for t in triplets or []]
    res = _audit(rows, core=True)
    if res.get('confidence') in ('low', 'invalid'):
        return _verdict(res)
    return ''


def render_note(stats: Dict[str, Any], triplets: List[Dict[str, Any]],
                unverified: List[Dict[str, Any]], marked: int) -> str:
    """生成【核验数据】整段（三元组 + 未核验清单）。"""
    total = int(stats.get('total') or 0)
    if total <= 0:
        return ''
    lines = ['', '', '## 核验数据（数据必须真实）',
             f"共识别 **{total}** 处数字：**已核验 {stats.get('verified', 0)}** · "
             f"**未核验 {stats.get('unverified', 0)}**"
             f"（未核验率 {stats.get('ratio', 0):.0%}）。",
             '',
             '每条已核验数字给出**三元组**：报告原文表述 → 工具返回 → 来源。']
    if triplets:
        lines += ['', render_triplets(triplets)]
        ev = _evidence_check(triplets)
        if ev:
            lines += ['', ev]
    derived = stats.get('derived_data') or []
    if derived:
        lines += ['', _render_derived(derived)]
    if unverified:
        lines += ['', render_unverified(unverified, inline_marked=bool(marked))]
        if marked:
            lines.append(f"- 正文已就地标注 **{marked}** 处 ⚠️。")
        lines += ['', '> ⚠️ **真实性约束**：未核验数字**不得**作为投资/研究结论依据；'
                      '请改为标注「待补充」，或重新调用工具取数后再引用。']
    else:
        lines += ['', '> ✓ 报告数字均已对上本轮工具真实返回，可作为分析依据。']
    return '\n'.join(lines)
