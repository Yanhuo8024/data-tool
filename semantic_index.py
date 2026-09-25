# -*- coding: utf-8 -*-
"""零依赖语义向量索引（LSA / 潜在语义分析）。

背景：本仓库便携环境未安装 sentence-transformers / torch / faiss，
`rag_unified.EmbeddingEngine` 的降级实现只是 `ord(c) % 256` 字符码点直方图
（无语义能力），`VectorIndex.search` 在无 faiss 时恒返回空。本模块用环境内
**已有的 sklearn** 提供真·稠密语义向量：

    TF-IDF(char_wb 2-3gram) --TruncatedSVD--> 稠密向量 --L2--> 余弦可比

语义能力弱于 bge 类预训练模型，但远强于字符直方图，且零新增依赖。

接口（模块级，带内容指纹缓存 + 锁）：
    impl_name                        -> 'lsa'
    index(corpus)                    -> Optional[np.ndarray]  拟合语料，返回归一化向量矩阵
    encode(texts)                    -> Optional[List[List[float]]]  在最近一次拟合的基上编码
    scores(query, docs)              -> Optional[List[float]]  一步式：拟合 docs 后算 query 余弦
    cosine_topk(matrix, ids, vec, k) -> List[Tuple[str, float]]  无 faiss 时的暴力兜底
    status()                         -> Dict[str, Any]

任一环节失败均返回 None/空（调用方据此降级），不抛异常打断检索主流程。
"""

import hashlib
import logging
import threading
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

impl_name = "lsa"
_DIM = 256          # 目标稠密维度（实际取 min(_DIM, n_docs-1, n_features-1)）
_MAX_CACHE = 8      # 语料指纹缓存上限（FIFO 淘汰），与 hybrid_retrieval 同策略
_MIN_CORPUS = 3     # 少于该条数时 SVD 退化，直接降级

_lock = threading.Lock()
_cache: Dict[str, Dict[str, Any]] = {}
_active_key: Optional[str] = None
_last_error: Optional[str] = None


def _fingerprint(docs: List[str]) -> str:
    """内容指纹（长度 + 内容）。禁用 id() —— CPython 会复用已回收对象的地址。"""
    h = hashlib.md5()
    for d in docs:
        s = str(d)
        h.update(str(len(s)).encode("utf-8"))
        h.update(s.encode("utf-8", "replace"))
    return h.hexdigest()


def _l2(mat: Any) -> Any:
    """按行 L2 归一化（零向量保持零，避免除零）。"""
    import numpy as np
    norm = np.linalg.norm(mat, axis=1, keepdims=True)
    norm[norm == 0] = 1.0
    return mat / norm


def _fit(docs: List[str]) -> Optional[Dict[str, Any]]:
    """在语料上拟合 TF-IDF + SVD，返回 {'model','svd','vecs','dim'}；失败返回 None。"""
    global _last_error
    if len(docs) < _MIN_CORPUS:
        _last_error = "语料过少(%d < %d)，LSA 不启用" % (len(docs), _MIN_CORPUS)
        return None
    try:
        from sklearn.decomposition import TruncatedSVD
        from sklearn.feature_extraction.text import TfidfVectorizer
        model = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 3), min_df=1)
        mat = model.fit_transform([str(d) for d in docs])
        k = max(2, min(_DIM, len(docs) - 1, mat.shape[1] - 1))
        svd = TruncatedSVD(n_components=k, random_state=0)
        vecs = _l2(svd.fit_transform(mat))
        _last_error = None
        return {"model": model, "svd": svd, "vecs": vecs, "dim": k}
    except Exception as e:
        _last_error = "%s: %s" % (type(e).__name__, e)
        logger.debug("LSA 拟合失败，语义路降级: %s", e)
        return None


def _get_or_fit(docs: List[str]) -> Optional[Dict[str, Any]]:
    """按内容指纹取缓存；未命中则拟合并写入（FIFO 淘汰）。"""
    global _active_key
    key = _fingerprint(docs)
    with _lock:
        entry = _cache.get(key)
        if entry is None:
            entry = _fit(docs)
            if entry is None:
                return None
            if len(_cache) >= _MAX_CACHE:
                _cache.pop(next(iter(_cache)), None)
            _cache[key] = entry
        _active_key = key
    return entry


def index(corpus: List[str]) -> Optional[Any]:
    """拟合语料并返回 L2 归一化后的稠密向量矩阵（n_docs × dim）；不可用返回 None。"""
    docs = [str(c) for c in (corpus or [])]
    if not docs:
        return None
    entry = _get_or_fit(docs)
    return None if entry is None else entry["vecs"]


def encode(texts: List[str]) -> Optional[List[List[float]]]:
    """在最近一次 index() 拟合的基上编码 texts（不重新拟合）；无可用基返回 None。"""
    with _lock:
        entry = _cache.get(_active_key) if _active_key else None
    if entry is None or not texts:
        return None
    try:
        mat = entry["model"].transform([str(t) for t in texts])
        dense = _l2(entry["svd"].transform(mat))
        return [row.tolist() for row in dense]
    except Exception as e:
        logger.debug("LSA 编码失败，语义路降级: %s", e)
        return None


def clean_query(query: str) -> str:
    """查询净化：复用 term_extract 过滤中文虚词（与索引侧同源）。

    问句形式（如"久期与凸性是什么关系？"）的虚词 n-gram 会稀释查询向量，
    使无关条目的余弦相对回升（实测"中心极限定理"会升到第 3 位）——
    净化为实词串即可消除该退化。过滤为空或依赖缺失时保留原串。
    """
    raw = str(query)
    try:
        from term_extract import extract_terms
        terms = [t for t in extract_terms(raw) if t]
        return " ".join(terms) if terms else raw
    except Exception as e:
        logger.debug("查询净化不可用，使用原串: %s", e)
        return raw


def scores(query: str, docs: List[str]) -> Optional[List[float]]:
    """一步式：在 docs 上拟合（命中缓存则复用），返回 query 对每条 doc 的余弦分。

    query 不参与拟合，故不污染语义空间；打分前先做虚词净化（见 clean_query）。
    """
    ds = [str(d) for d in (docs or [])]
    if not ds:
        return None
    entry = _get_or_fit(ds)
    if entry is None:
        return None
    try:
        qv = _l2(entry["svd"].transform(entry["model"].transform([clean_query(query)])))[0]
        return [float(x) for x in (entry["vecs"] @ qv)]
    except Exception as e:
        logger.debug("LSA 打分失败，语义路降级: %s", e)
        return None


def cosine_topk(matrix: Any, ids: List[str], query_vec: Any, top_k: int) -> List[Tuple[str, float]]:
    """无 faiss 时的暴力余弦兜底：matrix 已 L2 归一化，点积即余弦。"""
    import numpy as np
    if matrix is None or not ids or query_vec is None:
        return []
    m = np.asarray(matrix, dtype="float32")
    q = np.asarray(query_vec, dtype="float32").ravel()
    if m.ndim != 2 or m.shape[0] == 0 or q.shape[0] != m.shape[1]:
        return []
    sims = (m @ q).tolist()
    order = sorted(range(len(sims)), key=lambda i: sims[i], reverse=True)[:max(1, int(top_k))]
    return [(ids[i], float(sims[i])) for i in order if i < len(ids)]


def status() -> Dict[str, Any]:
    """诊断用：当前实现、缓存规模与最近一次失败原因。"""
    return {
        "impl": impl_name,
        "available": True,
        "dim_target": _DIM,
        "cached_corpora": len(_cache),
        "active": bool(_active_key),
        "last_error": _last_error,
    }
