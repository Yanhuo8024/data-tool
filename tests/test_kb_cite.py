#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""知识库引用溯源测试（kb_cite）

覆盖：编号稳定性、依据块渲染、引用反向核验（防编造出处）、幂等增强。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kb_cite import (annotate_context, build_citation_block, cite_id,
                     enhance_answer, format_knowledge_answer, verify_citations)

E1 = {'name': '久期与凸性', 'category': 'fixed_income', 'tab': '固定收益',
      'desc': '久期衡量利率敏感性', 'detail': '凸性是二阶修正'}
E2 = {'name': '个人所得税', 'category': 'tax', 'tab': '税收政策',
      'desc': '综合所得按年计', 'detail': '七级超额累进'}


def test_cite_id_is_stable_and_name_bound():
    """同一 name 必须跨调用稳定；不同 name 必须不同（否则历史引用会串号）。"""
    assert cite_id(E1) == cite_id(dict(E1))
    assert cite_id(E1) != cite_id(E2)
    assert cite_id(E1).startswith('KB-fixed_income-')
    # 缺字段不炸，落到 misc 兜底
    assert cite_id({}).startswith('KB-misc-')


def test_cite_id_ignores_other_field_changes():
    """编号只由 name 决定：desc 变化不应改号，否则知识库改文案就断链。"""
    mutated = dict(E1)
    mutated['desc'] = '换了描述'
    mutated['detail'] = '换了详情'
    assert cite_id(mutated) == cite_id(E1)


def test_citation_block_contains_id_and_source():
    block = build_citation_block([E1, E2])
    assert '📚 依据' in block
    assert cite_id(E1) in block and cite_id(E2) in block
    assert '固定收益' in block and '税收政策' in block


def test_citation_block_empty_when_no_entries():
    assert build_citation_block([]) == ''
    assert build_citation_block(None) == ''


def test_verify_citations_detects_fabrication():
    """回答里引用了不在召回集合内的编号 → 必须被识别为编造出处。"""
    real = cite_id(E1)
    text = f'久期越大波动越大 {real}，另外据 {("KB-tax-dead")} 也在上升。'
    valid, fabricated = verify_citations(text, [E1])
    assert real in valid
    assert 'KB-tax-dead' in fabricated


def test_verify_citations_empty_when_no_reference():
    valid, fabricated = verify_citations('这是一段没有任何引用的回答。', [E1])
    assert valid == [] and fabricated == []


def test_build_citation_block_flags_fabricated():
    block = build_citation_block([E1], fabricated=['KB-tax-dead'])
    assert '无法核验' in block and 'KB-tax-dead' in block


def test_enhance_answer_appends_block_and_is_idempotent():
    out = enhance_answer('久期越长，利率敏感度越高。', [E1])
    assert '📚 依据' in out and cite_id(E1) in out
    assert enhance_answer(out, [E1]) == out          # 二次调用不重复追加


def test_enhance_answer_accepts_disclaimer():
    out = enhance_answer('税率以官方为准。', [E2], disclaimer='\n\n> 时效性提示')
    assert '时效性提示' in out and '📚 依据' in out
    assert out.index('时效性提示') < out.index('📚 依据')


def test_enhance_answer_empty_input_passthrough():
    assert enhance_answer('', [E1]) == ''


def test_annotate_context_carries_ids_and_rules():
    ctx = annotate_context([E1, E2])
    assert cite_id(E1) in ctx and cite_id(E2) in ctx
    assert '引用规则' in ctx and '不得编造' in ctx
    assert annotate_context([]) == ''


def test_format_knowledge_answer_keeps_legacy_sections():
    """既有字段（公式/参数/踩坑）不得因迁模块而丢失。"""
    entry = dict(E1, formula='D = Σ t·CF/(1+y)^t',
                 params=[{'k': 'y', 'v': '到期收益率'}],
                 pitfalls=['用修正久期算大幅利率变动', ],
                 related=['收益率曲线'])
    out = format_knowledge_answer([entry])
    for token in ('公式', '参数', '常见踩坑', '相关概念', cite_id(entry), '📚 依据'):
        assert token in out, token


def test_format_knowledge_answer_empty():
    assert format_knowledge_answer([]) == ''


def test_reexport_from_knowledge_rag_behaves_identically():
    """迁出后 knowledge_rag 必须原样再导出（转发实现，行为一致），既有 import 路径不得断裂。"""
    import knowledge_rag
    assert knowledge_rag.format_knowledge_answer([E1]) == format_knowledge_answer([E1])
    assert '📚 依据' in knowledge_rag.format_knowledge_answer([E1])


def test_knowledge_context_injection_carries_ids():
    """注入上下文必须带编号，否则模型无法在正文标注出处。"""
    import knowledge_rag
    ctx = knowledge_rag.get_knowledge_context('久期是什么', top_k=2)
    if not ctx:
        return                                        # 知识库缺失时跳过（环境相关）
    assert 'KB-' in ctx
