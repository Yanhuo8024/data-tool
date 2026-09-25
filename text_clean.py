#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""text_clean.py — 模型输出的文本清洗（思维链标签剥离）

从 `ai_assistant.py` 拆出。拆出的原因有两条，都是工程性的：
① 这段逻辑与助手业务无关，是纯文本工具，混在 3700 行的业务文件里既难复用也难测；
② `ai_assistant.py` 受架构门禁 R1（超长文件棘轮禁止增长）约束，新增出境闸门需要腾出额度。

`ai_assistant` 会原样再导出 `strip_thinking`，因此
`from ai_assistant import strip_thinking` 的既有调用点（路由层、测试）**无需改动**。

被剥离的四类形态（P0-1）：
- 完整的 `<thinking>…</thinking>` 块；
- 未闭合的开标签（流式只到达一半，从标签起截断到结尾）；
- 零散残留的尖括号标签（无配对时直接删除）；
- ```thinking … ``` 代码围栏形式。
"""
import re

# ==================== P0-1：思维链标签统一剥离 ====================
# 模型被要求先在 <thinking> 内做四步拆解再回答；若模型把该块一并输出，
# 前端 formatMD 会把 '<' '>' 转义成可见文本，导致"回答里出现原始标签+推理正文"。
_THINK_BLOCK_RE = re.compile(
    r'<(thinking|think|reasoning|analysis|思考|分析)\b[^>]*>.*?</\1\s*>',
    re.IGNORECASE | re.DOTALL)
# 未闭合的开标签：从该标签起截断到结尾（流式过程可能只到一半）
_THINK_OPEN_RE = re.compile(
    r'<(thinking|think|reasoning|analysis)\b[^>]*>.*\Z',
    re.IGNORECASE | re.DOTALL)
# 独立出现的闭合/尖括号标签（无配对时直接删除）
_THINK_TAG_RE = re.compile(
    r'</?(thinking|think|reasoning)\s*>',
    re.IGNORECASE)
# ```thinking ... ``` 代码围栏形式
_THINK_FENCE_RE = re.compile(
    r'```(?:thinking|think|reasoning)\b.*?```',
    re.IGNORECASE | re.DOTALL)


def strip_thinking(text: str) -> str:
    """剥离模型输出中的思维链标签块（P0-1）。

    处理四类形态：完整的 <thinking>…</thinking>、未闭合的开标签、
    零散的尖括号标签、```thinking 代码围栏。无标签时原样返回，零成本。
    """
    if not text:
        return text
    s = str(text)
    if '<' not in s and '```' not in s and '＜' not in s:
        return strip_meta_chain(s)
    # 全角尖括号归一（部分模型会输出全角）
    s = s.replace('＜', '<').replace('＞', '>')
    s = _THINK_FENCE_RE.sub('', s)
    s = _THINK_BLOCK_RE.sub('', s)
    s = _THINK_OPEN_RE.sub('', s)
    s = _THINK_TAG_RE.sub('', s)
    # 清理因此产生的多余空行
    s = re.sub(r'\n{3,}', '\n\n', s).strip()
    return strip_meta_chain(s)


# ==================== 元思考链剥离（2026-09-22）====================
# 模型偶尔不写 <thinking>，而是直接把「分析用户输入 → 理解上下文 → 确定回复策略
# → 草拟回复」整段推理当正文输出。对用户应只见最终答复。
_META_HEAD_MARKERS = (
    "分析用户输入", "理解上下文", "确定回复策略", "草拟回复",
    "不要暴露思考", "合规要求", "回复内容：",
    "分析请求", "系统提示词约束", "合规与免责（必须自动附上",
    "让我们看看", "为了安全起见", "不要暴露思考的过程",
    "用户输入：", "回复策略",
)


def looks_like_meta_chain(text: str) -> bool:
    head = str(text or "")[:200]
    return sum(1 for m in _META_HEAD_MARKERS if m in head) >= 2


def strip_meta_chain(text: str) -> str:
    """丢掉开头的元思考/系统提示推演，只保留真正答复（无元思考时原样返回）。"""
    if not text:
        return text
    s = str(text)
    if not looks_like_meta_chain(s):
        return s
    # 1) 显式草稿标记之后
    for key in ("草拟回复：", "回复内容：", "直接输出内容", "直接输出："):
        if key in s:
            tail = s.split(key, 1)[1].strip()
            tail = tail.lstrip("0123456789.*-:： \t\n\"'")
            if tail and not looks_like_meta_chain(tail):
                return tail
    # 2) 逐行剔除「系统提示/约束/合规推演」类行
    keep = []
    for ln in s.splitlines():
        st = ln.strip()
        if not st:
            continue
        if any(m in st for m in _META_HEAD_MARKERS):
            continue
        if st.startswith(("*", "-", "•")) and any(
                k in st for k in ("智能助手", "系统", "约束", "要求", "提示词", "合规", "思考", "铺垫", "编造")):
            continue
        if any(st.startswith(p) for p in ("1.", "2.", "3.", "4.", "5.", "6.", "7.")) and any(
                k in st for k in ("分析", "理解", "确定", "草拟", "请求")):
            continue
        if any(k in st for k in ("是否适用", "为了安全", "让我们看看", "等等，", "过于繁重", "标准的免责")):
            continue
        keep.append(ln)
    out = "\n".join(keep).strip()
    return out or s
