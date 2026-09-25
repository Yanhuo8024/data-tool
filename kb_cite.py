#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kb_cite.py - 知识库引用溯源（可溯源答案）

券商 AI Agent 能力清单要求知识问答「给出可溯源答案」。此前知识库链路只把
`name + desc + detail` 拼进回答，**没有任何出处标识**：
  - 用户无法核对答案依据哪一条；
  - 模型可以自由发挥而不被发现（无编号 → 无法反向核验）。

本模块补齐三个环节：
  1. `cite_id()`   给每条知识条目分配**稳定编号** KB-<分类>-<短哈希>
     （基于条目名，跨会话/跨进程稳定，不像序号会随召回顺序漂移）；
  2. 注入侧 `annotate_context()` / 回答侧 `build_citation_block()`
     在两端都带上编号，使「回答里的引用」与「知识库条目」可机械比对；
  3. `verify_citations()` 反向核验：回答中出现的 KB 编号若不在本次检索集合内，
     判为**编造出处**并在结尾如实标注（不静默通过）。

`format_knowledge_answer` 从 knowledge_rag.py 迁到此处（该文件受行数棘轮约束），
在 knowledge_rag 中原样再导出，`from knowledge_rag import format_knowledge_answer`
的既有调用点零改动。
"""
import hashlib
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 引用编号形如 KB-finance-a3f1
_CITE_RE = re.compile(r"KB-([a-z_]+)-([0-9a-f]{4})", re.IGNORECASE)


def _cat_of(entry: Dict[str, Any]) -> str:
    """条目分类（用于编号可读性）；缺失时归为 misc"""
    cat = str(entry.get("category") or entry.get("cat") or "misc").strip().lower()
    cat = re.sub(r"[^a-z0-9_]", "", cat)
    return cat[:12] or "misc"


def cite_id(entry: Dict[str, Any]) -> str:
    """条目稳定编号：KB-<分类>-<条目名短哈希>。

    用内容哈希而非序号，保证召回顺序变化、知识库增删其它条目时**编号不变**
    ——序号式编号会让历史会话里的引用全部失效，无法追溯。
    """
    if not isinstance(entry, dict):
        return "KB-misc-0000"
    name = str(entry.get("name") or entry.get("title") or "")
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:4]
    return f"KB-{_cat_of(entry)}-{digest}"


def _source_line(entry: Dict[str, Any]) -> str:
    """出处描述：分类 · 所属 tab（知识库条目带 tab 字段，能指到站内页面）"""
    bits = [_cat_of(entry)]
    tab = str(entry.get("tab") or "").strip()
    if tab:
        bits.append(tab)
    return " · ".join(bits)


def build_citation_block(entries: List[Dict[str, Any]],
                         fabricated: Optional[List[str]] = None,
                         max_items: int = 5) -> str:
    """生成回答末尾的「依据」块（Markdown）。无条目时返回空串。"""
    items = [e for e in (entries or []) if isinstance(e, dict)][:max_items]
    if not items:
        return ""
    lines = ["", "---", "#### 📚 依据（本地知识库）"]
    for e in items:
        cid = cite_id(e)
        name = str(e.get("name") or e.get("title") or "未命名")
        desc = str(e.get("desc") or "")[:60]
        lines.append(f"- `[{cid}]` **{name}**（{_source_line(e)}）{desc}")
    if fabricated:
        # 编造出处必须显式暴露：合规场景下"看起来有出处"比"没出处"更危险
        bad = "、".join(f"`{c}`" for c in fabricated[:5])
        lines.append(f"- ⚠️ 上述回答中另有 {len(fabricated)} 处引用编号（{bad}）"
                     f"**不在本次检索到的条目内**，无法核验，请勿采信。")
    lines.append("> 以上依据来自本地知识库条目，可点「知识库」标签页按名称检索原文。")
    return "\n".join(lines)


def verify_citations(text: str, entries: List[Dict[str, Any]]) -> Tuple[List[str], List[str]]:
    """核验回答中的 KB 引用。

    返回 (valid, fabricated)：
      - valid      = 引用了且确在本次检索集合内的编号
      - fabricated = 引用了但集合内不存在的编号（编造出处 / 串号）
    未引用任何编号时两者皆空（不算错，只是没有可核验内容）。
    """
    known = {cite_id(e) for e in (entries or []) if isinstance(e, dict)}
    cited: List[str] = []
    for m in _CITE_RE.finditer(str(text or "")):
        cid = "KB-%s-%s" % (m.group(1).lower(), m.group(2).lower())
        if cid not in cited:
            cited.append(cid)
    known_lc = {c.lower() for c in known}
    valid = [c for c in cited if c.lower() in known_lc]
    fabricated = [c for c in cited if c.lower() not in known_lc]
    return valid, fabricated


def annotate_context(entries: List[Dict[str, Any]], max_items: int = 3) -> str:
    """把条目渲染为**带编号**的注入上下文，供 agent system prompt 使用。

    与旧 `get_knowledge_context` 的差别：每条前置 `[KB-xxx]`，并在末尾给出
    「引用规则」，让模型有能力**在正文里标注出处**（而非只在末尾堆依据）。
    """
    items = [e for e in (entries or []) if isinstance(e, dict)][:max_items]
    if not items:
        return ""
    parts = ["以下为知识点库中与用户问题相关的参考条目（每条带引用编号），"
             "回答时优先基于这些内容，并在引用处标注对应编号："]
    for e in items:
        tag = "[核心] " if str(e.get("level", "")).lower() == "core" else ""
        parts.append(
            f"{tag}[{cite_id(e)}] {e.get('name') or ''}（{_source_line(e)}）："
            f"{e.get('desc', '')}。{e.get('detail', '')}"
        )
    parts.append("引用规则：只允许标注上面列出的编号；不得编造编号或出处。")
    return "\n".join(parts)


def enhance_answer(answer: str, entries: List[Dict[str, Any]],
                   disclaimer: str = "") -> str:
    """给知识库回答补「依据」块 + 引用核验结论（对外统一入口）。

    幂等：已含「📚 依据」块时不再重复追加。
    """
    text = str(answer or "")
    if not text.strip():
        return text
    if "📚 依据" in text:
        return text
    _valid, fabricated = verify_citations(text, entries)
    block = build_citation_block(entries, fabricated=fabricated)
    if disclaimer:
        text = text.rstrip() + disclaimer
    return text.rstrip() + block


def format_knowledge_answer(entries: list) -> str:
    """把检索到的知识点格式化为可直接展示的答案（带出处编号）。

    由 knowledge_rag 迁入；相对原实现新增两处：
      - 标题后标注引用编号，便于用户对照知识库原文；
      - 末尾统一附「📚 依据」块。
    """
    if not entries:
        return ""
    lines = []
    for e in entries:
        lines.append(f"### {e.get('name', '')} `[{cite_id(e)}]`")
        if e.get("desc"):
            lines.append(f"> {e['desc']}")
        if e.get("detail"):
            lines.append(e["detail"])
        if e.get("formula"):
            lines.append(f"- **公式**：{e['formula']}")
        if e.get("params"):
            lines.append("- **参数**：" + "；".join(
                f"{p.get('k', '')}={p.get('v', '')}" for p in e["params"]))
        if e.get("scenarios"):
            lines.append("- **适用场景**：" + "；".join(e["scenarios"]))
        if e.get("pitfalls"):
            lines.append("- **常见踩坑**：" + "；".join(e["pitfalls"]))
        if e.get("tips"):
            lines.append("- **注意事项**：" + "；".join(e["tips"]))
        if e.get("related"):
            lines.append("- **相关概念**：" + "、".join(e["related"]))
        lines.append("")
    body = "\n".join(lines).strip()
    return body + build_citation_block(list(entries))
