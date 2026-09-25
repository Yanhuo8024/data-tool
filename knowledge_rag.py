#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
knowledge_rag.py - 知识点库检索模块
数据源：static/knowledge_data.json（由 gen_knowledge.py 生成，与知识点库 tab 同源）
供 AI 助手使用：用户提问时检索相关知识点，注入回答或 agent 上下文。
"""
import os
import re
import json
import logging

logger = logging.getLogger(__name__)

# 知识点 JSON 路径（相对项目根目录）
_KB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "knowledge_data.json")

_kb_cache = None


def load_knowledge() -> list:
    """加载全部知识点，带缓存；文件缺失时返回空列表"""
    global _kb_cache
    if _kb_cache is not None:
        return _kb_cache
    try:
        if not os.path.exists(_KB_PATH):
            logger.warning("knowledge_data.json 不存在: %s", _KB_PATH)
            _kb_cache = []
            return _kb_cache
        with open(_KB_PATH, encoding="utf-8") as f:
            data = json.load(f)
        _kb_cache = data if isinstance(data, list) else []
    except Exception as e:
        logger.error("加载知识点失败: %s", e)
        _kb_cache = []
    return _kb_cache


def _entry_aliases(entry: dict) -> list:
    """提取条目名称及括号内的别名，如 '市盈率(PE)' -> ['市盈率', 'PE']"""
    names = [entry.get("name", "")]
    m = re.search(r"\((.*?)\)\s*$", entry.get("name", ""))
    if m:
        names += [a.strip() for a in m.group(1).split("/") if a.strip()]
    return names


def _keyword_scores(q: str, kb: list) -> dict:
    """关键词精确打分：返回 {条目索引: 分数}"""
    terms = re.findall(r"[a-zA-Z0-9][a-zA-Z0-9.\-]{1,}|[\u4e00-\u9fa5]{2,4}", q)
    scores = {}
    for idx, entry in enumerate(kb):
        name_l = entry.get("name", "").lower()
        hay = " ".join([
            entry.get("name", ""),
            entry.get("desc", ""),
            entry.get("detail", ""),
            " ".join(entry.get("related", []) or []),
            " ".join(entry.get("tips", []) or []),
            " ".join(entry.get("scenarios", []) or []),
        ]).lower()

        score = 0
        for alias in _entry_aliases(entry):
            a = alias.lower()
            if a and a in q:
                score += 60
        for t in terms:
            # 英文/数字词用词边界匹配，避免 'did' 命中 independent 之类子串
            if t.isascii():
                if re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", hay):
                    score += min(len(t), 10)
                    if t in name_l:
                        score += 20
            else:
                if t in hay:
                    score += min(len(t), 10)
                    if t in name_l:
                        score += 20
        if score >= 10:
            scores[idx] = score
    return scores


# ---- 切块（P1-3）：长 detail 切成语义段落，检索/注入在块级，避免整条粗匹配与上下文膨胀 ----
_CHUNK_SIZE = 500
_CHUNK_OVERLAP = 100


def _chunk_text(text: str, size: int = _CHUNK_SIZE, overlap: int = _CHUNK_OVERLAP) -> list:
    """把长文本按固定长度+重叠切成若干段；过短文本原样返回"""
    text = str(text)
    if len(text) <= size:
        return [text] if text else []
    step = max(1, size - overlap)
    return [text[i:i + size] for i in range(0, len(text), step)]


def _entry_blocks(entry: dict) -> list:
    """把条目切成检索块（含名称/描述/别名，保证首块即可命中）。返回块文本列表"""
    head = " ".join(p for p in [
        entry.get("name", ""), entry.get("desc", "") or "",
        " ".join(_entry_aliases(entry)),
    ] if p)
    body = entry.get("detail", "") or ""
    blocks = _chunk_text(body) if body else []
    return [head] + blocks if head else blocks


def _char_jaccard(a: str, b: str) -> float:
    """字符级 Jaccard，用于折叠近重复条目（去噪）"""
    sa, sb = set(str(a)), set(str(b))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


# ---- 查询同义/实体归一（P2-7）：把近义说法补成规范词，提升不同表述的召回 ----
QUERY_SYNONYMS = {
    "估值": "估值 市盈率 PE DCF",
    "回测": "回测 backtest 测试",
    "DID": "DID 双重差分",
    "双重差分": "双重差分 DID",
    "特征工程": "特征工程 特征 变量",
    "因子检验": "因子检验 IC 因子",
    "摊销": "摊销 折旧",
}
_QUERY_SYNONYM_KEYS = sorted(QUERY_SYNONYMS.keys(), key=len, reverse=True)


def _expand_query(q: str) -> str:
    """对查询做同义扩展（追加规范词），供粗召回打分使用；找不到同义则原样返回"""
    expanded = [q]
    for k in _QUERY_SYNONYM_KEYS:
        if k in q:
            expanded.append(QUERY_SYNONYMS[k])
    return " ".join(expanded) if len(expanded) > 1 else q
_tfidf_cache = None  # 仅缓存全局知识点库（load_knowledge）的索引


def _build_tfidf(entries: list):
    """构建 TF-IDF 索引"""
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    docs = [" ".join([
        e.get("name", ""),
        e.get("desc", "") or "",
        e.get("detail", "") or "",
        " ".join(e.get("related", []) or []),
    ]) for e in entries]
    vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 3), min_df=1)
    mat = vec.fit_transform(docs)
    return vec, mat, cosine_similarity


def _tfidf_scores(q: str, kb: list) -> dict:
    """TF-IDF 相似度打分：返回 {条目索引: 分数(0-100)}，语义近义召回（'估值'->'市盈率'）。
    全局知识点库用缓存索引，其他条目列表（如企业知识库）临时构建。"""
    if not kb:
        return {}
    global _tfidf_cache
    if kb is _kb_cache and _tfidf_cache is not None:
        vec, mat, cosine_similarity = _tfidf_cache
    else:
        try:
            vec, mat, cosine_similarity = _build_tfidf(kb)
        except Exception:
            return {}
        if kb is _kb_cache:
            _tfidf_cache = (vec, mat, cosine_similarity)
    try:
        sims = cosine_similarity(vec.transform([q]), mat)[0]
        return {i: round(float(sims[i]) * 100, 2) for i in range(len(kb)) if sims[i] > 0.08}
    except Exception:
        return {}


def search_over_entries(query: str, entries: list, top_k: int = 3) -> list:
    """对任意条目列表检索（关键词优先 + TF-IDF 兜底），返回 [(score, entry)]"""
    if not query or not entries:
        return []
    q = query.lower().strip()
    qc = _clean_query(q)
    target = qc if qc else q
    kw = _keyword_scores(target, entries)
    kw_hits = [(kw[i], entries[i]) for i in kw if kw[i] >= 10]
    kw_hits.sort(key=lambda x: -x[0])
    tf = _tfidf_scores(target, entries)
    tf_hits = [(tf[i], entries[i]) for i in tf if kw.get(i, 0) < 10 and tf[i] >= 8]
    tf_hits.sort(key=lambda x: -x[0])
    return (kw_hits + tf_hits)[:top_k]


# 记录最近一次检索摘要（P2-8 可观测：查询、启用路、向量/精排是否打通、top 命中）
_last_retrieval_summary = {}


def get_last_retrieval_summary() -> dict:
    """返回最近一次检索摘要（供 trace/监控/排查）"""
    return dict(_last_retrieval_summary)


def _relevance_overlap(query: str, entry: dict) -> bool:
    """词面相关性地面：候选条目须与查询有至少一个核心 token（词/数字/别名）重合。
    用于挡住"你好"这类零相关查询误回收，避免命中空造（向量路可用时放宽为语义命中）。"""
    terms = re.findall(r"[a-zA-Z0-9][a-zA-Z0-9.\-]{1,}|[\u4e00-\u9fa5]{2,4}", str(query))
    text = _entry_text(entry).lower()
    for t in terms:
        t = t.lower()
        if t.isascii():
            if re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", text):
                return True
        elif t in text:
            return True
    for alias in _entry_aliases(entry):
        a = alias.lower()
        if a and a in str(query).lower():
            return True
    return False


def rank_entries(query: str, entries: list, top_k: int = 3) -> list:
    """通用块级混合检索 + 置信度门槛 + 近重复去重（P0-2 / P1-3）。
    对任意条目列表返回 [(score, entry)]：
      - 查询先做同义扩展（_expand_query）；
      - 每条目切成检索块，块级走混合检索（TF-IDF/BM25/向量/RRF/可选精排）；
      - 命中弱尾（RRF 显著低于最强命中）被过滤，近重复条目被折叠；
      - 返回条目级结果，附 meta 可观测信息。"""
    global _last_retrieval_summary
    if not query or not entries:
        _last_retrieval_summary = {"query": query, "hits": 0, "paths": [], "vector_ok": False}
        return []
    try:
        from hybrid_retrieval import hybrid_search_with_meta
    except Exception:
        # 混合检索不可用时退回词面 + TF-IDF
        _last_retrieval_summary = {"query": query, "paths": [], "vector_ok": False,
                                   "fallback": True}
        return search_over_entries(_expand_query(query), entries, top_k)

    # 块级展开：doc_text / owner（条目下标）
    doc_texts, owners = [], []
    for ei, e in enumerate(entries):
        for blk in _entry_blocks(e):
            doc_texts.append(blk)
            owners.append(ei)

    q_expanded = _expand_query(query.strip())
    idxs, meta = hybrid_search_with_meta(q_expanded, doc_texts, top_k=top_k * 3)

    # 置信度门槛：丢弃远弱于最强命中的尾部（注入纯度，治"噪声稀释"）
    rrf = meta.get("rrf", {})
    if rrf:
        max_rrf = max(rrf.values())
        idxs = [i for i in idxs if rrf.get(i, 0.0) >= max_rrf * 0.25]

    # 近重复折叠：同一条目取一次，跨条目近重复被跳过
    picked_entries, seen_texts = [], []
    for di in idxs:
        if di < 0 or di >= len(owners):
            continue
        ei = owners[di]
        if ei >= len(entries):
            continue
        ent = entries[ei]
        txt = _entry_text(ent)
        if any(_char_jaccard(txt, s) >= 0.92 for s in seen_texts):
            continue
        seen_texts.append(txt)
        picked_entries.append(ent)
        if len(picked_entries) >= top_k:
            break

    scored = [(round((1.0 / (i + 1)) * 100.0, 2), e)
              for i, e in enumerate(picked_entries)]
    _last_retrieval_summary = {
        "query": query,
        "hits": len(scored),
        "top": [e.get("name", "") for _, e in scored[:3]],
        "paths": meta.get("paths", []),
        "vector_ok": meta.get("vector_ok", False),
        "rerank_ok": meta.get("rerank_ok", False),
    }
    logger.info("RAG 检索[%s] paths=%s vector=%s rerank=%s top=%s",
                query, meta.get("paths", []), meta.get("vector_ok", False),
                meta.get("rerank_ok", False), [e.get("name", "") for _, e in scored[:3]])
    return scored[:top_k]


def format_knowledge_answer(entries: list) -> str:
    """把检索到的知识点格式化为可直接展示的答案（含出处编号，见 kb_cite）。

    实现已迁至 `kb_cite.py`（本文件受架构守卫 R1 行数棘轮约束，不得继续增长），
    此处保留同名转发，`from knowledge_rag import format_knowledge_answer` 零改动。
    """
    from kb_cite import format_knowledge_answer as _impl
    return _impl(entries)


def _entry_level(entry: dict, default: str = "peripheral") -> str:
    """条目权威分级：core=权威核心 / peripheral=外围（用于分层注入，避免外围稀释主导信号）"""
    return "core" if str(entry.get("level", default)).lower() == "core" else "peripheral"


# ---- P2-9 泛化/低信息量条目：检索常因共享通用统计/工具词而霸榜（实测 S 噪声源），
#      注入时降权到备选位；仅当用户明确问它的概念定义时才保留优先 ----
_GENERIC_NAMES = {
    "中心极限定理", "数据分析", "中介效应", "异质性分析", "白噪声", "正态分布",
}


def _is_generic_noise(entry: dict, query: str) -> bool:
    """条目属于泛化噪声，且当前查询并非明确在问它 → True（注入时降权）"""
    if entry.get("name", "") not in _GENERIC_NAMES:
        return False
    q = str(query).lower()
    for alias in _entry_aliases(entry):
        if alias and alias.lower() in q:
            return False  # 用户在明确问这个概念 → 保留
    return True


def get_knowledge_context(query: str, top_k: int = 2) -> str:
    """生成注入 agent system prompt 的紧凑知识点上下文。
    分层注入（P1-5）：多取几条后 core（权威核心）条目优先入选且保底前置，外围条目补位，
    避免外围内容挤占主导信号。"""
    hits = search_knowledge(query, top_k=top_k * 3)
    if not hits:
        return ""
    # core 优先 → 非泛化噪声优先 → 检索原序补位；避免泛化通用条目霸占注入位
    rank = {id(e): i for i, e in enumerate(hits)}
    hits.sort(key=lambda e: (
        0 if _entry_level(e) == "core" else 1,
        1 if _is_generic_noise(e, query) else 0,
        rank.get(id(e), 0)))
    hits = hits[:top_k]
    # 注入侧带引用编号（kb_cite.annotate_context）：让模型能在正文标注出处，
    # 也使回答里的编号可与本次召回集合机械比对（引用核验）。
    from kb_cite import annotate_context
    parts = [annotate_context(hits, max_items=top_k)]
    # 质量回归钩子（P2-6）：把检索-注入的关键足迹交给可选的下游记录器（无依赖时静默跳过）
    try:
        from quality_regression import record_kb_injection
        record_kb_injection(query, [e.get("name", "") for e in hits])
    except Exception:
        pass
    return "\n".join(parts)


QUESTION_WORDS = [
    "什么是", "是什么", "啥是", "何为", "介绍", "解释", "讲讲", "讲下",
    "说明", "科普", "了解下", "怎么用", "如何使用", "如何用", "有什么用",
    "区别", "为什么", "为何", "原理", "含义", "tell me", "what is",
    "what's", "explain", "how to", "what does", "meaning",
    "怎么算", "怎么办", "多少", "划算", "有哪些", "是什么", "多少钱",
    "要交", "交税", "税率", "比例", "缴多少", "免息期",
]

# 查询清洗用噪音词（TF-IDF 前去除，保留核心实体）
_QUERY_NOISE = [
    '怎么算', '怎么办', '怎么', '多少', '多少钱', '划算吗', '划算', '有哪些',
    '是什么', '什么是', '请问', '帮我', '介绍一下', '介绍下', '介绍', '解释一下',
    '解释', '讲讲', '为什么', '为何', '如何', '吗', '呢', '啊', '呀', '要交税吗',
    '要交', '交税', '比例', '税率', '是多少', '什么意思', '含义', '原理', '区别',
    '多少点', '保多少', '缴多少', '免息期', '哪个', '那种', '几种', '几项',
]


def _clean_query(q: str) -> str:
    """去疑问词/噪音，保留核心实体（供 TF-IDF 与关键词匹配）"""
    for w in _QUERY_NOISE:
        q = q.replace(w, ' ')
    q = re.sub(r'[，。？！、；：“”"\'()（）\[\]【】,.!?;:()\-—\s]+', ' ', q)
    return re.sub(r'\s+', ' ', q).strip()


# P1-5 轻量优化：检索结果 TTL 缓存——同一条消息在 ai_assistant 同步/流式与
# is_knowledge_query、agent get_knowledge_context 中会触发 2-3 次相同检索，
# hybrid（TF-IDF+BM25+向量）开销不菲。按 (query, top_k) 缓存 60 秒即可省 2/3。
_SEARCH_CACHE: dict = {}
_SEARCH_CACHE_TTL = 60.0
_SEARCH_CACHE_MAX = 256
_search_cache_lock = None


def _search_cache_get(key):
    import time as _time
    hit = _SEARCH_CACHE.get(key)
    if not hit:
        return None
    ts, val = hit
    if _time.time() - ts > _SEARCH_CACHE_TTL:
        _SEARCH_CACHE.pop(key, None)
        return None
    return val


def _search_cache_put(key, val):
    import time as _time
    if len(_SEARCH_CACHE) >= _SEARCH_CACHE_MAX:
        # 简单淘汰：清掉最早的一半
        for k in sorted(_SEARCH_CACHE, key=lambda k: _SEARCH_CACHE[k][0])[:_SEARCH_CACHE_MAX // 2]:
            _SEARCH_CACHE.pop(k, None)
    _SEARCH_CACHE[key] = (_time.time(), val)


def search_knowledge_scored(query: str, top_k: int = 3) -> list:
    """检索全局知识点库，带分数返回 [(score, entry)]
    优先走混合检索（②：TF-IDF + BM25 + 向量 + RRF + 可选精排），
    任何依赖缺失或失败时回退到词面 + TF-IDF。
    结果带 60s TTL 缓存（P1-5：消除每消息重复检索）。"""
    entries = load_knowledge()
    if not query or not entries:
        return []
    _ck = (str(query), int(top_k))
    cached = _search_cache_get(_ck)
    if cached is not None:
        return cached
    try:
        from hybrid_retrieval import hybrid_search_indices
        docs = [_entry_text(e) for e in entries]
        idxs = hybrid_search_indices(query, docs, top_k=top_k * 2)
        if idxs:
            # 融合词面强命中：混合检索（BM25/向量/RRF）对"含该词的長篇综述条目"
            # 易过度加权——例如查询"什么是DID"会把长篇"平台总览（…）"顶到
            # 专用条目"DID双重差分"之前。此处把词/别名落在条目名称上的强命中
            # （_keyword_scores ≥ 20，即 token 命中 name）前置，保证专用条目
            # 优先于泛化综述条目，同时保留混合检索对其余候选的语义排序。
            qc2 = _clean_query(query.lower().strip()) or query.lower().strip()
            kw = _keyword_scores(qc2, entries)
            order: list = []
            for i in sorted((i for i in kw if kw[i] >= 20), key=lambda x: -kw[x]):
                if i not in order:
                    order.append(i)
            for idx in idxs:
                if idx not in order:
                    order.append(idx)
            # 分数用统一 RRF 权重代理（1/rank）
            scored = [((1.0 / (rank + 1)), entries[idx])
                      for rank, idx in enumerate(order)][:top_k]
            _search_cache_put(_ck, scored)
            return scored
    except Exception as e:
        logger.debug("混合检索回退至词面+TF-IDF: %s", e)
    scored = search_over_entries(query, entries, top_k)
    _search_cache_put(_ck, scored)
    return scored


def _entry_text(entry: dict) -> str:
    """把知识点条目拼成便于检索的文本"""
    parts = [entry.get("name", "")]
    parts.append(entry.get("desc", "") or "")
    parts.append(entry.get("detail", "") or "")
    parts.append(entry.get("formula", "") or "")
    aliases = _entry_aliases(entry)
    parts.append(" ".join(aliases))
    return "\n".join(p for p in parts if p)


def search_knowledge(query: str, top_k: int = 3) -> list:
    """检索相关知识点条目（对外，去掉分数）"""
    return [e for _, e in search_knowledge_scored(query, top_k)]


# 数据分析意图词：命中即走数据分析流程，不抢知识问答（单一真相源，供 is_knowledge_query 与企业库检索复用）
ACTION_WORDS = ['分析', '训练', '预测', '建模', '回测', '画图', '生成图表',
                '清洗', '筛选', '计算', '回归', '聚类', '降维', '跑模型', '拉数据', '做因子']

# 概念性定义提问词：问"什么是X/介绍一下X"这类，即使含"分析"等动作词也是求知概念，应优先知识问答
DEFINITION_WORDS = ('什么是', '是什么', '啥是', '何为', '啥叫', '啥意思',
                    '介绍一下', '介绍下', '解释一下', '解释下', '讲讲', '科普', '科普一下')


def is_knowledge_query(query: str, hits: list = None) -> bool:
    """
    判断是否应按知识点回答：
    - 定义性提问（什么是/介绍一下...）→ 优先知识问答，不被动作词拦截
    - 明确的数据分析意图词（分析/训练/预测...）且非定义提问 → False，走数据分析流程
    - 含提问词（什么是/介绍/怎么用...）且命中知识点
    - 或语义强命中（TF-IDF 相似度 >= 0.35，长句/近义词也能召回）
    - 或查询很短且命中条目名称/别名（"PE"、"DID"）
    """
    q = query.lower().strip()
    if not q:
        return False
    # 数据分析意图词 → 不抢知识问答（但"什么是数据分析"这类定义提问除外）
    if not any(w in q for w in DEFINITION_WORDS):
        if any(w in q for w in ACTION_WORDS):
            return False

    qc = _clean_query(q)
    scored = search_knowledge_scored(q, top_k=3)
    if not scored:
        return False
    top_score = scored[0][0]
    entries = [e for _, e in scored]

    if any(w in q for w in QUESTION_WORDS):
        return True
    # 语义强命中（清洗后核心实体明确命中条目，TF-IDF 相似度高）
    if qc and top_score >= 25:
        return True
    if len(q) <= 24:
        for e in entries:
            for alias in _entry_aliases(e):
                a = alias.lower()
                if not a:
                    continue
                if a in q:
                    if len(a) / max(len(q), 1) >= 0.5:
                        return True
                elif q in a and len(q) >= 3:
                    return True
    return False


if __name__ == "__main__":
    # 简单自测
    for q in ["什么是市盈率", "PE", "DID是什么", "随机森林怎么调参", "如何做特征工程", "分析一下这个数据文件"]:
        hits = search_knowledge(q, top_k=2)
        print(f"\n== {q} -> 命中: {[e['name'] for e in hits]} | is_kb_query: {is_knowledge_query(q, hits)}")
