# -*- coding: utf-8 -*-
"""回归：RAG 问答接通知识库（方案一）。

修复前的行为（均有源码定位）：
  * `/api/rag/query` 只查内存态 `rag_engine` —— 无持久化，重启即空；
  * 从不传 `llm_function`，`answer` 恒为「已检索到 N 条与「X」相关的片段。」；
  * 与知识库管理页写入的 SQLite `knowledge_docs` 完全不连通
    （生产实测 143 篇文档在该接口下**一篇都检索不到**）。

本组用例断言修复后的契约：
  知识库文档必须能被本接口检索到，回答不得是那句无信息量的计数废话。
"""

import os
import re

import pytest

# 哨兵词：内置知识点库（static/knowledge_data.json）一定不含它，
# 因此任何命中都只可能来自本用例插入的 SQLite 文档 —— 用于隔离 B 通道效果。
KB_SENTINEL = 'SENTINEL_DURATION_CONVEXITY_9X7'


@pytest.fixture
def seeded_kb(app):
    """在建一个知识库 + 一篇含哨兵词的文档；用例结束后清理。"""
    from models import db, KnowledgeBase, KnowledgeDoc
    with app.app_context():
        _purge_kb()
        kb = KnowledgeBase(id='kb_rag_bridge_test', user_id='u_rag_bridge',
                           name='RAG 桥接测试库')
        doc = KnowledgeDoc(
            id='doc_rag_bridge_test', kb_id='kb_rag_bridge_test', user_id='u_rag_bridge',
            title='久期与凸性说明',
            content=(f'{KB_SENTINEL} 久期衡量债券价格对利率的一阶敏感度，'
                     '凸性衡量二阶敏感度；利率变动较大时凸性不可忽略。'),
            file_type='txt', chunk_count=1)
        db.session.add(kb)
        db.session.add(doc)
        db.session.commit()
    yield
    with app.app_context():
        _purge_kb()


def _purge_kb():
    """删除本用例写入的知识库数据（幂等）。"""
    from models import db, KnowledgeBase, KnowledgeDoc
    KnowledgeDoc.query.filter_by(kb_id='kb_rag_bridge_test').delete()
    KnowledgeBase.query.filter_by(id='kb_rag_bridge_test').delete()
    db.session.commit()


@pytest.fixture
def no_llm(monkeypatch):
    """关闭 LLM 生成，使断言聚焦于检索链路本身（并避免测试触网）。"""
    monkeypatch.setenv('RAG_ANSWER_LLM', '0')


def test_knowledge_base_doc_is_retrievable(app, seeded_kb, no_llm):
    """知识库(SQLite)里的文档必须能被 RAG 问答检索到 —— 这是本次修复的核心。"""
    from rag_answer import answer_with_knowledge
    with app.app_context():
        res = answer_with_knowledge(KB_SENTINEL)
    assert res['knowledge_hits'], (
        '知识库文档未被检索到：RAG 问答仍与知识库脱节（修复前该断言必然失败）')
    assert any('久期与凸性说明' in s['doc_name'] for s in res['sources']), \
        '来源列表必须带出知识库文档标题'


def test_answer_is_not_bare_count_sentence(app, seeded_kb, no_llm):
    """回答不得是修复前那句无信息量的计数废话。"""
    from rag_answer import answer_with_knowledge
    with app.app_context():
        res = answer_with_knowledge(KB_SENTINEL)
    answer = res['answer'] or ''
    assert answer.strip(), '不得返回空回答'
    assert not re.match(r'^已检索到\s*\d+\s*条与', answer), \
        '回答不能退化成「已检索到 N 条…」的计数句'
    assert KB_SENTINEL in answer or '久期' in answer, \
        '兜底回答必须包含真正命中的原文，而不是一句计数'


def test_sources_shape_is_frontend_compatible(app, seeded_kb, no_llm):
    """sources 结构须保持前端既有渲染契约（doc_name + score），并标注来源类别。"""
    from rag_answer import answer_with_knowledge
    with app.app_context():
        res = answer_with_knowledge(KB_SENTINEL)
    assert res['sources'], '必须有来源'
    for s in res['sources']:
        assert 'doc_name' in s and 'score' in s, '前端依赖 doc_name / score 两个字段'
        assert isinstance(s['score'], float), 'score 必须是可直接展示的数值'
        assert 0.0 <= s['score'] <= 1.0, f"score 未归一化，前端会显示成「相关度 {s['score']}」"
        assert s['doc_name'].startswith('['), 'doc_name 需带来源前缀以区分知识库/用户文件'


def test_empty_question_is_guarded(app, no_llm):
    """空问题必须被挡住，不得抛异常、不得触发检索。"""
    from rag_answer import answer_with_knowledge
    with app.app_context():
        res = answer_with_knowledge('   ')
    assert res['sources'] == [] and res['chunks'] == []
    assert res['answer'].startswith('请先输入'), '空问题需给出明确提示而非静默返回'


def test_llm_is_used_when_available(app, seeded_kb, monkeypatch):
    """LLM 可用时必须走综合生成，而不是兜底摘要。"""
    import ai_assistant
    monkeypatch.setenv('RAG_ANSWER_LLM', '1')
    monkeypatch.setattr(ai_assistant, '_rag_answer',
                        lambda *a, **k: '这是模型综合生成的回答')
    from rag_answer import answer_with_knowledge
    with app.app_context():
        res = answer_with_knowledge(KB_SENTINEL)
    assert res['answer'] == '这是模型综合生成的回答'
    assert res['llm_synthesized'] is True


def test_llm_failure_falls_back_to_readable_summary(app, seeded_kb, monkeypatch):
    """LLM 抛异常时必须回退到含原文的可读摘要，且不把失败写成成功。"""
    import ai_assistant

    def _boom(*a, **k):
        raise RuntimeError('provider 不可用')

    monkeypatch.setenv('RAG_ANSWER_LLM', '1')
    monkeypatch.setattr(ai_assistant, '_rag_answer', _boom)
    from rag_answer import answer_with_knowledge
    with app.app_context():
        res = answer_with_knowledge(KB_SENTINEL)
    assert res['llm_synthesized'] is False, '生成失败不得标记为已生成'
    assert res['answer'].strip(), '回退后仍必须有可读内容'
    assert '检索到' in res['answer']


def test_user_file_rag_still_contributes(app, monkeypatch):
    """用户文件（内存 rag_engine）通道不得因本次改动失效，且来源需可区分。"""
    from rag_unified import rag_engine
    monkeypatch.setenv('RAG_ANSWER_LLM', '0')
    doc_id = rag_engine.add_document(
        '文件: sales.csv\n列: 客户编号, 客户收入, 城市\nSENTINEL_FILE_ROW 1200 北京\n',
        'sales.csv', 'user_file')
    try:
        from rag_answer import answer_with_knowledge
        with app.app_context():
            res = answer_with_knowledge('客户收入', top_k=5)
        assert res['chunks'], '内存 RAG 通道失效：用户文件片段未返回'
        assert any(s['doc_name'].startswith('[用户文件]') for s in res['sources']), \
            '用户文件来源需带 [用户文件] 前缀以便与知识库区分'
    finally:
        rag_engine.delete_document(doc_id)


def test_route_uses_unified_answer(app, client, admin_token, no_llm):
    """/api/rag/query 必须走统一入口（返回 llm_synthesized 字段即证明已接通）。"""
    r = client.post('/api/rag/query',
                    json={'question': KB_SENTINEL, 'top_k': 3},
                    headers={'Authorization': f'Bearer {admin_token}'})
    assert r.status_code == 200, f'接口返回 {r.status_code}，修复前此路由恒定 500'
    body = r.get_json()
    assert 'answer' in body and 'sources' in body
    assert 'llm_synthesized' in body, '缺少该字段说明路由未走 rag_answer 统一入口'
