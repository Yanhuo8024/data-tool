#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""risk_review.py - 风控初审 Agent（确定性规则审查）

券商清单要求「风控/合规初审 Agent：风险识别、资料初筛、异常提示，**转人工审核**」。
本项目的"风控合规官"此前只是 `agent_pipeline` 里的**一段提示词**——由 LLM 自行决定
要不要提风险，无规则、无阈值、无留痕、无转人工动作，属典型的"形式有、实质弱"。

本模块把初审做成**确定性规则**（不依赖模型判断，可复现、可审计）：

  1. `return_pledge`       收益承诺 / 保本表述      —— 合规红线（HIGH）
  2. `trade_advice`        具体买卖指令 / 荐股      —— 合规红线（HIGH）
  3. `missing_disclaimer`  涉金融结论却无免责声明    —— 需复核（MID）
  4. `data_incomplete`     资料完整性初筛（缺字段/样本量）—— 需复核（MID）
  5. `unverified_numbers`  正文数字未能溯源到工具返回 —— 需复核（MID）

**误报控制**：合规文本常在**否定句**中出现敏感词（"不得承诺保本保收益"
"不构成投资建议，不推荐买入"），命中位置前若存在否定词则不计违规。这条不做，
风控模块会把自己的免责声明判成违规。

裁定：任一 HIGH → `block`（须人工复核后才可外发）；仅 MID → `needs_review`；
无命中 → `pass`。`handoff=True` 表示需转人工。
"""
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 否定窗口：命中词前 10 字符内出现下列词 → 视为合规性引用/否定表述，不计违规
_NEGATIONS = ('不', '无', '非', '禁止', '不得', '不能', '没有', '未', '拒绝', '严禁', '禁止')

_RETURN_PLEDGE_WORDS = (
    '保本保息', '保本保收益', '保证收益', '保证本金', '承诺收益', '稳赚不赔', '稳赚',
    '包赚', '必赚', '零风险', '无风险收益', '锁定收益', '刚性兑付', '一定赚',
    '绝对不会亏', '稳赢', '躺赚',
)

# 买卖指令：需要"祈使/建议"语境才算（单纯出现"买入"是中性词）
_TRADE_VERBS = ('买入', '卖出', '增持', '减持', '加仓', '减仓', '清仓', '全仓', '重仓')
_TRADE_CUES = ('建议', '推荐', '应该', '可以买', '不妨', '值得买', '果断', '立即', '马上')

_DISCLAIMER_MARKERS = (
    '不构成投资建议', '不构成任何投资建议', '投资有风险', '市场有风险',
    '仅供参考', '风险自担', '以官方最新', '不承诺收益', '不保证收益', '不荐股',
)

# 出现这些词说明是金融/投研结论，应带免责声明
_FINANCE_TOPIC = (
    '估值', '市盈率', 'PE', 'PB', 'ROE', '收益率', '净值', '基金', '债券', '股票',
    '组合', '配置', '理财', '行情', '财报', '净利润', '现金流', '回撤', '夏普',
)

_LEVEL_RANK = {'low': 0, 'mid': 1, 'high': 2}


def _negated(text: str, idx: int, window: int = 10) -> bool:
    """命中位置前 window 字符内是否有否定词（避免把免责声明判成违规）。"""
    seg = text[max(0, idx - window):idx]
    return any(n in seg for n in _NEGATIONS)


def _scan_words(text: str, words) -> List[str]:
    """扫描敏感词，跳过否定语境中的命中。"""
    hits: List[str] = []
    for w in words:
        start = 0
        while True:
            i = text.find(w, start)
            if i < 0:
                break
            if not _negated(text, i):
                hits.append(w)
                break
            start = i + len(w)
    return hits


def check_return_pledge(text: str) -> Optional[Dict[str, Any]]:
    """规则 1：收益承诺 / 保本表述（合规红线）"""
    hits = _scan_words(text, _RETURN_PLEDGE_WORDS)
    if not hits:
        return None
    return {
        'rule': 'return_pledge', 'label': '收益承诺 / 保本表述', 'level': 'high',
        'hits': hits,
        'advice': '删除承诺性表述，改为「业绩比较基准」「历史业绩不代表未来」等合规口径，'
                  '并补充风险提示。此类表述违反资管新规与基金销售适当性要求。',
    }


def check_trade_advice(text: str) -> Optional[Dict[str, Any]]:
    """规则 2：具体买卖指令 / 荐股（合规红线）"""
    hits: List[str] = []
    for verb in _TRADE_VERBS:
        start = 0
        while True:
            i = text.find(verb, start)
            if i < 0:
                break
            seg = text[max(0, i - 12):i]
            if not _negated(text, i) and any(c in seg for c in _TRADE_CUES):
                hits.append(seg[-6:] + verb)
                break
            start = i + len(verb)
    if not hits:
        return None
    return {
        'rule': 'trade_advice', 'label': '买卖指令 / 荐股', 'level': 'high',
        'hits': hits,
        'advice': '改为客观数据陈述（估值分位、财务指标对比），删除操作指令，'
                  '并声明「不构成投资建议」。向不特定对象提供买卖建议属证券投资咨询业务，需持牌。',
    }


def check_disclaimer(text: str, has_finance_topic: bool = None) -> Optional[Dict[str, Any]]:
    """规则 3：涉金融结论但缺免责声明"""
    if not text or len(str(text)) < 60:
        return None
    body = str(text)
    topic = has_finance_topic
    if topic is None:
        topic = any(k in body for k in _FINANCE_TOPIC)
    if not topic:
        return None
    if any(m in body for m in _DISCLAIMER_MARKERS):
        return None
    return {
        'rule': 'missing_disclaimer', 'label': '缺少免责声明', 'level': 'mid', 'hits': [],
        'advice': '涉金融内容的结论须附风险提示（如「不构成投资建议，市场有风险」）。',
    }


def check_data_incomplete(file_name: str = None, stats: Dict[str, Any] = None,
                          max_missing: float = 0.5, min_rows: int = 30) -> Optional[Dict[str, Any]]:
    """规则 4：资料完整性初筛（针对上传的数据文件）

    检查项：样本量是否足够、是否存在高缺失率字段。信息不足时不报（避免噪声）。
    """
    if not file_name and not stats:
        return None
    s = stats or {}
    rows = s.get('rows')
    missing_top = s.get('missing_top') or []      # [(列名, 缺失率)]
    problems: List[str] = []
    if isinstance(rows, int) and rows < min_rows:
        problems.append(f"有效样本仅 {rows} 行，低于建模建议下限 {min_rows} 行，统计结论稳定性不足")
    for col, rate in missing_top:
        try:
            if float(rate) >= max_missing:
                problems.append(f"字段 `{col}` 缺失率 {float(rate):.0%}，高于阈值 {max_missing:.0%}，"
                                f"其分析结论不可用")
        except (TypeError, ValueError):
            logger.debug('缺失率解析跳过: %r', rate)
    if not problems:
        return None
    return {
        'rule': 'data_incomplete', 'label': '资料完整性不足', 'level': 'mid',
        'hits': problems,
        'advice': '补齐关键字段或扩充样本后再出具结论，否则结论仅在受限范围内成立，需人工确认。',
    }


def check_unverified_numbers(stats: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """规则 5：正文数字未能溯源到工具真实返回（复用 fact_audit 统计）

    注意：`stats['ratio']` 语义是**未核验率**（不是通过率），> 0.5 视为异常。
    """
    if not isinstance(stats, dict) or not stats.get('total'):
        return None
    ratio = stats.get('ratio')
    try:
        ratio = float(ratio)
    except (TypeError, ValueError):
        return None
    if ratio <= 0.5:
        return None
    unverified = [d.get('value') for d in (stats.get('unverified_data') or [])][:6]
    return {
        'rule': 'unverified_numbers', 'label': '正文数字未溯源', 'level': 'mid',
        'hits': [f"未核验率 {ratio:.0%}（共 {stats.get('total')} 个数字，"
                 f"{stats.get('unverified')} 个无法匹配工具返回）"] + unverified,
        'advice': '数字必须来自本轮成功工具的真实返回；无法溯源的数字须删除或标注为估算，'
                  '严禁以「看起来合理」的数值替代。',
    }


def review(text: str, *, current_file: str = None, file_stats: Dict[str, Any] = None,
           fact_stats: Dict[str, Any] = None, min_length: int = 0) -> Dict[str, Any]:
    """执行完整初审，返回裁定结果。

    返回：{'verdict','level','findings','handoff','checked','summary'}
      verdict: pass | needs_review | block
    """
    body = str(text or "")
    findings: List[Dict[str, Any]] = []
    if len(body) >= min_length:
        for fn, args in ((check_return_pledge, (body,)),
                         (check_trade_advice, (body,)),
                         (check_disclaimer, (body,))):
            try:
                r = fn(*args)
            except Exception as e:                    # 单条规则失败不拖垮整体初审
                logger.warning('风控规则 %s 执行失败: %s', getattr(fn, '__name__', fn), e)
                continue
            if r:
                findings.append(r)
    try:
        r = check_data_incomplete(current_file, file_stats)
        if r:
            findings.append(r)
    except Exception as e:
        logger.warning('资料完整性检查失败: %s', e)
    try:
        r = check_unverified_numbers(fact_stats)
        if r:
            findings.append(r)
    except Exception as e:
        logger.warning('数字溯源检查失败: %s', e)

    level = 'low'
    for f in findings:
        if _LEVEL_RANK.get(f['level'], 0) > _LEVEL_RANK[level]:
            level = f['level']
    if level == 'high':
        verdict = 'block'
    elif findings:
        verdict = 'needs_review'
    else:
        verdict = 'pass'
    summary = {
        'pass': '未发现合规风险项与资料缺陷。',
        'needs_review': f'发现 {len(findings)} 项需复核事项，建议人工确认后再对外使用。',
        'block': f'发现 {len(findings)} 项问题，其中含合规红线，**须人工复核后方可外发**。',
    }[verdict]
    return {
        'verdict': verdict, 'level': level, 'findings': findings,
        'handoff': verdict != 'pass', 'checked': len(body), 'summary': summary,
    }


_VERDICT_ICON = {'pass': '✅', 'needs_review': '🟡', 'block': '🔴'}
_VERDICT_TEXT = {'pass': '通过', 'needs_review': '需人工复核', 'block': '拦截 · 转人工'}


def render_review(result: Dict[str, Any]) -> str:
    """把初审结果渲染为 Markdown 块。"""
    if not isinstance(result, dict) or not result.get('findings'):
        return ""
    verdict = result.get('verdict', 'pass')
    lines = ["", "---", f"#### 🛡️ 风控初审：{_VERDICT_ICON.get(verdict, '')} "
                       f"{_VERDICT_TEXT.get(verdict, verdict)}", result.get('summary', "")]
    for f in result['findings']:
        icon = '🔴' if f['level'] == 'high' else '🟡'
        lines.append(f"- {icon} **{f['label']}**"
                     + (f"：{'；'.join(str(h) for h in f['hits'][:3])}" if f.get('hits') else ""))
        if f.get('advice'):
            lines.append(f"  - 处置建议：{f['advice']}")
    if result.get('handoff'):
        lines.append("")
        lines.append("> ⚖️ 本结论由**确定性规则**产出（可复现、可审计），已标记 **转人工审核**；"
                     "未通过人工确认前，请勿将该内容对外发布或用于决策。")
    return "\n".join(lines)


def attach(payload: Dict[str, Any], *, current_file: str = None,
           fact_stats: Dict[str, Any] = None, min_length: int = 120) -> Dict[str, Any]:
    """出口统一挂载：给响应体补 `risk_review` 字段并按需追加审查块（幂等）。

    只审查达到 `min_length` 的回答——短应答（"好的"、纯数字回执）没有审查价值，
    全量审查只会制造噪声。
    """
    if not isinstance(payload, dict) or payload.get('ok') is False:
        return payload
    reply = payload.get('reply')
    if not isinstance(reply, str) or len(reply) < min_length:
        return payload
    if '🛡️ 风控初审' in reply:
        return payload
    # 数字溯源统计由 agent 链路写入 payload['fact_audit']，此处自动取用，
    # 避免调用方漏传导致"有未核验数字却报通过"。
    fact_stats = fact_stats or payload.get('fact_audit')
    try:
        result = review(reply, current_file=current_file, fact_stats=fact_stats)
    except Exception as e:
        logger.warning('风控初审整体失败: %s', e)
        return payload
    out = dict(payload)
    out['risk_review'] = {
        'verdict': result['verdict'], 'level': result['level'],
        'handoff': result['handoff'],
        'findings': [{'rule': f['rule'], 'label': f['label'], 'level': f['level']}
                     for f in result['findings']],
    }
    block = render_review(result)
    if block:
        out['reply'] = reply.rstrip() + block
    return out
