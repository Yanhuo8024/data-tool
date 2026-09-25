#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hybrid_retrieval.py - 混合检索 + 重排序（方案四 4.1，②）

两阶段：
  阶段1 多路粗召回 + RRF 融合：
     - TF-IDF（可字 n-gram，保留既有无条件兜底）
     - BM25（kw 精确匹配，专治"修正久期""OAS"等术语）
     - 向量（优先 sentence-transformers，缺失时回落零依赖 LSA；见 semantic_index.py）
  阶段2 可选 Cross-Encoder 精排（若已安装）→ Top N 注入 Prompt

所有可选依赖均惰性导入：装了就启用，没装自动降级，绝不报错。
每路都带缓存，避免重复计算。
"""

import hashlib
import logging
import re
import threading
from typing import List, Dict, Any, Optional, Tuple

import semantic_index as _semantic_index   # 零依赖 LSA 语义路（无 sentence-transformers 时兜底）

logger = logging.getLogger(__name__)

# ---------------- 依赖探测（惰性） ----------------
_has_bm25 = False
try:
    from rank_bm25 import BM25Okapi
    _has_bm25 = True
except Exception:
    _has_bm25 = False


class _MiniBM25:
    """rank_bm25 缺失时的最小 BM25 实现（Okai 公式，中文 char+en token）。

    便携环境常无 rank_bm25/sentence_transformers，导致混合检索只剩 TF-IDF 单路，
    向量路 vector_ok=False、术语精确匹配变差。本类提供零依赖 BM25 兜底。
    """

    def __init__(self, corpus_tokens: List[List[str]], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.corpus = corpus_tokens
        self.n = len(corpus_tokens)
        self.doc_len = [len(d) for d in corpus_tokens]
        self.avgdl = (sum(self.doc_len) / self.n) if self.n else 0.0
        self.df: Dict[str, int] = {}
        for doc in corpus_tokens:
            for t in set(doc):
                self.df[t] = self.df.get(t, 0) + 1

    def get_scores(self, query_tokens: List[str]) -> List[float]:
        import math
        scores = [0.0] * self.n
        if not self.avgdl:
            return scores
        for t in query_tokens:
            df = self.df.get(t)
            if not df:
                continue
            idf = math.log(1 + (self.n - df + 0.5) / (df + 0.5))
            for i, doc in enumerate(self.corpus):
                tf = doc.count(t)
                if not tf:
                    continue
                denom = tf + self.k1 * (1 - self.b + self.b * self.doc_len[i] / self.avgdl)
                scores[i] += idf * (tf * (self.k1 + 1)) / denom
        return scores


def _bm25_impl():
    """返回可用的 BM25 构造器：优先 rank_bm25，否则 _MiniBM25。"""
    if _has_bm25:
        return BM25Okapi
    return _MiniBM25

_has_tfidf = False
try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    _has_tfidf = True
except Exception:
    _has_tfidf = False

_has_st = False
_sentence_encoder = None  # True/False/obj；False 表示已确认不可用（不再尝试下载）
try:
    from sentence_transformers import SentenceTransformer
    _has_st = True
except Exception:
    _has_st = False

_has_reranker = False
_cross_encoder = None
try:
    from sentence_transformers import CrossEncoder
    _has_reranker = True
except Exception:
    _has_reranker = False


# ---------------- 文本分词（中文：char + 简单英文 token） ----------------
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[一-龥]")


def _tokenize(text: str) -> List[str]:
    return [t.lower() for t in _TOKEN_RE.findall(str(text))]


# ---------------- BM25 ----------------
def _bm25_scores(query: str, docs: List[str]) -> List[float]:
    corpus = [_tokenize(d) for d in docs]
    bm25 = _bm25_impl()(corpus)
    scores = bm25.get_scores(_tokenize(query))
    return [float(s) for s in scores]


# ---------------- TF-IDF（char n-gram，中文友好） ----------------
_tfidf_index: Dict[str, Any] = {}
_TFIDF_INDEX_CAP = 8          # 只保留最近若干份语料矩阵，防止长跑内存无界增长
_tfidf_lock = threading.Lock()


def _docs_fingerprint(docs: List[str]) -> str:
    """语料内容指纹（md5），用作 TF-IDF 矩阵的缓存键。

    ⚠️ 历史缺陷：旧实现用 ``id(docs)`` 当键。CPython 会复用已回收列表的内存地址
    （实测紧循环新建 2000 个 list 只产生 1 个不同 id），于是「新语料命中旧矩阵」——
    返回的得分条数与 docs 条数不符：少了会被上层 except 静默吞掉（TF-IDF 路
    悄悄消失，只剩 BM25），多了则纯粹错配。修复后键随内容变化，且能真正命中
    缓存（知识库全库 fit_transform ≈ 70ms/次，命中后降到 1ms 级）。
    """
    h = hashlib.md5()
    h.update(str(len(docs)).encode('utf-8'))
    for d in docs:
        h.update(b'\x00')
        h.update(str(d).encode('utf-8', 'ignore'))
    return h.hexdigest()


def _tfidf_scores(query: str, docs: List[str]) -> List[float]:
    key = _docs_fingerprint(docs)
    with _tfidf_lock:
        hit = _tfidf_index.get(key)
        if hit is None:
            vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 3), min_df=1)
            mat = vec.fit_transform(docs)
            if len(_tfidf_index) >= _TFIDF_INDEX_CAP:
                _tfidf_index.pop(next(iter(_tfidf_index)), None)   # FIFO 淘汰
            _tfidf_index[key] = (vec, mat)
        else:
            vec, mat = hit
    q = vec.transform([query])
    return cosine_similarity(q, mat).tolist()[0]


# ---------------- 向量 embedding ----------------
def _read_env(name: str, default: str) -> str:
    import os
    return os.environ.get(name, default)


# 模型缺省值可经环境变量覆盖；RAG_AUTO_DOWNLOAD=1 时允许首次加载联网拉取（默认仅用本地缓存，避免挂死）
_EMBED_MODEL = _read_env("RAG_EMBED_MODEL", "BAAI/bge-small-zh-v1.5")
_RERANKER_MODEL = _read_env("RAG_RERANKER_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2")
_AUTO_DOWNLOAD = _read_env("RAG_AUTO_DOWNLOAD", "0") in ("1", "true", "yes", "on")
_VECTOR_PROBE_DONE = False   # 会话级一次性探测标记


def _model_cached(model_id: str) -> bool:
    """判断模型是否已在本地 HF 缓存（命中才加载，避免联网下载挂死）"""
    import os
    from pathlib import Path
    home = os.environ.get("HF_HOME") or os.path.join(Path.home(), ".cache", "huggingface")
    snapshot = Path(home) / "hub" / ("models--" + model_id.replace("/", "--"))
    return snapshot.exists()


def set_retrieval_config(**kw) -> None:
    """运行期覆盖检索配置。
    kw 支持：embed_model / reranker_model / auto_download。
    用于在冻结后仍可打通向量/精排（如预下载模型目录、内网模型仓库）。"""
    global _EMBED_MODEL, _RERANKER_MODEL, _AUTO_DOWNLOAD, _sentence_encoder, _def_reranker, _VECTOR_PROBE_DONE
    if "embed_model" in kw:
        _EMBED_MODEL = str(kw["embed_model"])
    if "reranker_model" in kw:
        _RERANKER_MODEL = str(kw["reranker_model"])
    if "auto_download" in kw:
        _AUTO_DOWNLOAD = bool(kw["auto_download"])
    # 模型口令/缓存路径发生变化时，重置已缓存句柄，下次检索重新按新配置加载
    _sentence_encoder = None
    _def_reranker = None
    _VECTOR_PROBE_DONE = False


def _load_encoder():
    """安全加载中文 embedding 模型（非阻塞、防挂起）。
    默认仅当模型已本地缓存时才加载；RAG_AUTO_DOWNLOAD=1 时允许联网拉取（带模型仓库覆盖）。
    加载后常驻；失败则 _sentence_encoder=False，本会话不再尝试，向量路自动降级。"""
    global _sentence_encoder, _VECTOR_PROBE_DONE
    if _VECTOR_PROBE_DONE:
        return _sentence_encoder
    _VECTOR_PROBE_DONE = True
    if not _has_st:
        _sentence_encoder = False
        return None
    try:
        if not _model_cached(_EMBED_MODEL) and not _AUTO_DOWNLOAD:
            logger.info("embedding 模型未缓存(%s)，向量路降级（置 RAG_AUTO_DOWNLOAD=1 可自动下载）", _EMBED_MODEL)
            _sentence_encoder = False
            return None
        from sentence_transformers import SentenceTransformer
        _sentence_encoder = SentenceTransformer(_EMBED_MODEL)
    except Exception as e:
        logger.warning("embedding 模型不可用（向量路降级）: %s", e)
        _sentence_encoder = False
    return _sentence_encoder


_emb_cache = {}


def _embed_texts(texts: List[str]) -> Optional[Any]:
    enc = _load_encoder()
    if enc is None:
        return None
    import numpy as np
    key = tuple(texts)
    if key in _emb_cache:
        return _emb_cache[key]
    try:
        emb = enc.encode([str(t) for t in texts], normalize_embeddings=True,
                         batch_size=64, show_progress_bar=False)
        _emb_cache[key] = emb
        return emb
    except Exception as e:
        logger.debug("embedding 计算失败: %s", e)
        return None


def _vec_scores(query: str, docs: List[str]) -> Optional[List[float]]:
    """语义向量路：优先 sentence-transformers，未安装时回落零依赖 LSA。"""
    emb = _embed_texts([query] + docs)
    if emb is None:
        return _semantic_index.scores(query, docs)
    import numpy as np
    q, mat = emb[0], emb[1:]
    return [float(s) for s in (mat @ q).tolist()]


def _vector_impl(ok: bool) -> Optional[str]:
    """当前语义路的实现名（供诊断与对外口径）：预训练模型 / 零依赖 LSA。"""
    if not ok:
        return None
    return "sentence_transformers" if _load_encoder() else _semantic_index.impl_name


# ---------------- RRF 融合 ----------------
def _rrf_merge(ranked_lists: List[List[int]], k: int = 60) -> List[int]:
    """多路排名(每个元素是 doc 下标列表) → RRF 融合后的重排序下标"""
    from collections import defaultdict
    rrf = defaultdict(float)
    for ranks in ranked_lists:
        for rank, doc_idx in enumerate(ranks):
            rrf[doc_idx] += 1.0 / (k + rank + 1)
    return [idx for idx, _ in sorted(rrf.items(), key=lambda x: x[1], reverse=True)]


# ---------------- Cross-Encoder 精排 ----------------
_def_reranker = None


def _load_reranker():
    """安全加载重排模型；默认仅当模型已缓存才加载，RAG_AUTO_DOWNLOAD=1 时允许联网拉取"""
    global _def_reranker
    if _has_reranker and _def_reranker is None:
        if not _model_cached(_RERANKER_MODEL) and not _AUTO_DOWNLOAD:
            logger.info("重排模型未缓存(%s)，跳过精排（置 RAG_AUTO_DOWNLOAD=1 可自动下载）", _RERANKER_MODEL)
            _def_reranker = None
            return None
        try:
            from sentence_transformers import CrossEncoder
            _def_reranker = CrossEncoder(_RERANKER_MODEL)
        except Exception as e:
            logger.warning("加载重排模型失败，跳过精排: %s", e)
            _def_reranker = None
    return _def_reranker


def _rerank(query: str, docs: List[str], candidates: List[int], top_k: int) -> List[int]:
    reranker = _load_reranker()
    if reranker is None or not candidates:
        return candidates[:top_k]
    try:
        pairs = [(str(query), str(docs[i])) for i in candidates]
        scores = reranker.predict(pairs, show_progress_bar=False)
        scored = sorted(zip(candidates, scores), key=lambda x: x[1], reverse=True)
        return [i for i, _ in scored[:top_k]]
    except Exception as e:
        logger.debug("rerank 失败，用融合序: %s", e)
        return candidates[:top_k]


# ---------------- 顶层 API ----------------

def hybrid_search_indices(query: str, docs: List[str], top_k: int = 5) -> List[int]:
    """返回 doc 下标（按相关度降序）。TF-IDF 无条件、BM25/向量可选、RRF 融合、可选精排。"""
    idxs, _ = hybrid_search_with_meta(query, docs, top_k=top_k)
    return idxs


def hybrid_search_with_meta(query: str, docs: List[str], top_k: int = 5):
    """
    比 hybrid_search_indices 多返回一层元信息，供上层做置信度门槛/去重/可观测：
    返回 (indices, meta)；
    meta 形如：
      {
        'rrf': {doc_idx: rrf_score},          # RRF 质子分（降序序依赖，可作置信度代理）
        'paths': ['tfidf', 'bm25', 'vector'],  # 本轮实际参与排序的检索路
        'vector_ok': bool, 'rerank_ok': bool,  # 向量/精排是否真正可用
      }
    """
    n = len(docs)
    empty_meta = {'rrf': {}, 'paths': [], 'vector_ok': False, 'rerank_ok': False}
    if n == 0:
        return [], empty_meta
    ranked_lists: List[List[int]] = []
    paths = []

    # TF-IDF（无条件兜底）
    if _has_tfidf:
        try:
            scores = _tfidf_scores(query, docs)
            ranked_lists.append(sorted(range(n), key=lambda i: scores[i], reverse=True))
            paths.append('tfidf')
        except Exception as e:
            logger.debug("TF-IDF 失败: %s", e)

    # BM25（rank_bm25 或内置 _MiniBM25，恒可用）
    try:
        bs = _bm25_scores(query, docs)
        ranked_lists.append(sorted(range(n), key=lambda i: bs[i], reverse=True))
        paths.append('bm25')
    except Exception as e:
        logger.debug("BM25 失败: %s", e)

    # 向量（可选）
    vector_ok = False
    try:
        vs = _vec_scores(query, docs)
        if vs is not None:
            ranked_lists.append(sorted(range(n), key=lambda i: vs[i], reverse=True))
            paths.append('vector')
            vector_ok = True
    except Exception as e:
        logger.debug("向量检索失败: %s", e)

    if not ranked_lists:
        return list(range(min(top_k, n))), empty_meta

    from collections import defaultdict
    rrf = defaultdict(float)
    for ranks in ranked_lists:
        for rank, doc_idx in enumerate(ranks):
            rrf[doc_idx] += 1.0 / (60 + rank + 1)
    merged = [idx for idx, _ in sorted(rrf.items(), key=lambda x: x[1], reverse=True)]

    # 精排（可选）
    rerank_ok = bool(_load_reranker() is not None)
    if rerank_ok and len(merged) > top_k:
        merged = _rerank(query, docs, merged[:max(top_k * 3, 20)], top_k)

    meta = {
        'rrf': {i: float(rrf.get(i, 0.0)) for i in merged},
        'paths': paths,
        'vector_ok': vector_ok,
        'vector_impl': _vector_impl(vector_ok),
        'rerank_ok': rerank_ok,
    }
    return merged[:top_k], meta


def retrieval_status() -> Dict[str, Any]:
    """返回各检索能力是否可用（供排查/日志）。
    语义向量路有两档实现：sentence-transformers（若已装）或零依赖 LSA（恒可用）。"""
    st_ok = bool(_has_st) and bool(_load_encoder())   # 注意 _load_encoder 三态：encoder/None/False，须用真值判断
    lsa_ok = bool(_semantic_index.status().get("available"))
    return {
        "tfidf": _has_tfidf,
        "bm25": True,  # rank_bm25 或内置 _MiniBM25
        "bm25_impl": "rank_bm25" if _has_bm25 else "mini",
        "vector": st_ok or lsa_ok,
        "vector_impl": "sentence_transformers" if st_ok else (_semantic_index.impl_name if lsa_ok else None),
        "rerank": _has_reranker and _load_reranker() is not None,
    }