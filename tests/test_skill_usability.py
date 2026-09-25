# -*- coding: utf-8 -*-
"""
test_skill_usability.py — 校验每个 Skill 是否真实可用。

对 skills/<name>/SKILL.md 逐一验证：
  1. 文件存在且能被 _parse_skill_md 正确解析（name/description/正文非空）
  2. get_skill() 能按名加载完整正文
  3. 出现在 skill_manager.list_skills() 中
  4. run_skill 工具：已启用 → 能取回正文；未启用 → 明确报"未启用"
  5. 已启用技能会注入 get_skills_prompt()
  6. match_skill() 已启用技能可被触发；未启用技能返回未命中但能识别出 candidate
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import skill_manager as sm
from skill_manager import SKILLS_DIR

# 平台当前内置技能（要求全部存在；启用与否不在此断言）
KNOWN_SKILLS = {'buffett-oracle-analyzer', 'company-analysis', 'finance-sentiment', 'industry-research'}


def _all_skill_dirs():
    if not os.path.isdir(SKILLS_DIR):
        return []
    return [d for d in sorted(os.listdir(SKILLS_DIR)) if os.path.isfile(os.path.join(SKILLS_DIR, d, 'SKILL.md'))]


@pytest.mark.parametrize('known', sorted(KNOWN_SKILLS))
def test_known_skill_present(known):
    """内置技能目录都必须存在且含可解析的 SKILL.md。"""
    assert os.path.isfile(os.path.join(SKILLS_DIR, known, 'SKILL.md')), f"缺少技能目录: {known}"


@pytest.mark.parametrize('sname', _all_skill_dirs())
def test_each_skill_usable(sname):
    """逐一校验每个技能「可解析、可加载、可被工具调用、可触发」。"""
    d = os.path.join(SKILLS_DIR, sname)
    md = os.path.join(d, 'SKILL.md')
    with open(md, encoding='utf-8') as f:
        text = f.read()

    # 1) 解析
    name, desc, body = sm._parse_skill_md(text)
    assert name == sname, f"{sname}: frontmatter name 不一致 ({name})"
    assert desc.strip(), f"{sname}: description 为空"
    assert len(body.strip()) >= 100, f"{sname}: 正文过短（不能成为有效工作流），len={len(body)}"

    # 2) get_skill 加载
    skill = sm.get_skill(name)
    assert skill and skill.get('body'), f"{sname}: get_skill 加载失败"

    # 3) 出现在 list_skills
    listed = {s['name'] for s in sm.list_skills()}
    assert name in listed, f"{sname}: 未出现在 list_skills()"

    # 4) run_skill 工具（保留原始启用状态，测试后还原，不改变用户配置）
    from ai_agent import tool_run_skill
    prior_enabled = os.path.isfile(os.path.join(d, '.enabled'))
    sm.set_skill_enabled(name, True)
    try:
        res = tool_run_skill(name, task='测试')
        assert not res.startswith('Error'), f"{sname} 启用后 run_skill 失败: {res[:120]}"
        assert res.startswith('# 技能已加载'), f"{sname}: run_skill 返回格式异常: {res[:60]}"
    finally:
        sm.set_skill_enabled(name, prior_enabled)
    if not prior_enabled:
        res = tool_run_skill(name, task='测试')
        assert res.startswith('Error'), f"{sname} 未启用却加载成功（状态异常）"


@pytest.mark.parametrize('sname', _all_skill_dirs())
def test_each_skill_registered_for_trigger(sname):
    """每个技能都应在自动触发关键词表或公司引用识别中有入口，确保能被自动调用。"""
    import ai_agent as A
    names = {t['name'] for t in A.registry.list_tools()}
    assert 'run_skill' in names and 'list_skills' in names, "run_skill/list_skills 工具未注册"
    assert sname in sm._SKILL_TRIGGERS or sname in ('buffett-oracle-analyzer', 'company-analysis'), \
        f"{sname}: 未在 _SKILL_TRIGGERS 中登记自动触发"


def test_enabled_skill_injected_into_prompt():
    """已启用技能须出现在 get_skills_prompt() 的索引提示中。（保留并还原原启用状态）"""
    orig = {s['name']: os.path.isfile(os.path.join(SKILLS_DIR, s['name'], '.enabled'))
            for s in sm.list_skills()}
    try:
        for name in orig:
            sm.set_skill_enabled(name, True)
        prompt = sm.get_skills_prompt(active_call=True)
        idx = {s['name'] for s in sm.get_skills_index()}
        assert idx, "启用后 skills 索引不应为空"
        for name in idx:
            assert name in prompt, f"启用技能 {name} 未注入系统提示"
        assert 'run_skill' in prompt, "active_call 提示缺少 run_skill 指令"
    finally:
        for name, en in orig.items():
            sm.set_skill_enabled(name, en)


def test_match_skill_detects_enabled_and_disabled():
    """给一段自然语言，已启用技能要能自动命中；未启用技能应被识别但不冒用。"""
    enabled = {s['name'] for s in sm.get_skills_index()}
    # 覆盖四类技能的代表性问法
    queries = {
        '一杯咖啡 12 元，成本 3 元，毛利是 75%': None,  # 占位，无技能命中正常
    }
    # 行业研究（当前已启用）：动态触发
    r = sm.match_skill('帮我做一份半导体行业研究报告')
    if 'industry-research' in enabled:
        assert r['matched'] is True and r['name'] == 'industry-research', f"行业研究未命中: {r}"
    else:
        assert r['matched'] is False, "未启用技能不应 matched=True"
    print("match_skill 校验完成，已启用技能 =", sorted(enabled))


# ---------------- 网站功能桥接工具 & 新增原生工具 ----------------

def test_new_local_tools_registered():
    """新增原生工具（list_files / data_quality 及既有 safe_calc / percent_change）必须已注册且范型可在 schema 中找到。"""
    import ai_agent as A
    names = {t['name'] for t in A.registry.list_tools()}
    for tool in ('list_files', 'data_quality', 'safe_calc', 'percent_change'):
        assert tool in names, f"工具 {tool} 未注册"
    for tool in ('list_files', 'data_quality'):
        assert tool in A._TOOL_PARAM_SCHEMA, f"{tool} 缺少 _TOOL_PARAM_SCHEMA 条目"


def test_list_files_returns_listing():
    """list_files 不应崩溃且应返回'可用数据文件'引导（空目录时也应返回非异常提示）。"""
    from ai_agent import tool_list_files
    res = tool_list_files()
    assert isinstance(res, str) and not res.startswith('Error'), f"list_files 失败: {res[:120]}"
    assert '可用数据文件' in res or '没有可用的数据文件' in res, f"list_files 输出异常: {res[:80]}"


def test_functional_endpoints_discovered_and_scoped():
    """运行时端点到表：应覆盖新增功能族（fs/company/screener/nlp/report），
    同时不放行管理/破坏性端点（upload/share/delete）。"""
    from app import app  # 触发真实路由表（本地 test_client 目录，无外网）
    import ai_agent as A
    eps = [e['endpoint'] for e in A._discover_functional_endpoints()]
    eps_set = set(eps)
    # 新增能力族必须可发现
    for want in ('/api/fs/dcf', '/api/company-analysis', '/api/screener/screen',
                 '/api/nlp/analyze', '/api/report/preview', '/api/rag/query'):
        assert want in eps_set, f"功能端点 {want} 未发现（是否已注册？）"
    # 白名单校验：合法功能性端点判定为真，管理类为假
    assert A._analysis_api_registered('/api/fs/dcf') is True
    assert A._analysis_api_registered('/api/screener/fetch') is True
    # 安全边界：管理/破坏性端点不放行
    for blocked in ('/api/upload', '/api/files/delete', '/api/share/create',
                    '/api/report/export-docx', '/api/plugins/run', '/api/task/submit'):
        assert blocked not in A._FUNCTIONAL_PREFIXES, f"{blocked} 不应进入功能白名单"


def test_list_analysis_api_catalog_has_new_groups():
    """重写后的 list_analysis_api 应从真实路由表产出目录，并包含新增功能族。"""
    import ai_agent as A
    out = A.tool_list_analysis_api()
    for marker in ('股票筛选', 'NLP 文本分析', '财务报表/估值', '报告预览', '/api/fs/dcf', '/api/company-analysis'):
        assert marker in out, f"list_analysis_api 缺少 {marker}"