#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""term_extract.py - 中文检索词抽取与相关度打分（零依赖 / 零 LLM）

为什么需要本模块
----------------
`rag_unified.HybridRAG` 原来的中文分词只有一行：

    re.findall(r'[\\u4e00-\\u9fff]{2,}', text)

该正则是**贪婪**的：一整段连续中文会被切成一个「词」。于是索引里存的键是
整块 ~300 字语料，而用户提问「这份数据里哪些列是客户收入信息」也是一个整块
字符串——两者永不相等。**中文提问在用户文件 RAG 上几乎必然零命中**，Agent 只能
回答「未检索到相关内容」；只有英文列名（如 revenue）能靠英文分支命中。

本模块提供对称的中文检索词抽取，作为索引侧与查询侧的唯一口径：

- 中文按 2-gram / 3-gram 切分（长度 ≤4 的短串原样保留，如列名「营收」）；
- 过滤含虚词字符（的/了/是/在…）的 gram，压低噪声；
- 英文/数字混合串沿用原行为（`revenue`、`col1`、`2024`）；
- 输出**排序去重**（原实现直接返回 set 转 list，索引内容随进程而变，不可复现）。

刻意**不引入 jieba**：索引侧与查询侧必须同源，引入外部分词器会让「索引已建、
查询词表却变了」这类漂移重新出现。gram 切分对「列名 / 代码 / 指标名」这类
短词为主的检索场景足够，且无需任何依赖。

同时提供 `overlap()` 相关度（查询覆盖度），供长期记忆按主题召回历史结论使用。
"""

import re

# 虚词/功能字：含这些字的 gram 基本无区分度（"的收"、"是在"），直接丢弃。
# 索引侧与查询侧用同一张表，保证两侧切词口径完全一致。
_FUNC_CHARS = frozenset('的了着过吗呢吧啊是在和与及或把被对为以之其也就都还又而但如果'
                        '这那些什么怎么如何是否以及对于关于可以根据进行一个我们你们他们')

_CJK_RUN = re.compile(r'[\u4e00-\u9fff]+')
_EN_WORD = re.compile(r'[a-z]{2,}')
_EN_MIX = re.compile(r'[a-z]+\d+|\d+[a-z]+')

# 单条文本最多产出多少检索词。超长文档按此截断，避免索引体积被单篇撑爆。
DEFAULT_MAX_TERMS = 400


def _ngrams(run: str, n: int) -> list:
    """中文串切分为 n-gram（丢弃含虚词字的 gram）。"""
    return [run[i:i + n] for i in range(len(run) - n + 1)
            if _FUNC_CHARS.isdisjoint(run[i:i + n])]


def extract_terms(text: str, max_terms: int = DEFAULT_MAX_TERMS) -> list:
    """抽取检索词。索引侧与查询侧必须调用同一个函数（对称性即命中保证）。

    - 中文：2-gram + 3-gram；整串长度 ≤4 时整串原样保留
    - 英文：长度 ≥2 的字母串；字母数字混合串
    - 超上限时**优先保留 2-gram**（召回主干），3-gram 按剩余额度补足：
      若按字典序硬截断，像「客户」「风险」这类高频词会被随机丢掉，
      表现为「语料明明有这个词却检不到」。
    - 返回排序去重后的列表（确定性，便于对比与测试）
    """
    if not text:
        return []
    text = str(text)
    low = text.lower()
    terms = set(_EN_WORD.findall(low))
    terms.update(_EN_MIX.findall(low))
    two, three = set(), set()
    for run in _CJK_RUN.findall(text):
        if len(run) <= 4:
            terms.add(run)
        two.update(_ngrams(run, 2))
        three.update(_ngrams(run, 3))
    terms |= two
    room = max_terms - len(terms)
    if room > 0:
        terms |= set(sorted(three)[:room])
    return sorted(terms)


def grams2(text: str) -> set:
    """取 2-gram 集（相关度计算专用：比 3-gram 更稳，召回优先）。"""
    if not text:
        return set()
    out = set()
    for run in _CJK_RUN.findall(str(text)):
        if len(run) == 1:
            out.add(run)
        for i in range(len(run) - 1):
            g = run[i:i + 2]
            if _FUNC_CHARS.isdisjoint(g):
                out.add(g)
    out.update(_EN_WORD.findall(str(text).lower()))
    return out


def overlap(query: str, text: str) -> float:
    """查询词覆盖度 = |查询词 ∩ 文本词| / |查询词|，取值 0~1。

    用于「按当前问题召回相关历史结论」这类场景：分母取查询侧词数，
    保证「短查询命中多」与「长查询命中多」可比。
    """
    q = grams2(query)
    if not q:
        return 0.0
    return len(q & grams2(text)) / float(len(q))
