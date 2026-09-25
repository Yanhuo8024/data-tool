# -*- coding: utf-8 -*-
"""tests/test_rag_recall_fix.py — 用户文件 RAG 召回与长期记忆召回的功能性回归

本组测试针对三个**真实功能缺陷**（不是风格问题）：

① 中文提问在 `rag_unified` 里几乎必然零命中
   原分词是贪婪正则 `[\\u4e00-\\u9fff]{2,}`：一整段连续中文被当成「一个词」。
   索引键是整块 300 字语料，提问是另一个整块字符串，两者永不相等。
   只有英文列名（revenue）能靠英文分支命中 —— 中文列名的文件等于检索不到。

② 索引变更后查询缓存不失效（`QueryCache` 无失效路径）
   新增/删除文档后，同一提问在 300s TTL 内仍返回旧结果：新文件内容"看不见"，
   已删除内容仍能被检索到。

③ 同名文件改内容重传不重建索引
   幂等键只有 `文件名+模式`，覆盖上传后索引永远停留在旧内容。

另含长期记忆（`agent_long_memory`）从「固定取最近 3 条」改为按当前问题相关度
召回的行为验证。
"""
import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agent_long_memory  # noqa: E402
import core_config  # noqa: E402
import egress_policy as ep  # noqa: E402
import file_rag  # noqa: E402
import term_extract  # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    """隔离出境策略文件 + 清空相关环境变量"""
    p = tmp_path / 'egress_policy.json'
    monkeypatch.setenv('EGRESS_POLICY_FILE', str(p))
    for feat in ep.FEATURES.values():
        monkeypatch.delenv(feat['env'], raising=False)
    ep.reset_cache()
    yield p
    ep.reset_cache()


@pytest.fixture
def out_dir(tmp_path, monkeypatch):
    """把 file_rag 读取文件的位置指到临时目录"""
    monkeypatch.setattr(core_config, 'OUT_DIR', tmp_path)
    monkeypatch.setattr(file_rag, '_INDEXED', {})
    return tmp_path


@pytest.fixture
def eng_recorder(monkeypatch):
    """替掉 rag_unified.rag_engine，记录写入的文档正文"""

    class _Eng:
        def __init__(self):
            self.docs, self.deleted, self._n = {}, [], 0

        def add_document(self, text, name, dtype='generic'):
            self._n += 1
            did = f'd{self._n}'
            self.docs[did] = text
            return did

        def delete_document(self, did):
            self.deleted.append(did)
            self.docs.pop(did, None)

        def search(self, *_a, **_k):
            return []

    eng = _Eng()
    monkeypatch.setitem(sys.modules, 'rag_unified', types.SimpleNamespace(rag_engine=eng))
    return eng


def _engine():
    """真实 HybridRAG（无向量库，走关键词路 —— 与生产配置一致）"""
    from rag_unified import HybridRAG
    return HybridRAG(embedding_model='auto', use_faiss=False)


# ==================== ① 中文检索词抽取 ====================
def test_extract_terms_yields_chinese_ngrams_not_whole_run():
    terms = term_extract.extract_terms('客户收入缺失情况')
    assert '客户' in terms and '收入' in terms and '缺失' in terms
    assert '客户收入缺失情况' not in terms, '不得把整段中文当成一个词（原缺陷）'


def test_extract_terms_keeps_short_words_and_english():
    terms = term_extract.extract_terms('久期 duration 与 营收')
    assert '久期' in terms and '营收' in terms, '短词（列名）应原样保留'
    assert 'duration' in terms


def test_greedy_regex_proved_zero_overlap():
    """固化缺陷证据：旧的贪婪中文正则对自然语言提问与语料**零交集**"""
    import re
    greedy = lambda t: set(re.findall(r'[\u4e00-\u9fff]{2,}', t))  # noqa: E731
    query = '这份数据里哪些列反映客户收入情况'
    corpus = '列: 客户编号, 客户收入, 城市'
    assert not (greedy(query) & greedy(corpus)), '旧口径重叠为空 → 中文提问必然零命中'
    assert term_extract.grams2(query) & term_extract.grams2(corpus), '新口径必须能对上'


def test_chinese_query_hits_chinese_chunk():
    eng = _engine()
    eng.add_document('文件: sales.csv（5 行 × 3 列）\n列: 客户编号, 客户收入, 城市\n'
                     '客户编号 客户收入 城市\nC001 1200 北京\n', 'sales.csv', 'user_file')
    hits = eng.search('哪些列反映客户收入情况', top_k=3)
    assert hits, '中文提问必须能命中中文语料（原实现返回空）'
    assert '客户收入' in hits[0].chunk.text


# ==================== ② 索引变更后缓存失效 ====================
def test_new_document_visible_for_same_query():
    eng = _engine()
    eng.add_document('文件: a.csv\n列: 客户收入\n样本 1200\n', 'a.csv', 'user_file')
    assert eng.search('客户收入', top_k=5), '预热缓存：首查应有结果'

    eng.add_document('文件: b.csv\n列: 客户收入\n样本 9800 SENTINEL_B\n', 'b.csv', 'user_file')
    hits = eng.search('客户收入', top_k=5)
    assert any('SENTINEL_B' in h.chunk.text for h in hits), \
        '新增文档后同一提问必须能看到新内容（缓存未失效会返回旧结果）'


def test_deleted_document_disappears_for_same_query():
    eng = _engine()
    did = eng.add_document('文件: a.csv\n列: 客户收入\n样本 SENTINEL_A\n', 'a.csv', 'user_file')
    assert eng.search('客户收入', top_k=5), '预热缓存'
    eng.delete_document(did)
    hits = eng.search('客户收入', top_k=5)
    assert not any('SENTINEL_A' in h.chunk.text for h in hits), \
        '删除文档后不得再被检索到（缓存未失效会残留已删内容）'


def test_query_accepts_top_k_and_limits_sources():
    """回归：路由 /api/rag/query 传 top_k，而 HybridRAG.query 此前不接受该参数 → 该接口恒 500"""
    eng = _engine()
    for i in range(6):
        eng.add_document(f'文件: f{i}.csv\n列: 客户收入\n样本 {i} 客户收入\n', f'f{i}.csv', 'user_file')
    res = eng.query('客户收入', top_k=2)
    assert res['answer'], '无 llm_function 时也必须给出可读回答'
    assert len(res['sources']) <= 2, 'top_k 必须真正限制返回条数'


# ==================== ③ 同名文件内容变更重建索引 ====================
def test_same_name_file_with_new_content_is_reindexed(store, out_dir, eng_recorder):
    f = out_dir / 'data.csv'
    f.write_text('a,b\n1,2\n', encoding='utf-8')
    os.utime(f, (1000, 1000))
    first = file_rag.index_current_file('data.csv')
    assert first

    f.write_text('a,b\n3,4\n5,6\n', encoding='utf-8')
    os.utime(f, (2000, 2000))
    second = file_rag.index_current_file('data.csv')
    assert second and second != first, '同名文件内容变更后必须重建索引'
    assert first in eng_recorder.deleted, '旧索引须被剔除'


def test_unchanged_file_stays_idempotent(store, out_dir, eng_recorder):
    f = out_dir / 'data.csv'
    f.write_text('a,b\n1,2\n', encoding='utf-8')
    os.utime(f, (1000, 1000))
    first = file_rag.index_current_file('data.csv')
    second = file_rag.index_current_file('data.csv')
    assert first == second, '内容未变不得重复索引（幂等）'
    assert not eng_recorder.deleted


# ==================== 长期记忆按相关度召回 ====================
@pytest.fixture
def ltm(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_long_memory, '_MEMORY_ROOT', tmp_path)
    agent_long_memory.clear_cache()
    m = agent_long_memory.get_long_term('u-recall')
    # 最早一条与「久期」相关，随后 3 条无关主题 —— 默认只取最近 3 条会把它挤掉
    m.record_interaction('债券久期', '组合久期 5.2 年')
    m.record_interaction('股票估值', 'PE 偏高')
    m.record_interaction('房价走势', '一线城市回落')
    m.record_interaction('汇率波动', '美元指数走强')
    agent_long_memory.clear_cache()
    yield m
    agent_long_memory.clear_cache()


def test_recall_prefers_relevant_entry_over_recency(ltm):
    text = ltm.to_natural_language('债券久期怎么看')
    assert '债券久期' in text, '相关但与当前问题相隔较远的结论必须被召回'


def test_no_query_keeps_recent_behaviour(ltm):
    text = ltm.to_natural_language()
    assert '债券久期' not in text, '不传 query 时保持旧行为（仅最近 3 条）'


def test_overlap_is_zero_for_unrelated_text():
    assert term_extract.overlap('债券久期', '房价走势一线城市回落') == 0.0
    assert term_extract.overlap('债券久期', '组合久期 5.2 年') > 0.0


# ==================== ④ 分块器：超长无空行段落必须切开 ====================
def test_long_single_paragraph_is_split_into_bounded_chunks():
    """原 chunk_text 只按空行切段，超长单段整段吞下 → 无空行的长文/JSON 变 1 个巨型块"""
    from rag_unified import chunk_text
    text = '客户收入' * 900                     # 3600 字，无空行
    parts = chunk_text(text, 300, 50)
    assert len(parts) > 1, '超长单段必须被切开'
    assert max(len(p[0]) for p in parts) <= 320, '块长必须受 chunk_size 约束'


def test_long_chinese_document_is_retrievable():
    """长中文单段文档 + 中文提问：分块与词表同时正常时才可能命中（此前恒 0）"""
    eng = _engine()
    body = '本文档说明客户理财产品的风险等级划分与适当性要求。' * 60
    eng.add_document(body, 'risk.md', 'user_file')
    hits = eng.search('客户理财产品的风险等级怎么划分', top_k=3)
    assert hits, '长中文文档必须能被中文提问检索到'
    assert '风险等级' in hits[0].chunk.text


def test_terms_keep_two_grams_under_cap():
    """超上限时必须优先保留 2-gram：按字典序硬截断会随机丢掉「客户」「风险」这类词"""
    long_cn = '客户风险等级理财产品说明' * 80
    terms = term_extract.extract_terms(long_cn)
    assert '客户' in terms and '风险' in terms
    assert len(terms) <= term_extract.DEFAULT_MAX_TERMS
