# -*- coding: utf-8 -*-
"""knowledge_rag.py 单测"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from knowledge_rag import (search_knowledge, is_knowledge_query,
                           format_knowledge_answer, get_knowledge_context,
                           load_knowledge)


def test_load_knowledge():
    kb = load_knowledge()
    assert len(kb) > 50


def test_search_hits():
    assert search_knowledge('市盈率', top_k=1)[0]['name'].startswith('市盈率')
    assert search_knowledge('什么是DID', top_k=1)[0]['name'] == 'DID双重差分'
    assert search_knowledge('XGBoost', top_k=1)[0]['name'] == 'XGBoost'


def test_search_no_noise():
    # 英文词不能子串误伤（did 不应命中无关条目）
    hits = search_knowledge('DID是什么', top_k=3)
    assert hits[0]['name'] == 'DID双重差分'


def test_is_knowledge_query():
    assert is_knowledge_query('什么是随机森林', search_knowledge('什么是随机森林'))
    assert is_knowledge_query('PE', search_knowledge('PE'))
    assert is_knowledge_query('DID', search_knowledge('DID'))
    # 分析类请求不应误判
    assert not is_knowledge_query('用随机森林分析数据', search_knowledge('用随机森林分析数据'))
    assert not is_knowledge_query('你好', search_knowledge('你好'))


def test_format_answer():
    hits = search_knowledge('什么是市盈率', top_k=1)
    ans = format_knowledge_answer(hits)
    assert '市盈率' in ans and '公式' in ans


def test_agent_context():
    ctx = get_knowledge_context('随机森林和XGBoost的区别', top_k=2)
    assert '随机森林' in ctx and 'XGBoost' in ctx
