# -*- coding: utf-8 -*-
"""report_degrade.py — 红色审计降级 + 风险评级净化（2026-10-09 用户 P0 三项）

## 为什么需要

证据审计（evidence_grade）判定 🔴 invalid（核心结论只依赖 C/D 级来源）后，
报告**正文照旧给出操作建议**——审计说「不得作为方向性依据」，报告却还在给方向，
两者自相矛盾。用户 P0 要求：

1. **红色审计触发后必须降级报告**：剥离「操作建议/投资建议/方向性判断」节，
   顶部加横幅：``⚠️ 本报告核心结论依赖 C/D 级来源，已降级为「待补充证据」，
   暂不提供方向性判断。``
2. **未核验数字不得进入风险评级和操作建议**：风险段落里含未核验数字的行，
   星级（★…）一律替换为「待补充」，不得给出 ★★★☆☆。
3. **审计违规具体清单**：``N 处未走首选工具`` 必须逐条列出——指标、应走工具、
   实际用了什么（清单本体在 evidence_grade.render_verdict，本模块负责降级联动）。

接入点：``agent_fact_audit._audit_fact_sources`` —— 它返回的
``annotated_text`` / ``note`` 会被 ai_agent 原样使用，因此 ai_agent 零改动。

零项目硬依赖（惰性 import evidence_grade，失败不阻断核验区渲染）。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

DEGRADE_BANNER = ('> ⚠️ **本报告核心结论依赖 C/D 级来源，已降级为「待补充证据」，'
                  '暂不提供方向性判断。**')

# P0-1（2026-10-10）：黄色审计（low）→ 操作建议前加中等置信度标注
ADVICE_FLAG = '> ⚠️ 本建议基于部分 C/D 级来源，置信度中等'

# P0-3（2026-10-10）：走错工具 → 就地标注
_MISMATCH_MARK = '工具来源不匹配'

# P0-1（2026-10-10 第四批，第三次同类问题）：红色降级**硬约束**（规则层，不靠 LLM 自觉）
_DIRECTIONAL = re.compile(
    r'拐点|马太效应|必将|看好|看空|反转|见顶|触底回升|持续增长|持续下滑|'
    r'趋势向好|趋势向坏|景气上行|景气下行|确定性机会|即将迎来|'
    r'碾压|最优选择|不可撼动|遥遥领先|断层第一|断层领先|护城河深|无懈可击|压制对手|'
    r'行业龙头|龙头地位')
SECTION_BANNER = '> ⚠️ 基于 C 级来源，暂不提供方向性判断，仅列数据对比。'
_DIR_HIDDEN = '（方向性判断已隐藏，待补充证据）'
_STAR_HIDDEN = '评级已隐藏（C 级来源，不提供方向性评级）'
_STAR_ANY = re.compile(r'[★☆]+')


def _strip_all_stars(text: str) -> Tuple[str, int]:
    """红色降级下：删除**全文**星级评级（不只风险段）。"""
    n = 0
    def _rep(m):
        nonlocal n
        n += 1
        return _STAR_HIDDEN
    return _STAR_ANY.sub(_rep, str(text or '')), n

# 触发降级剥离的建议类节标题（用户口径：审计红 → 不给方向）
_ADVICE_HEADING = re.compile(
    r'^(#{1,6})\s*(.*)$|^\*\*(.+?)\*\*\s*$')
_ADVICE_WORDS = ('操作建议', '投资建议', '方向性判断', '投资评级', '评级建议',
                 '买卖建议', '目标价', '综合结论', '买入', '卖出', '增持', '减持')
_RISK_HEADING = re.compile(r'(风险|雷点|排雷)')
_STAR = re.compile(r'[★☆]{2,}')
_NUMISH = re.compile(r'[\d]')


def apply(final_text: str, rep: Dict[str, Any],
          stats: Dict[str, Any]) -> Dict[str, Any]:
    """主入口：三元组报告 ``rep`` + 核验 ``stats`` → 降级/净化后的正文与元信息。

    返回 ``{'text','degraded','confidence','violations_md'}``。
    任何失败都回退原文（核验链路不允许因装饰逻辑崩溃）。
    """
    text = str(final_text or '')
    out = {'text': text, 'degraded': False, 'confidence': '',
           'violations_md': ''}
    try:
        ev = _evidence_audit(rep)
        conf = str(ev.get('confidence') or '')
        out['confidence'] = conf
        stats['evidence_confidence'] = conf
        if conf == 'invalid':
            text = _strip_advice_sections(text)
            if not text.strip():
                text = str(final_text or '')
            # P0-1（第四批）：星级全删（规则层硬约束）
            text, n_star = _strip_all_stars(text)
            if n_star:
                stats['stars_hidden'] = n_star
            # 剩余节的方向性表述一并约束
            text, n_dir, n_sec = _constrain_directional(text)
            if n_sec:
                stats['directional_constrained'] = n_sec
            banner = DEGRADE_BANNER
            text = banner + '\n\n' + text if text.strip() else banner
            out['degraded'] = True
            stats['report_degraded'] = True
        elif conf == 'low':
            # P0-1：黄色审计不剥离正文，但在建议节前插入置信度标注
            text, n_flag = _flag_advice(text)
            if n_flag:
                stats['advice_flagged'] = n_flag
        # P0-3：走错工具的数字就地标注「工具来源不匹配，置信度降低」
        text, n_mm = _mark_mismatch(text, ev)
        if n_mm:
            stats['tool_mismatch_marked'] = n_mm
        # P0-3：未核验数字不进风险评级（无论审计是否红色，未核验一律待补充）
        unverified = [str(u.get('value') or '') for u in (stats.get(
            'unverified_data') or []) if u.get('value') is not None]
        derived = stats.get('derived_data') or []
        text2, n_star = _sanitize_risk(text, unverified)
        if n_star:
            out['degraded'] = out['degraded'] or False
            stats['risk_rating_pending'] = n_star
        out['text'] = text2
        # P0-4：违规具体清单（若有）
        out['violations_md'] = _violations_md(ev)
    except Exception as e:                       # noqa: BLE001 — 装饰逻辑兜底
        logger.debug('report_degrade 跳过: %s', e)
        out['text'] = str(final_text or '')
    return out


def _evidence_audit(rep: Dict[str, Any]) -> Dict[str, Any]:
    """对三元组重跑证据等级审计（与 fact_triplet._evidence_check 同口径）。"""
    try:
        from evidence_grade import audit as _audit
        from fact_triplet import _tight_metric
    except Exception as e:                       # noqa: BLE001
        logger.debug('evidence_grade 不可用: %s', e)
        return {}
    triplets = (rep or {}).get('triplets') or []
    rows = [{'metric': _tight_metric(t.get('quote'), t.get('tool_value')),
             'value': t.get('tool_value'),
             'source': t.get('source_name') or ''}
            for t in triplets]
    return _audit(rows, core=True)


# ==================== P0-1：剥离建议类节 ====================


def _strip_advice_sections(text: str) -> str:
    """删除建议类小节的**正文**，标题行替换为「待补充证据」。

    标题形态：markdown 标题（# …）或加粗行（**…**），含 _ADVICE_WORDS 之一。
    围栏代码块（```）内不动。
    """
    lines = str(text or '').split('\n')
    out: List[str] = []
    in_code = False
    skipping = False
    head_level = 0
    for ln in lines:
        if ln.strip().startswith('```'):
            in_code = not in_code
            out.append(ln)
            continue
        if in_code:
            out.append(ln)
            continue
        hit = _match_advice_heading(ln)
        if hit:
            level, title = hit
            skipping = True
            head_level = level
            out.append(f'{ln} ｜ *（待补充证据，暂不提供方向性判断）*')
            continue
        if skipping:
            lvl = _heading_level(ln)
            if lvl and lvl <= head_level:        # 同级/更高级标题 → 本节结束
                skipping = False
                out.append(ln)
            # 否则吞掉本行（建议正文）
            continue
        out.append(ln)
    return '\n'.join(out)


def _heading_level(ln: str) -> int:
    s = ln.strip()
    if s.startswith('#'):
        return len(s) - len(s.lstrip('#'))
    if s.startswith('**') and s.rstrip().endswith('**'):
        return 7                                 # 加粗行视作最深一级
    return 0


def _match_advice_heading(ln: str) -> Tuple[int, str] | None:
    s = ln.strip()
    if not s:
        return None
    lvl = _heading_level(s)
    if not lvl:
        return None
    title = s.lstrip('#').strip().strip('*').strip()
    if any(w in title for w in _ADVICE_WORDS):
        return lvl, title
    return None


# ==================== P0-3：风险段落净化 ====================


def _sanitize_risk(text: str, unverified: List[str]) -> Tuple[str, int]:
    """风险段落里：含未核验数字的行，星级替换为「待补充」。

    风险段落判定：标题（#/加粗行）含「风险」起，到下一个标题止。
    返回 (新文本, 替换的星级行数)。
    """
    vals = [v.replace(',', '').strip() for v in unverified if v.strip()]
    vals = [v for v in vals if _NUMISH.search(v)]
    if not vals:
        return text, 0
    lines = str(text or '').split('\n')
    out: List[str] = []
    in_code = False
    in_risk = False
    n = 0
    for ln in lines:
        if ln.strip().startswith('```'):
            in_code = not in_code
            out.append(ln)
            continue
        if in_code:
            out.append(ln)
            continue
        lvl = _heading_level(ln.strip())
        if lvl:
            title = ln.strip().lstrip('#').strip().strip('*').strip()
            in_risk = bool(_RISK_HEADING.search(title))
        if in_risk and _STAR.search(ln):
            bare = ln.replace(',', '')
            if any(v in bare for v in vals):
                new = _STAR.sub('待补充', ln)
                if new != ln:
                    n += 1
                    ln = new + '（数字未核验，评级待补充）'
        out.append(ln)
    return '\n'.join(out), n


# ==================== P0-1：方向性表述约束（红色审计） ====================


def _constrain_directional(text: str) -> Tuple[str, int, int]:
    """对红色降级后的剩余正文：含方向性表述的句子替换为占位，
    所在节标题后插入声明。返回 (新文本, 隐藏句数, 受约束节数)。"""
    lines = str(text or '').split('\n')
    out: List[str] = []
    in_code = False
    n_sent = 0
    flagged: List[int] = []                    # 需插声明的 out 下标集合
    for ln in lines:
        if ln.strip().startswith('```'):
            in_code = not in_code
            out.append(ln)
            continue
        if in_code:
            out.append(ln)
            continue
        if _DIRECTIONAL.search(ln) and not _heading_level(ln.strip()):
            new_ln, k = _hide_directional_sentences(ln)
            if k:
                n_sent += k
                # 记录该行所属节标题在 out 中的位置
                for idx in range(len(out) - 1, -1, -1):
                    if _heading_level(out[idx].strip()):
                        flagged.append(idx)
                        break
            out.append(new_ln)
        else:
            out.append(ln)
    # 去重后按行号插入声明（倒序插入避免位移）
    n_sec = 0
    for idx in sorted(set(flagged), reverse=True):
        out.insert(idx + 1, SECTION_BANNER)
        n_sec += 1
    return '\n'.join(out), n_sent, n_sec


def _hide_directional_sentences(ln: str) -> Tuple[str, int]:
    """把一行内含方向性词的句子替换为占位（按中英文句读切分）。"""
    parts = re.split(r'(?<=[。！？!?；;])', ln)
    outp: List[str] = []
    k = 0
    for p in parts:
        if _DIRECTIONAL.search(p):
            outp.append(_DIR_HIDDEN)
            k += 1
        else:
            outp.append(p)
    return ''.join(outp), k


# ==================== P0-1：黄色审计建议标注 ====================


def _flag_advice(text: str) -> Tuple[str, int]:
    """在建议类标题行后插入中等置信度标注（不剥离正文）。"""
    lines = str(text or '').split('\n')
    out: List[str] = []
    in_code = False
    n = 0
    for ln in lines:
        out.append(ln)
        if ln.strip().startswith('```'):
            in_code = not in_code
            continue
        if in_code:
            continue
        hit = _match_advice_heading(ln)
        if hit:
            out.append(ADVICE_FLAG)
            n += 1
    return '\n'.join(out), n


# ==================== P0-3：走错工具就地标注 ====================


def _mark_mismatch(text: str, ev: Dict[str, Any]) -> Tuple[str, int]:
    """审计 rows 中 fallback/mismatch 行的数值，在正文首次出现处行尾标注。

    标注文案：``⚠️（工具来源不匹配：应走 `preferred`，实际 `used`，置信度降低）``。
    代码块内、已标注行不重复。
    """
    rows = (ev or {}).get('rows') or []
    marks: List[Tuple[str, str, str]] = []
    for r in rows:
        tc = r.get('tool_check') or {}
        if tc.get('level') in ('fallback', 'mismatch'):
            val = str(r.get('value') or '').strip()
            if val:
                marks.append((val, tc.get('preferred') or '未登记',
                              tc.get('used') or '未标注'))
    if not marks:
        return text, 0
    lines = str(text or '').split('\n')
    out: List[str] = []
    in_code = False
    pending = list(marks)
    n = 0
    for ln in lines:
        if ln.strip().startswith('```'):
            in_code = not in_code
            out.append(ln)
            continue
        if not in_code and pending:
            keep: List[Tuple[str, str, str]] = []
            for val, pref, used in pending:
                if val in ln and _MISMATCH_MARK not in ln:
                    ln = (f'{ln} ⚠️（{ _MISMATCH_MARK }：应走 `{pref}`，'
                          f'实际 `{used}`，置信度降低）')
                    n += 1
                else:
                    keep.append((val, pref, used))
            pending = keep
        out.append(ln)
    return '\n'.join(out), n


# ==================== P0-4：违规清单联动 ====================


def _violations_md(ev: Dict[str, Any]) -> str:
    """从审计结果渲染违规清单（数据 → 应走工具 → 实际来源 → 降级原因）。"""
    vi = (ev or {}).get('violations') or []
    if not vi:
        return ''
    lines = [f'**未走首选工具清单（{len(vi)} 处）：**']
    level_note = {'fallback': '首选工具未用，来源降级',
                  'mismatch': '口径与首选工具不一致'}
    for v in vi[:12]:
        note = level_note.get(v.get('level') or '', '口径存疑')
        val = f'（值 {v.get("value")}）' if v.get('value') else ''
        lines.append(f"- 「{v.get('metric') or '?'}」{val}"
                     f"应走 `{v.get('preferred')}`，"
                     f"实际用了 `{v.get('used') or '未标注工具'}` —— {note}")
    return '\n'.join(lines)
