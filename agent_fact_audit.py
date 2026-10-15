# -*- coding: utf-8 -*-
"""
agent_fact_audit.py — 事实核查 / 数字核验（从 ai_agent 拆出）

职责：
  - 从成功工具返回抽取真实证据
  - 报告数字逐条核对，产出【核验数据】
  - 生成前数字白名单（数据必须真实）

ai_agent 继续 re-export 兼容旧调用点。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_FAIL_CALL_STATUS = {"error", "failed", "fail", "timeout", "exception"}

_NUM_TOKEN_RE = re.compile(
    r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|"
    r"\d+(?:\.\d+)?"
    r"\s*(?:个百分点|万亿元|亿元|万元|亿吨|万吨|万|亿|%|桶|美元|元|倍|点)?"
)


# 三元组核验 + 正文内联标注（单一实现，见 fact_triplet）
from fact_triplet import build_report as _build_verification_report
from fact_triplet import is_noise_token


def _real_tool_evidence(calls) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for c in (calls or []):
        if not isinstance(c, dict):
            text = str(c)
            if text.strip():
                out.append({"tool": "", "text": text, "params": {}})
            continue
        st = str(c.get("status") or "ok").lower()
        if st in _FAIL_CALL_STATUS:
            continue
        text = str(c.get("result") or c.get("result_brief")
                   or c.get("result_preview") or c.get("preview") or "")
        if not text.strip():
            continue
        params = c.get("params") if isinstance(c.get("params"), dict) else {}
        out.append({"tool": str(c.get("tool") or ""), "text": text, "params": params})
    return out


def _is_section_number(tok: str, ctx: str, pos: int) -> bool:
    """章节编号判定（2026-10-10 P0-2 第二次同类 bug）。

    「2.1」「3」「2.2」出现在标题行（#/加粗/编号列表行首）时是**目录编号**，
    不是财务数字。正文行首恰好以编号开头的列表项同样拦截。
    """
    t = str(tok or '').strip()
    bare = t.replace(',', '')
    if not re.fullmatch(r'\d{1,2}(?:\.\d{1,2})?', bare):
        return False
    if not (ctx and 0 <= pos < len(ctx)):
        return False
    ls = ctx.rfind('\n', 0, pos) + 1
    le = ctx.find('\n', pos)
    line = ctx[ls: le if le != -1 else len(ctx)]
    st = line.lstrip()
    if st.startswith('#'):
        st = st.lstrip('#').lstrip()
    elif st.startswith('**'):
        st = st.lstrip('*').lstrip()
    elif st[:1] in ('-', '*', '•'):
        st = st[1:].lstrip()
    if st.startswith(t):
        after = st[len(t): len(t) + 1]
        if after == '' or after in ' \t、．.：:　':
            return True
    return False


def _extract_numeric_tokens(text: str) -> set:
    toks = set()
    body = str(text or "")
    for m in _NUM_TOKEN_RE.finditer(body):
        tok = m.group().strip()
        bare = re.sub(r"\D", "", tok)
        if re.fullmatch(r"(?:19|20)\d{2}", bare):
            continue
        if len(bare) > 12:
            continue
        # 🚨 黑名单（2026-10-09 用户要求）：法规编号（18C、第 X 条）、产品型号
        #（GLM-4、SU7、S800、MA20）、股票代码（600660、000550）不进财务核验
        if is_noise_token(tok, body, m.start()):
            continue
        # 🚨 章节编号（2026-10-10 P0-2 二次修复）：2.1 / 3 等目录编号不进核验
        if _is_section_number(tok, body, m.start()):
            continue
        toks.add(tok)
    return toks


def _tok_variants(tok: str) -> set:
    out = {tok, tok.replace(",", "")}
    bare = re.sub(r"[,\s]", "", tok)
    no_unit = re.sub(r"(?:个百分点|万亿元|亿元|万吨|亿吨|万元|亿|万|桶|美元|元|倍|点|%)+$",
                     "", bare).strip()
    out.add(no_unit)
    try:
        f = float(no_unit)
        out.add(str(int(round(f))))
        if 0 < abs(f) < 1000:
            out.add(f"{f:.1f}")
    except (ValueError, OverflowError):
        pass
    return {v for v in out if v}


def _source_blob_nums(blob_nc: str) -> set:
    nums = set()
    for _m in re.finditer(r"\d+(?:\.\d+)?", blob_nc):
        _s = _m.group()
        nums.add(_s)
        try:
            nums.add(str(int(round(float(_s)))))
        except ValueError:
            pass
    return nums


def _match_in_text(tok: str, text: str, text_nc: str, nums: set) -> bool:
    for v in _tok_variants(tok):
        if v in text or v in text_nc or v in nums:
            return True
    return False


def _audit_fact_sources(final_text, calls) -> tuple:
    """返回 (报告核验说明, stats)。ratio=未核验率。"""
    evidence = _real_tool_evidence(calls)
    parts = []
    for e in evidence:
        parts.append(e.get("tool") or "")
        parts.append(str(e.get("params") or ""))
        parts.append(e.get("text") or "")
    blob = " ".join(parts)
    blob_nc = blob.replace(",", "")
    blob_nums = _source_blob_nums(blob_nc)

    def _find_source(tok: str) -> Optional[str]:
        for e in evidence:
            t = e.get("text") or ""
            t_nc = t.replace(",", "")
            if _match_in_text(tok, t, t_nc, _source_blob_nums(t_nc)):
                return e.get("tool") or "工具返回"
        if _match_in_text(tok, blob, blob_nc, blob_nums):
            return "工具返回"
        return None

    used = _extract_numeric_tokens(final_text)
    verified_data: List[Dict[str, Any]] = []
    unverified_data: List[Dict[str, Any]] = []
    missing: List[str] = []
    for tok in sorted(used):
        src = _find_source(tok)
        if src:
            verified_data.append({"value": tok, "source": src, "real": True})
        else:
            missing.append(tok)
            unverified_data.append({
                "value": tok,
                "real": False,
                "reason": "未在本轮成功工具的真实返回中找到",
            })

    total = len(used)
    stats = {
        "total": total,
        "verified": len(verified_data),
        "unverified": len(missing),
        "ratio": round(len(missing) / total, 2) if total else 0.0,
        "verified_data": verified_data[:40],
        "unverified_data": unverified_data[:20],
        "sources": [e.get("tool") for e in evidence if e.get("tool")],
        "policy": "data_must_be_real",
        "real_data_only": True,
        "evidence_tools": len(evidence),
    }
    if total == 0:
        return "", stats
    # P0-2（2026-10-10）：衍生数字计算链——已核验数字算出的值（如
    # 108% ≈ 57÷52.48，57 = 1418.78×4%）标注「本系统计算」，不再标未核验。
    # 此前 derive_range 无人调用、derived_data 恒为空，此处补齐生产链路。
    # 🚨 必须在 build_report 之前写入 stats['derived_data']，剔除与渲染才生效。
    try:
        from derive_chain import compute as _derive_chain
        _vd = [d.get("value") for d in (stats.get("verified_data") or []) if d.get("value")]
        _uv = [d.get("value") for d in (stats.get("unverified_data") or []) if d.get("value")]
        _chain = _derive_chain(_vd, _uv)
        if _chain:
            stats["derived_data"] = (stats.get("derived_data") or []) + _chain
            stats["derived_chain_hits"] = len(_chain)
            # P0（2026-10-10 第四批）：「本系统计算」与「未核验」互斥——
            # 同一数字不得既进本系统计算节又留在未核验清单。
            _hit = {str(d.get("value_text")).strip() for d in _chain if d.get("value_text")}
            if _hit:
                _keep = [u for u in (stats.get("unverified_data") or [])
                         if str(u.get("value") or "").strip() not in _hit]
                stats["unverified_data"] = _keep
                stats["unverified"] = len(_keep)
                _total = int(stats.get("total") or 0)
                if _total:
                    stats["ratio"] = round(len(_keep) / _total, 2)
    except Exception as e:                       # 衍生推导故障不阻断核验区
        logger.debug("derive_chain 跳过: %s", e)
    rep = _build_verification_report(final_text, stats)
    stats["inline_marked"] = rep["marked"]
    # P0（2026-10-09）：红色审计降级 + 风险评级净化 + 违规清单。
    # 在 report_degrade 内完成；degraded 标记写进 stats 随 fact_audit 上报前端。
    try:
        from report_degrade import apply as _degrade
        _dg = _degrade(final_text, rep, stats)
        rep = dict(rep)
        rep["text"] = _dg["text"]
        if _dg.get("violations_md"):
            rep["note"] = (rep.get("note") or "") + "\n\n" + _dg["violations_md"]
        stats["report_degraded"] = bool(_dg.get("degraded"))
    except Exception as e:                       # 降级链路故障不阻断核验区
        logger.debug("report_degrade 跳过: %s", e)
        stats["report_degraded"] = False
    stats["annotated_text"] = rep["text"]      # 带 ⚠️ 内联标注的正文，调用方取用后应 pop
    return rep["note"], stats


def _verified_number_whitelist(calls) -> str:
    """从成功工具返回抽取数字白名单，供终稿生成前约束「只准写真实数」。"""
    evidence = _real_tool_evidence(calls)
    if not evidence:
        return ""
    nums = set()
    for e in evidence:
        for m in re.finditer(r"\d+(?:\.\d+)?", (e.get("text") or "").replace(",", "")):
            v = m.group()
            if re.fullmatch(r"(?:19|20)\d{2}", v):
                continue
            if len(v) > 12:
                continue
            nums.add(v)
            try:
                nums.add(str(int(round(float(v)))))
            except ValueError:
                pass
    if not nums:
        return ""
    ordered = sorted(nums, key=lambda x: (len(x), x))[:80]
    tools = "、".join(dict.fromkeys(e.get("tool") or "工具" for e in evidence))
    return f"（来源工具：{tools}）" + "、".join(ordered)
