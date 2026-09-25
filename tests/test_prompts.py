# -*- coding: utf-8 -*-
"""prompts.py 单测：提示词默认值 / 版本化覆盖 / 领域人设"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import prompts
from prompts import get_prompt, set_prompt, get_all_versions, resolve_domain_prompt


def test_default_prompts_present():
    for name in ('agent_system', 'refinement', 'force_final', 'no_tools_retry',
                 'human_style', 'knowledge_system', 'fast_chat_system',
                 'did_framework', 'panel_data_hint'):
        assert get_prompt(name), f'缺少默认提示词: {name}'


def test_agent_system_has_tools_placeholder():
    assert '{tools_prompt}' in get_prompt('agent_system')


def test_agent_system_guides_panel_analysis():
    """系统提示应引导面板数据识别与分层分析（达到深度分析程度）"""
    prompt = get_prompt('agent_system')
    assert '面板' in prompt or 'overview' in prompt
    assert 'did_regression' in prompt or 'did_full' in prompt


def test_convergence_prompts_are_chinese_only():
    """收敛提示词必须全程中文（含语言纪律条款）——
    英文开头的收敛提示会把推理模型带成英文思考链，
    导致前端展示的 reasoning_content 出现 'Let me carefully review...' 之类英文推理（2026-09-18 用户反馈）。
    """
    import re
    for name in ('refinement', 'force_final', 'refinement_no_file', 'force_final_no_file'):
        p = get_prompt(name)
        # ① 不得以英文句子开头（首行须含中文）
        first_line = p.splitlines()[0]
        assert re.search(r'[\u4e00-\u9fff]', first_line), f'{name} 首行不是中文: {first_line!r}'
        # ② 不得残留英文指令短语
        for frag in ('Based on the tool results', 'You have already received',
                     'STRICT RULES', 'STOP calling tools', 'Do NOT output'):
            assert frag not in p, f'{name} 残留英文指令: {frag!r}'
        # ③ 必须显式要求内部思考也用中文
        assert '内部思考' in p and '简体中文' in p, f'{name} 缺少语言纪律条款'


def test_refinement_is_deep_report():
    """收敛提示应为深度专业报告模板（7 段结构、支持表格）"""
    prompt = get_prompt('refinement')
    assert '数据概况' in prompt and '变量体系' in prompt
    assert '研究设计' in prompt and '稳健性' in prompt
    assert '数据质量评价' in prompt and '下一步建议' in prompt
    assert '表格' in prompt


def test_did_framework_present():
    prompt = get_prompt('did_framework')
    assert '双向固定效应' in prompt and '平行趋势' in prompt
    assert '安慰剂' in prompt and '聚类' in prompt


def test_panel_data_hint_present():
    prompt = get_prompt('panel_data_hint')
    assert '平衡性' in prompt and '处理组' in prompt


def test_set_prompt_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(prompts, '_OVERRIDE_FILE', tmp_path / 'prompt_versions.json')
    set_prompt('refinement', '自定义收敛提示')
    assert get_prompt('refinement') == '自定义收敛提示'
    # 覆盖文件已写入
    assert (tmp_path / 'prompt_versions.json').exists()
    # 未覆盖的提示词仍用默认
    assert get_prompt('agent_system')


def test_get_all_versions():
    versions = get_all_versions()
    assert 'agent_system' in versions and 'human_style' in versions


def test_resolve_domain_tax():
    prompt = resolve_domain_prompt('增值税税率是多少')
    assert '税务师' in prompt and '主管税务机关' in prompt


def test_resolve_domain_finance():
    prompt = resolve_domain_prompt('房贷利率怎么算')
    assert '金融' in prompt or '银行' in prompt


def test_resolve_domain_generic():
    prompt = resolve_domain_prompt('今天天气怎么样')
    assert '智能信息检索' in prompt
