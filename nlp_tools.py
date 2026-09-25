#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""  nlp.tools    / Jieba  /  / TF-IDF """
import re
import pandas as pd
import numpy as np
from io import BytesIO
import base64
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from collections import Counter

plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'Arial Unicode MS', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False

#  
CN_STOPWORDS = set(['','','','','','','','','','','','','',
    '','','','','','','','','','','','','','',
    '','','','','','','','','','','','','','','','',
    '','','','','','','','','','','','','','','','',
    '','','','','','','','','','','','','','','','',
    '','','','','','','','','','','','','','','','',
    '','','','','','','','','','','','','','','','',
    '','','','','','','','','','','','','','','',
    '','','','','','','','','','','','','','','',
    '0','1','2','3','4','5','6','7','8','9','10'])

# ========================================
# 1.  
# ========================================
def clean_chinese_text(texts):
    """  """
    cleaned = []
    for t in texts:
        t = str(t)
        t = re.sub(r'http[s]?://\S+', '', t)  #  URL
        t = re.sub(r'[^\u4e00-\u9fff\w\s]', '', t)  #  
        t = re.sub(r'\s+', ' ', t).strip()  #  
        t = re.sub(r'\d+', '', t)  #  
        cleaned.append(t)
    return cleaned

# ========================================
# 2. Jieba  +  
# ========================================
def segment_and_count(texts, top_n=50):
    """ Jieba  +  """
    try:
        import jieba
    except ImportError:
        return {'error': 'jieba  (pip install jieba)'}
    
    cleaned = clean_chinese_text(texts)
    all_words = []
    for t in cleaned:
        if t.strip():
            words = jieba.cut(t)
            all_words.extend([w.strip() for w in words if w.strip() and len(w.strip()) > 1 
                             and w.strip() not in CN_STOPWORDS])
    
    word_counts = Counter(all_words)
    top_words = word_counts.most_common(top_n)
    
    return {
        'total_words': len(all_words),
        'unique_words': len(word_counts),
        'top_words': [{'word': w, 'count': c} for w, c in top_words],
        'word_counts': dict(word_counts),
    }

# ========================================
# 3.  
# ========================================
def generate_wordcloud(texts, width=800, height=400, bg_color='white', max_words=100):
    """  """
    try:
        from wordcloud import WordCloud
    except ImportError:
        return {'error': 'wordcloud  (pip install wordcloud)'}
    
    try:
        import jieba
    except ImportError:
        return {'error': 'jieba  (pip install jieba)'}
    
    cleaned = clean_chinese_text(texts)
    text = ' '.join(cleaned)
    words = jieba.cut(text)
    filtered = ' '.join([w for w in words if len(w.strip()) > 1 and w.strip() not in CN_STOPWORDS])
    
    if not filtered.strip():
        return {'error': '  '}
    
    wc = WordCloud(
        width=width, height=height,
        background_color=bg_color,
        font_path=None,  #  
        max_words=max_words,
        collocations=False,
        colormap='viridis',
    ).generate(filtered)
    
    fig, ax = plt.subplots(figsize=(width/100, height/100))
    ax.imshow(wc, interpolation='bilinear')
    ax.axis('off')
    plt.tight_layout(pad=0)
    
    buf = BytesIO()
    plt.savefig(buf, format='png', dpi=100, bbox_inches='tight', facecolor=bg_color)
    buf.seek(0)
    chart_b64 = base64.b64encode(buf.read()).decode('utf-8')
    plt.close()
    
    return {
        'chart': chart_b64,
        'width': width, 'height': height,
    }

# ========================================
# 4. TF-IDF 
# ========================================
def extract_keywords_tfidf(texts, top_n=20):
    """ TF-IDF  """
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        import jieba
    except ImportError:
        return {'error': 'scikit-learn / jieba  (pip install scikit-learn jieba)'}
    
    cleaned = clean_chinese_text(texts)
    segmented = []
    for t in cleaned:
        if t.strip():
            words = jieba.cut(t)
            filtered = [w for w in words if len(w.strip()) > 1 and w.strip() not in CN_STOPWORDS]
            segmented.append(' '.join(filtered))
        else:
            segmented.append('')
    
    if not any(segmented):
        return {'error': '  '}
    
    vectorizer = TfidfVectorizer(max_features=top_n * 2)
    tfidf_matrix = vectorizer.fit_transform(segmented)
    
    feature_names = vectorizer.get_feature_names_out()
    scores = np.array(tfidf_matrix.sum(axis=0)).flatten()
    
    keyword_scores = sorted(zip(feature_names, scores), key=lambda x: x[1], reverse=True)[:top_n]
    
    return {
        'keywords': [{'word': w, 'score': float(s)} for w, s in keyword_scores],
        'n_docs': len(texts),
    }

# ========================================
# 5. 
# ========================================
def sentiment_analysis(texts):
    """ SnowNLP  """
    try:
        from snownlp import SnowNLP
    except ImportError:
        return {'error': 'snownlp  (pip install snownlp)'}

    scores = []
    for t in texts:
        t = str(t).strip()
        if t:
            s = SnowNLP(t)
            scores.append(round(s.sentiments, 4))
        else:
            scores.append(0.5)

    if not scores:
        return {'error': '  '}

    avg = sum(scores) / len(scores)
    positive = sum(1 for s in scores if s > 0.5)
    negative = sum(1 for s in scores if s < 0.5)
    neutral = len(scores) - positive - negative

    return {
        'scores': scores,
        'avg': round(avg, 4),
        'positive': positive,
        'negative': negative,
        'neutral': neutral,
        'total': len(scores),
        'label': '正面' if avg > 0.5 else '负面' if avg < 0.5 else '中性',
    }


# ========================================
# 4. LDA 主题建模（sklearn）
# ========================================
def _seg_docs(texts):
    """分词去停用词，返回空格连接的分词文档（供向量化）"""
    import jieba
    cleaned = clean_chinese_text(texts)
    docs = []
    for t in cleaned:
        words = [w for w in jieba.cut(t)
                 if w.strip() and len(w.strip()) > 1 and w.strip() not in CN_STOPWORDS]
        docs.append(' '.join(words))
    return docs


def lda_topics(texts, n_topics=3, top_words=8):
    """LDA 主题建模（财报/研报解读：自动归纳主题词）"""
    from sklearn.feature_extraction.text import CountVectorizer
    from sklearn.decomposition import LatentDirichletAllocation
    docs = [d for d in _seg_docs(texts) if d.strip()]
    if len(docs) < n_topics + 3:
        return {'error': f'有效文档不足（{len(docs)}<{n_topics + 3}），无法主题建模'}
    vec = CountVectorizer(max_features=1000)
    X = vec.fit_transform(docs)
    if X.shape[1] < 3:
        return {'error': '词汇量过小，无法主题建模'}
    lda = LatentDirichletAllocation(n_components=n_topics, random_state=42)
    lda.fit(X)
    feat = vec.get_feature_names_out()
    topics = []
    for i, comp in enumerate(lda.components_):
        top_idx = comp.argsort()[-top_words:][::-1]
        topics.append({
            'topic': i + 1,
            'words': [str(feat[j]) for j in top_idx],
            'weights': [round(float(comp[j]), 3) for j in top_idx],
        })
    doc_top = lda.transform(X)
    return {
        'topics': topics,
        'n_docs': len(docs),
        'n_topics': n_topics,
        'doc_distribution': [[round(float(v), 3) for v in row] for row in doc_top[:20]],
    }


# ========================================
# 5. 文本相似度（TF-IDF + cosine）
# ========================================
def text_similarity(texts, target=None, top_n=10):
    """文本相似度：target 指定 → 各文本与 target 相似度；否则两两最高相似对"""
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    docs = _seg_docs(texts)
    valid = [(i, d) for i, d in enumerate(docs) if d.strip()]
    if len(valid) < 2 and target is None:
        return {'error': '有效文档不足（<2）'}
    ids = [i for i, _ in valid]
    corpus = [d for _, d in valid]
    if target is not None:
        t_doc = ' '.join(_seg_docs([target]))
        vec = TfidfVectorizer()
        mat = vec.fit_transform(corpus + [t_doc])
        sims = cosine_similarity(mat[:-1], mat[-1:]).flatten()
        ranked = sorted(zip(ids, sims.tolist()), key=lambda x: -x[1])[:top_n]
        return {'target_similarity': [
            {'doc_index': i, 'similarity': round(float(s), 4)} for i, s in ranked],
            'n_docs': len(valid)}
    vec = TfidfVectorizer()
    mat = vec.fit_transform(corpus)
    sim = cosine_similarity(mat)
    pairs = []
    for a in range(len(valid)):
        for b in range(a + 1, len(valid)):
            pairs.append({'a_index': ids[a], 'b_index': ids[b],
                          'similarity': round(float(sim[a][b]), 4)})
    pairs.sort(key=lambda x: -x['similarity'])
    return {'top_pairs': pairs[:top_n], 'n_docs': len(valid)}
