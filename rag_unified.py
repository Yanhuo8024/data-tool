#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rag_unified.py - 统一 RAG 引擎

合并自: rag_knowledge.py + rag_hybrid.py + rag_enified.py
功能:
- PDF/文本解析 + 智能分块 (统一实现)
- 本地 Embedding (BGE-M3 若已装 / 零依赖 LSA 兜底, 见 semantic_index.py) + 向量索引 (faiss / 暴力余弦兜底)
- BM25 关键词检索 + 混合检索 (关键词 + 向量 + 业务重排序)
- 数据标准化 (品牌名/别名统一)
- LLM 生成回答 (带引用来源)

零费用: 纯本地运行, 无需 API Key
"""

import re
import math
import json
import time
import hashlib
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field

from term_extract import extract_terms  # 中文检索词抽取（索引/查询同源，见该模块说明）
import semantic_index as _semantic_index  # 零依赖 LSA 语义向量（无 sentence-transformers/faiss 时兜底）


# ==========================================================
# 配置
# ==========================================================

KNOWLEDGE_DIR = Path("output/knowledge")
UPLOAD_DIR = KNOWLEDGE_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


# ==========================================================
# 数据结构
# ==========================================================

@dataclass
class Chunk:
    """文档分块"""
    id: str
    doc_id: str
    doc_name: str
    text: str
    start_pos: int = 0
    end_pos: int = 0
    chunk_index: int = 0
    doc_type: str = "generic"
    created_at: float = 0.0
    relevance_fields: List[str] = field(default_factory=list)


@dataclass
class SearchResult:
    """搜索结果"""
    chunk: Chunk
    final_score: float = 0.0
    keyword_score: float = 0.0
    vector_score: float = 0.0
    business_score: float = 0.0
    rank: int = 0


# ==========================================================
# 文本分块
# ==========================================================

def chunk_text(text: str, chunk_size: int = 300, overlap: int = 50) -> List[Tuple[str, int, int]]:
    """统一文本分块实现：按空行分段打包，**超长单段按窗口硬切**。

    原实现对超长单段整段吞下（无空行的长文 / JSON / 单行长句会变成 1 个巨型块），
    检索粒度、词表覆盖与片段展示都会失真；这里保证任一块不超过 chunk_size。
    """
    chunks: List[Tuple[str, int, int]] = []
    step = max(1, chunk_size - overlap)
    pos = 0
    buf = ""
    for para in text.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        for piece in [para[i:i + chunk_size] for i in range(0, len(para), step)]:
            if buf and len(buf) + len(piece) + 2 > chunk_size:
                chunks.append((buf.strip(), pos, pos + len(buf)))
                pos += len(buf)
                tail = buf[-overlap:] if overlap > 0 else ""
                buf = tail if len(tail) + len(piece) + 2 <= chunk_size else ""
            buf += piece + "\n\n"
    if buf.strip():
        chunks.append((buf.strip(), pos, pos + len(buf)))
    return chunks


# ==========================================================
# Embedding 引擎
# ==========================================================

class EmbeddingEngine:
    """本地语义编码器：优先 sentence-transformers，否则零依赖 LSA（semantic_index）。"""

    def __init__(self, model_name: str = "auto"):
        self.model = None
        self.model_name = model_name
        self.dimension = 256
        self._load_model()

    def _load_model(self):
        if self.model_name != "auto":
            self.model_name = "lsa"
            return
        try:
            from sentence_transformers import SentenceTransformer
            self.model = SentenceTransformer("BAAI/bge-m3")
            self.dimension = self.model.get_sentence_embedding_dimension()
            self.model_name = "bgem3"
        except Exception:
            self.model = None
            self.model_name = "lsa"   # ImportError 或 torch DLL 初始化失败 → 回落零依赖 LSA

    def fit(self, corpus: List[str]) -> bool:
        """在语料上拟合 LSA 基（语料变化须重拟合）；预训练模型模式无需拟合。"""
        return True if self.model else (_semantic_index.index(corpus) is not None)

    def encode(self, texts: List[str]) -> List[List[float]]:
        if self.model:
            try:
                return self.model.encode(texts, show_progress_bar=False, normalize_embeddings=True).tolist()
            except Exception:
                self.model = None   # 预训练编码异常 → 永久回落 LSA
        vecs = _semantic_index.encode(texts)
        if not vecs:
            return [[0.0] * self.dimension for _ in texts]
        self.dimension = len(vecs[0])
        return vecs

    def encode_query(self, query: str) -> List[float]:
        if self.model_name == "bgem3" and self.model:
            return self.encode([f"Represent this sentence for searching relevant passages: {query}"])[0]
        return self.encode([_semantic_index.clean_query(query)])[0]


# ==========================================================
# FAISS 向量索引
# ==========================================================

class VectorIndex:
    """FAISS 向量索引封装"""
    
    def __init__(self, dimension: int, index_type: str = "flat"):
        self.dimension = dimension
        self.index_type = index_type
        self.index = None
        self.chunk_ids: List[str] = []
        self._vectors = None       # 原始向量矩阵：无 faiss 时的暴力余弦兜底数据源
        self.is_built = False
        self._build()
    
    def _build(self):
        try:
            import faiss
            if self.index_type == "flat":
                self.index = faiss.IndexFlatIP(self.dimension)
            elif self.index_type == "hnsw":
                self.index = faiss.IndexHNSWFlat(self.dimension, 32)
            print(f"[FAISS] {self.index_type} index built")
        except ImportError:
            self.index = None
            print("[FAISS] faiss unavailable, using brute force")
    
    def add(self, vectors: List[List[float]], chunk_ids: List[str], reset: bool = False):
        """添加向量；reset=True 表示语料已变化需整表重建（LSA 换基后旧向量不可比）。"""
        if not vectors:
            return
        import numpy as np
        matrix = np.array(vectors, dtype='float32')
        if reset:
            self.chunk_ids = []
            self._build()          # faiss 索引无法原地清空，只能重建
        self._vectors = matrix if (reset or self._vectors is None) else np.vstack([self._vectors, matrix])
        if self.index:
            self.index.add(matrix)
        self.chunk_ids.extend(chunk_ids)
        self.is_built = True
    
    def search(self, query_vec: List[float], top_k: int) -> List[Tuple[str, float]]:
        if not self.is_built:
            return []
        if self.index:
            import numpy as np
            query = np.array([query_vec], dtype='float32')
            scores, indices = self.index.search(query, top_k)
            return [(self.chunk_ids[idx], float(s)) for s, idx in zip(scores[0], indices[0]) if idx >= 0]
        # 无 faiss：暴力余弦兜底（此前直接返回空 → 向量路静默失效）
        return _semantic_index.cosine_topk(self._vectors, self.chunk_ids, query_vec, top_k)


# ==========================================================
# 数据标准化
# ==========================================================

class DataNormalizer:
    """数据标准化处理器"""
    
    BRAND_MAP = {
        "apple": "Apple", "苹果": "Apple", "iphone": "iPhone",
        "huawei": "华为", "华为": "华为", "xiaomi": "小米", "小米": "小米",
        "samsung": "三星", "三星": "三星", "oppo": "OPPO", "vivo": "vivo",
        "byd": "比亚迪", "比亚迪": "比亚迪", "tesla": "特斯拉", "特斯拉": "特斯拉",
        "catl": "宁德时代", "宁德时代": "宁德时代",
    }
    
    INDUSTRY_MAP = {
        "gmv": "商品交易总额", "roi": "投资回报率",
        "dau": "日活跃用户", "mau": "月活跃用户",
        "cvr": "转化率", "aov": "平均订单价值",
        "yoy": "同比", "mom": "环比",
    }
    
    @classmethod
    def normalize(cls, text: str) -> str:
        if not text:
            return text
        
        text = cls._normalize_brands(text)
        text = cls._normalize_industry_terms(text)
        return text
    
    @classmethod
    def _normalize_brands(cls, text: str) -> str:
        words = text.split()
        return ' '.join(cls.BRAND_MAP.get(w.lower(), w) for w in words)
    
    @classmethod
    def _normalize_industry_terms(cls, text: str) -> str:
        for term, full in cls.INDUSTRY_MAP.items():
            text = re.sub(rf'\b{term}\b', full, text, flags=re.IGNORECASE)
        return text
    
    @classmethod
    def extract_entities(cls, text: str) -> Dict:
        return {
            "brands": [b for k, b in cls.BRAND_MAP.items() if k in text.lower()],
            "terms": [t for k, t in cls.INDUSTRY_MAP.items() if k in text.lower()],
        }


# ==========================================================
# 查询缓存
# ==========================================================

class QueryCache:
    """查询结果缓存"""
    
    def __init__(self, ttl: int = 300, max_size: int = 50):
        self.ttl = ttl
        self.max_size = max_size
        self._cache: Dict[str, Tuple[float, List[SearchResult]]] = {}
    
    def get(self, query: str) -> Optional[List[SearchResult]]:
        if query in self._cache:
            ts, results = self._cache[query]
            if time.time() - ts < self.ttl:
                return results
            del self._cache[query]
        return None
    
    def put(self, query: str, results: List[SearchResult]):
        if len(self._cache) >= self.max_size:
            # Remove oldest
            oldest = min(self._cache, key=lambda k: self._cache[k][0])
            del self._cache[oldest]
        self._cache[query] = (time.time(), results)


# ==========================================================
# 文档解析
# ==========================================================

def extract_pdf_text(pdf_path: str) -> Tuple[str, int, List[str]]:
    """提取 PDF 文本"""
    try:
        import fitz
        doc = fitz.open(pdf_path)
        page_count = len(doc)
        page_texts = [doc[i].get_text() for i in range(page_count)]
        doc.close()
        return "\n\n".join(page_texts), page_count, page_texts
    except ImportError:
        pass
    
    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(pdf_path)
        page_count = len(reader.pages)
        page_texts = [page.extract_text() or "" for page in reader.pages]
        return "\n\n".join(page_texts), page_count, page_texts
    except ImportError:
        raise ImportError("Need PyMuPDF or PyPDF2")


# ==========================================================
# 统一 RAG 引擎
# ==========================================================

class HybridRAG:
    """
    统一 RAG 检索引擎
    
    整合关键词 BM25 + 向量语义 + 业务重排序
    """
    
    def __init__(self, embedding_model: str = "auto", use_faiss: bool = True):
        self.embedding = EmbeddingEngine(embedding_model)
        self.vector_index = VectorIndex(self.embedding.dimension) if use_faiss else None
        self.normalizer = DataNormalizer()
        self.cache = QueryCache()
        
        # 数据
        self.chunks: List[Chunk] = []
        self.chunk_map: Dict[str, Chunk] = {}
        
        # 关键词索引
        self.keyword_index: Dict[str, List[str]] = {}
        
        # 权重配置
        self.keyword_weight = 0.35
        self.vector_weight = 0.45
        self.business_weight = 0.15
        self.freshness_weight = 0.05
    
    def add_document(self, text: str, doc_name: str, doc_type: str = "generic") -> str:
        """添加文档"""
        doc_id = hashlib.md5(f"{doc_name}{time.time()}".encode()).hexdigest()[:12]
        
        # 标准化
        normalized = self.normalizer.normalize(text)
        
        # 分块
        text_chunks = chunk_text(normalized, 300, 50)
        
        # 创建 chunks
        chunks = []
        for i, (chunk_text_val, start, end) in enumerate(text_chunks):
            chunk = Chunk(
                id=f"{doc_id}_{i}",
                doc_id=doc_id,
                doc_name=doc_name,
                text=chunk_text_val,
                start_pos=start,
                end_pos=end,
                chunk_index=i,
                doc_type=doc_type,
                created_at=time.time(),
            )
            chunks.append(chunk)
            self.chunks.append(chunk)
            self.chunk_map[chunk.id] = chunk
        
        # 向量化（语料变化须整表重建：LSA 换基后旧向量不可比）
        self._rebuild_vectors()
        
        # 关键词索引
        for chunk in chunks:
            terms = self._extract_terms(chunk.text)
            for term in terms:
                self.keyword_index.setdefault(term, []).append(chunk.id)
        
        self.clear_cache()
        return doc_id

    def _rebuild_vectors(self):
        """语料变化后整表重建向量：先在全量语料上拟合，再编码并整表写入。"""
        corpus = [c.text for c in self.chunks]
        if not self.vector_index or not corpus or not self.embedding.fit(corpus):
            return
        self.vector_index.add(self.embedding.encode(corpus), [c.id for c in self.chunks], reset=True)
    
    def add_pdf(self, pdf_path: str, doc_name: str = "") -> str:
        """添加 PDF 文档"""
        text, _, _ = extract_pdf_text(pdf_path)
        return self.add_document(text, doc_name or Path(pdf_path).name, "pdf")
    
    def search(self, query: str, top_k: int = 5) -> List[SearchResult]:
        """混合检索"""
        # 检查缓存
        cached = self.cache.get(query)
        if cached:
            return cached
        
        # 标准化查询
        query = self.normalizer.normalize(query)
        
        # 双路召回
        keyword_scores = self._keyword_search(query, top_k * 3)
        vector_scores = self._vector_search(query, top_k * 3) if self.vector_index else {}
        
        # 合并去重
        all_ids = set(keyword_scores.keys()) | set(vector_scores.keys())
        
        results = []
        for chunk_id in all_ids:
            chunk = self.chunk_map.get(chunk_id)
            if not chunk:
                continue
            
            kw_score = keyword_scores.get(chunk_id, 0)
            vec_score = vector_scores.get(chunk_id, 0)
            biz_score = self._calc_business_score(chunk, query)
            fresh_score = self._calc_freshness_score(chunk)
            
            final = (
                self.keyword_weight * kw_score +
                self.vector_weight * vec_score +
                self.business_weight * biz_score +
                self.freshness_weight * fresh_score
            )
            
            results.append(SearchResult(
                chunk=chunk,
                final_score=final,
                keyword_score=kw_score,
                vector_score=vec_score,
                business_score=biz_score,
            ))
        
        results.sort(key=lambda x: x.final_score, reverse=True)
        for i, r in enumerate(results):
            r.rank = i + 1
        
        self.cache.put(query, results[:top_k])
        return results[:top_k]
    
    def query(self, prompt: str, llm_function=None, top_k: int = 5) -> Dict:
        """
        完整 RAG 查询（检索 + 生成回答）
        Args:
            prompt: 用户问题
            llm_function: LLM 调用函数 (context, prompt) -> answer
            top_k: 检索条数上限（默认 5）
        
        Returns:
            {"answer": str, "sources": [...], "chunks": [...]}
        """
        results = self.search(prompt, top_k=top_k)
        
        # 组装上下文
        context_parts = []
        sources = []
        for r in results:
            context_parts.append(f"[{r.chunk.doc_name}]\n{r.chunk.text}")
            sources.append({
                "doc_name": r.chunk.doc_name,
                "score": round(r.final_score, 4),
                "snippet": r.chunk.text[:150]
            })
        
        context = "\n\n".join(context_parts)
        
        # 生成回答
        if llm_function:
            answer = llm_function(context, prompt)
        else:
            answer = f"已检索到 {len(results)} 条与「{prompt}」相关的片段。"
        
        return {
            "answer": answer,
            "sources": sources,
            "chunks": [
                {"text": r.chunk.text, "doc": r.chunk.doc_name, "score": round(r.final_score, 4)}
                for r in results
            ]
        }
    
    def _keyword_search(self, query: str, top_k: int) -> Dict[str, float]:
        """BM25 关键词检索"""
        terms = self._extract_terms(query)
        chunk_scores = {}
        
        for term in terms:
            if term in self.keyword_index:
                for chunk_id in self.keyword_index[term]:
                    chunk_scores[chunk_id] = chunk_scores.get(chunk_id, 0) + 1.0
        
        if chunk_scores:
            max_score = max(chunk_scores.values())
            chunk_scores = {k: v/max_score for k, v in chunk_scores.items()}
        
        return dict(sorted(chunk_scores.items(), key=lambda x: x[1], reverse=True)[:top_k])
    
    def _vector_search(self, query: str, top_k: int) -> Dict[str, float]:
        """向量语义检索"""
        if not self.vector_index:
            return {}
        
        query_vec = self.embedding.encode_query(query)
        results = self.vector_index.search(query_vec, top_k)
        return {chunk_id: score for chunk_id, score in results}
    
    def _calc_business_score(self, chunk: Chunk, query: str) -> float:
        """业务相关性评分"""
        score = 0.0
        text_lower = chunk.text.lower()
        
        for term in self._extract_terms(query):
            if term in text_lower:
                score += 1.0
        
        # 文档类型加分
        type_bonus = {"financial": 2.0, "report": 1.5, "product": 1.2, "generic": 0}
        score += type_bonus.get(chunk.doc_type, 0)
        
        return min(score / 5.0, 1.0)
    
    def _calc_freshness_score(self, chunk: Chunk) -> float:
        """新鲜度评分"""
        days_ago = (time.time() - chunk.created_at) / 86400
        if days_ago <= 7:
            return 1.0
        elif days_ago <= 30:
            return 0.8
        elif days_ago <= 90:
            return 0.6
        elif days_ago <= 365:
            return 0.3
        return 0.1
    
    @staticmethod
    def _extract_terms(text: str) -> List[str]:
        """提取检索词。委托 term_extract.extract_terms —— 索引侧与查询侧同一口径。

        原实现用贪婪正则 `[\\u4e00-\\u9fff]{2,}` 取中文，会把整段连续中文当成
        一个词，导致中文提问与索引键永不相等（中文查询几乎必然零命中）。
        """
        return extract_terms(text)
    
    def clear_cache(self):
        """清空缓存"""
        self.cache._cache.clear()
    
    def list_documents(self) -> List[Dict]:
        """列出所有文档"""
        docs = {}
        for chunk in self.chunks:
            if chunk.doc_id not in docs:
                docs[chunk.doc_id] = {
                    "id": chunk.doc_id,
                    "name": chunk.doc_name,
                    "chunks": 0
                }
            docs[chunk.doc_id]["chunks"] += 1
        return list(docs.values())
    
    def delete_document(self, doc_id: str):
        """删除文档"""
        self.chunks = [c for c in self.chunks if c.doc_id != doc_id]
        self.chunk_map = {k: v for k, v in self.chunk_map.items() if v.doc_id != doc_id}
        # 重建索引
        self.keyword_index = {}
        for chunk in self.chunks:
            terms = self._extract_terms(chunk.text)
            for term in terms:
                self.keyword_index.setdefault(term, []).append(chunk.id)
        self._rebuild_vectors()
        self.clear_cache()


# ==============================================
# 全局实例
# ==============================================

rag_engine = HybridRAG(embedding_model="auto", use_faiss=True)  # 无 faiss 时由 semantic_index 暴力余弦兜底


# ==============================================
# 使用示例
# ==============================================

def demo():
    """统一 RAG 引擎 Demo"""
    rag = HybridRAG(embedding_model="tfidf", use_faiss=False)
    
    # 添加文档
    docs = [
        ("iPhone 15 Pro 2024年销售额达 1200亿美元，同比增长 8%。", "Apple_Report"),
        ("宁德时代2024年营收增长 22%，净利润增长 18%。", "CATL_Report"),
        ("比亚迪 2024年新能源汽车销量突破 302万辆。", "BYD_Report"),
        ("华为Mate 60 Pro 搭载麒麟 9000S 芯片，销量增长 35%。", "Huawei_Report"),
    ]
    
    for text, name in docs:
        rag.add_document(text, name)
    
    # 检索
    queries = ["iPhone 销售额", "比亚迪 销量", "华为 增长"]
    for q in queries:
        results = rag.search(q, top_k=2)
        print(f"\nQuery: {q}")
        for r in results:
            print(f"  [{r.final_score:.3f}] {r.chunk.doc_name}: {r.chunk.text[:50]}")
    
    # 完整查询
    rag_result = rag.query("iPhone 2024年销量如何？")
    print(f"\nAnswer: {rag_result['answer']}")
    print(f"Sources: {len(rag_result['sources'])}")


if __name__ == "__main__":
    demo()
