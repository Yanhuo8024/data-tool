# -*- coding: utf-8 -*-
"""零依赖语义向量（LSA）与检索接入的回归测试。

覆盖三层：
  1. semantic_index —— 语义区分力、小语料降级、边界输入
  2. hybrid_retrieval —— 向量路接入（LSA 兜底后 paths 含 vector、meta 暴露实现名）
  3. rag_unified —— VectorIndex 无 faiss 时的暴力余弦兜底、HybridRAG 端到端向量打分与整表重建
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import semantic_index as si                                    # noqa: E402
from hybrid_retrieval import hybrid_search_with_meta, retrieval_status   # noqa: E402
from rag_unified import HybridRAG, VectorIndex                 # noqa: E402

# 前两条语义相关，第三条（中心极限定理）为语义无关项——用于验证区分力
DOCS = [
    "久期是债券价格对利率变动的一阶敏感度，衡量利率风险。",
    "凸性是债券价格对利率变化的二阶导，修正久期的误差修正项。",
    "中心极限定理说明大量独立同分布随机变量之和近似正态分布。",
    "收益率曲线反映不同期限国债到期收益率之间的关系。",
]


class TestSemanticIndex:
    def test_relevant_beats_irrelevant(self):
        """语义相关项得分应显著高于无关项（中心极限定理）。"""
        s = si.scores("久期与凸性", DOCS)
        assert s is not None and len(s) == len(DOCS)
        assert max(s[0], s[1]) > s[2] + 0.3

    def test_semantic_recall_across_wording(self):
        """'利率敏感度'与'久期'字面不同，语义路应能识别。"""
        s = si.scores("利率敏感度", DOCS)
        assert s[0] > s[2]

    def test_small_corpus_degrades(self):
        assert si.index(["a", "b"]) is None

    def test_empty_inputs(self):
        assert si.scores("x", []) is None
        assert si.index([]) is None

    def test_status_exposes_impl(self):
        st = si.status()
        assert st["impl"] == "lsa"
        assert st["available"] is True


class TestCosineFallback:
    def test_topk_is_sorted(self):
        vecs = si.index(DOCS)
        ids = ["d%d" % i for i in range(len(DOCS))]
        qv = si.encode(["久期与凸性"])[0]
        got = si.cosine_topk(vecs, ids, qv, 3)
        assert len(got) == 3
        assert got[0][1] >= got[1][1] >= got[2][1]
        assert got[0][0] in ("d0", "d1")

    def test_dimension_mismatch_returns_empty(self):
        """维度不匹配须安全返回空，不得抛异常（原实现无兜底）。"""
        vecs = si.index(DOCS)
        assert si.cosine_topk(vecs, ["a"] * len(DOCS), [0.1, 0.2], 3) == []

    def test_none_matrix(self):
        assert si.cosine_topk(None, ["a"], [0.1], 3) == []


class TestHybridVectorPath:
    def test_vector_path_joins_fusion(self):
        idxs, meta = hybrid_search_with_meta("久期与凸性", DOCS, top_k=3)
        assert "vector" in meta["paths"]
        assert meta["vector_ok"] is True
        assert meta["vector_impl"] == "lsa"
        assert len(idxs) == 3

    def test_status_reports_vector_available(self):
        st = retrieval_status()
        assert st["vector"] is True
        assert st["vector_impl"] in ("lsa", "sentence_transformers")

    def test_irrelevant_pushed_out(self):
        """语义无关项应被挤出前三（此前稳定排第 3）。"""
        idxs, _ = hybrid_search_with_meta("久期与凸性", DOCS, top_k=3)
        assert 2 not in idxs


class TestRagUnifiedVector:
    def test_brute_force_fallback(self):
        """无 faiss 时 search 须走暴力余弦（原实现直接返回空）。"""
        vi = VectorIndex(3)
        vi.add([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], ["a", "b"])
        got = vi.search([1.0, 0.0, 0.0], 2)
        assert got and got[0][0] == "a"
        assert got[0][1] > got[1][1]

    def test_encoder_has_real_impl(self):
        """降级实现须是真语义编码器，不再是字符码点直方图。"""
        rag = HybridRAG(embedding_model="auto", use_faiss=True)
        assert rag.embedding.model_name in ("lsa", "bgem3")
        assert rag.embedding.dimension > 0

    def test_search_uses_vector_scores(self):
        rag = HybridRAG(embedding_model="auto", use_faiss=True)
        for i, t in enumerate(DOCS):
            rag.add_document(t, "doc%d" % i)
        res = rag.search("久期与凸性", top_k=2)
        assert res and res[0].vector_score > 0.5

    def test_delete_rebuilds_vectors(self):
        """删除后语料变化，向量须整表重建以保持与 chunks 对齐。"""
        rag = HybridRAG(embedding_model="auto", use_faiss=True)
        for i, t in enumerate(DOCS):
            rag.add_document(t, "doc%d" % i)
        n0 = len(rag.chunks)
        rag.delete_document(rag.chunks[0].doc_id)
        assert len(rag.chunks) == n0 - 1
        assert rag.vector_index._vectors.shape[0] == n0 - 1

    def test_tiny_corpus_survives(self):
        """语料不足以拟合 LSA 时不得抛异常。"""
        rag = HybridRAG(embedding_model="auto", use_faiss=True)
        rag.add_document("只有一条文档", "single")
        assert rag.search("只有一条", top_k=1)


class TestQueryCleaning:
    def test_strips_interrogative_words(self):
        """问句净化：虚词被滤掉，实词保留。"""
        q = si.clean_query("久期与凸性是什么关系？")
        assert "久期" in q and "凸性" in q

    def test_question_form_does_not_resurrect_noise(self):
        """带疑问词的长问句不得让无关条目回升（修复前实测该噪声升到第 3）。"""
        s = si.scores("久期与凸性是什么关系？", DOCS)
        assert s is not None
        assert max(s[0], s[1]) > s[2] + 0.3

    def test_fallback_keeps_raw(self):
        """净化结果为空时保留原串，不得返回空串。"""
        assert si.clean_query("") == ""
