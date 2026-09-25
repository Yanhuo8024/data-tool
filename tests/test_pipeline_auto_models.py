#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多Agent协作「自动选择大模型」回归测试（2026-09-17）。

覆盖：
- 可用 provider 探测（凭据来源、优先级、排除项）
- 阶段间轮换分配 / 单 provider 退化 / 空凭据返回 None
- 路由层 `pipeline_auto` 优先级与回退
- 前端接线（静态断言：模板开关 + core.js 请求体字段）

隔离约定：全部通过 monkeypatch 安装 stub（不写模块级全局 stub，避免污染 pytest session）；
环境变量按 provider 逐一 delenv 后按需注入，保证与真实 .env 无关、可重复。
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm_client as L  # noqa: E402
from agent_pipeline import PIPELINE_STAGE_NAMES, resolve_auto_pipeline_models  # noqa: E402

try:
    import secure_config as _SC
except Exception:  # pragma: no cover - 凭据库可选
    _SC = None


@pytest.fixture(autouse=True)
def _isolate_credentials(monkeypatch):
    """清空所有 provider 的环境变量，并屏蔽加密凭据库 → 从"零凭据"开始，按用例注入。"""
    for env_name in set(L._ENV_KEY_NAMES.values()):
        monkeypatch.delenv(env_name, raising=False)
    for prov in L._PROVIDER_URLS:
        monkeypatch.delenv(f"{prov.upper()}_MODEL", raising=False)
    if _SC is not None:
        monkeypatch.setattr(_SC, 'get_llm_api_key', lambda p: None, raising=False)
    yield


def _set(monkeypatch, **envs):
    for name, val in envs.items():
        monkeypatch.setenv(name, val)


# --------------------------------------------------------------------------
# 1. 可用 provider 探测
# --------------------------------------------------------------------------
def test_no_credentials_returns_empty():
    assert L.list_available_providers() == []


def test_detects_env_key(monkeypatch):
    _set(monkeypatch, ZHIPU_API_KEY='zk-test')
    avail = L.list_available_providers()
    assert [p['provider'] for p in avail] == ['zhipu']
    assert avail[0]['source'] == 'env'
    assert avail[0]['model'] == 'glm-4-flash'  # glm-4.7-flash 已于 2026-09-17 从模型库移除


def test_priority_order_free_first(monkeypatch):
    """优先级：zhipu → groq → dashscope（与 _AUTO_PROVIDER_PRIORITY 一致）。"""
    _set(monkeypatch, DASHSCOPE_API_KEY='d', GROQ_API_KEY='g', ZHIPU_API_KEY='z')
    assert [p['provider'] for p in L.list_available_providers()] == ['zhipu', 'groq', 'dashscope']


def test_groq_env_name_registered():
    """groq 曾被 _ENV_KEY_NAMES 遗漏，导致自动选择漏掉它 —— 锁定该修复。"""
    assert L._ENV_KEY_NAMES.get('groq') == 'GROQ_API_KEY'


def test_excludes_ollama_custom_azure_minimax(monkeypatch):
    """ollama（禁止自动拉起）/ custom / azure（需自定义 URL）/ minimax（sensenova 别名）永不入选。"""
    _set(monkeypatch, SENSENOVA_API_KEY='s')   # minimax 与 sensenova 共用该 env
    picked = [p['provider'] for p in L.list_available_providers()]
    assert 'sensenova' in picked
    for banned in ('ollama', 'custom', 'azure', 'minimax'):
        assert banned not in picked


def test_env_model_override(monkeypatch):
    _set(monkeypatch, ZHIPU_API_KEY='zk', ZHIPU_MODEL='glm-custom-x')
    assert L.list_available_providers()[0]['model'] == 'glm-custom-x'


# --------------------------------------------------------------------------
# 2. 自动分配
# --------------------------------------------------------------------------
def test_single_provider_all_stages(monkeypatch):
    _set(monkeypatch, ZHIPU_API_KEY='zk')
    auto = resolve_auto_pipeline_models(None)
    assert set(auto) == set(PIPELINE_STAGE_NAMES)
    assert all(v == {'provider': 'zhipu', 'model': 'glm-4-flash'} for v in auto.values())


def test_rotation_across_providers(monkeypatch):
    """多 provider 时按阶段轮换 —— 这是"多模型协作 + 摊薄 429"的核心行为。"""
    _set(monkeypatch, ZHIPU_API_KEY='z', GROQ_API_KEY='g', DASHSCOPE_API_KEY='d')
    auto = resolve_auto_pipeline_models(None)
    got = [auto[n]['provider'] for n in PIPELINE_STAGE_NAMES]
    assert got == ['zhipu', 'groq', 'dashscope', 'zhipu']


def test_stage_model_pref_applied_on_sensenova(monkeypatch):
    """用户指定（2026-09-18）：三阶段分别钉扎三个不同模型（并行执行）。"""
    _set(monkeypatch, SENSENOVA_API_KEY='s')
    auto = resolve_auto_pipeline_models(None)
    assert auto['市场分析师'] == {'provider': 'sensenova', 'model': 'glm-5.2'}
    assert auto['量化建模师'] == {'provider': 'sensenova', 'model': 'sensenova-6.8-flash-lite'}
    assert auto['风控合规官'] == {'provider': 'sensenova', 'model': 'deepseek-v4-flash'}
    # 终稿合成不钉扎，沿用 provider 默认模型
    assert auto['终稿合成'] == {'provider': 'sensenova', 'model': 'glm-5.2'}


def test_three_analysis_stages_get_distinct_models(monkeypatch):
    """核心诉求（2026-09-18）：三个分析阶段必须拿到三个**不同**的模型，配合并行提速。"""
    _set(monkeypatch, SENSENOVA_API_KEY='s')
    auto = resolve_auto_pipeline_models(None)
    models = [auto[n]['model'] for n in ('市场分析师', '量化建模师', '风控合规官')]
    assert len(set(models)) == 3, f'三阶段模型必须互不相同，实际 {models}'


def test_stage_model_pref_pins_across_rotation(monkeypatch):
    """钉扎优先于轮换：三阶段被轮换到其他 provider 时，也改钉回 sensenova 各自模型。"""
    _set(monkeypatch, SENSENOVA_API_KEY='s', GROQ_API_KEY='g')
    auto = resolve_auto_pipeline_models(None)
    assert auto['量化建模师'] == {'provider': 'sensenova', 'model': 'sensenova-6.8-flash-lite'}
    assert auto['市场分析师'] == {'provider': 'sensenova', 'model': 'glm-5.2'}
    assert auto['风控合规官'] == {'provider': 'sensenova', 'model': 'deepseek-v4-flash'}


def test_stage_model_pref_user_scenario_sensenova_zhipu(monkeypatch):
    """用户实测场景（2026-09-18 截图）：sensenova+智谱双 provider，
    轮换曾把量化建模师分到 glm-4-flash——钉扎后必须固定 sensenova/sensenova-6.8-flash-lite。
    """
    _set(monkeypatch, SENSENOVA_API_KEY='s', ZHIPU_API_KEY='z')
    auto = resolve_auto_pipeline_models(None)
    assert auto['量化建模师'] == {'provider': 'sensenova', 'model': 'sensenova-6.8-flash-lite'}
    # 三阶段钉扎 + 终稿固定首选 → 四阶段 provider 均为 sensenova（zhipu 仅在有阶段轮换时使用）
    assert [auto[n]['provider'] for n in PIPELINE_STAGE_NAMES] == ['sensenova'] * len(PIPELINE_STAGE_NAMES)
    assert auto['市场分析师']['model'] == 'glm-5.2'
    # 终稿合成固定用首选 provider（sensenova）的模型，不参与轮换（2026-09-18 回归修复）
    assert auto['终稿合成'] == {'provider': 'sensenova', 'model': 'glm-5.2'}


def test_final_stage_prefers_first_provider(monkeypatch):
    """回归（2026-09-18 实测）：sensenova+zhipu 双 provider 时，轮换曾把终稿合成
    分到 zhipu/glm-4-flash（最弱模型），而旧版单 provider 环境终稿一直是 glm-5.2。
    终稿是最终交付物，必须固定用首选 provider 的模型，只在中间三阶段做轮换。
    """
    _set(monkeypatch, SENSENOVA_API_KEY='s', ZHIPU_API_KEY='z')
    auto = resolve_auto_pipeline_models(None)
    assert auto['终稿合成'] == {'provider': 'sensenova', 'model': 'glm-5.2'}
    # 中间三阶段仍各自独立（钉扎生效），轮换价值不受影响
    assert len({auto[n]['model'] for n in ('市场分析师', '量化建模师', '风控合规官')}) == 3


def test_final_stage_follows_base_preference(monkeypatch):
    """全局首选 provider 为 zhipu 时，终稿应落到 zhipu（ordered[0]），而非固定写死 sensenova。"""
    _set(monkeypatch, SENSENOVA_API_KEY='s', ZHIPU_API_KEY='z')
    auto = resolve_auto_pipeline_models({'provider': 'zhipu', 'model': 'glm-4-flash'})
    assert auto['终稿合成'] == {'provider': 'zhipu', 'model': 'glm-4-flash'}


def test_stage_model_pref_skipped_when_provider_unavailable(monkeypatch):
    """sensenova 不可用时不钉扎——不把 sensenova-6.8-flash-lite 硬塞给没有该模型的 provider。"""
    _set(monkeypatch, ZHIPU_API_KEY='z')
    auto = resolve_auto_pipeline_models(None)
    assert auto['量化建模师'] == {'provider': 'zhipu', 'model': 'glm-4-flash'}


def test_no_available_returns_none():
    assert resolve_auto_pipeline_models(None) is None


def test_base_provider_promoted_and_model_kept(monkeypatch):
    """全局已选 provider 可用时提到首位，并沿用其指定 model。"""
    _set(monkeypatch, ZHIPU_API_KEY='z', GROQ_API_KEY='g')
    auto = resolve_auto_pipeline_models({'provider': 'groq', 'model': 'llama-3.3-70b'})
    assert auto[PIPELINE_STAGE_NAMES[0]] == {'provider': 'groq', 'model': 'llama-3.3-70b'}


def test_base_provider_unavailable_ignored(monkeypatch):
    """全局选了一个没有凭据的 provider（如已失效的 sensenova）时，不应拖垮自动分配。"""
    _set(monkeypatch, ZHIPU_API_KEY='z')
    auto = resolve_auto_pipeline_models({'provider': 'sensenova', 'model': 'kimi-k3'})
    assert all(v['provider'] == 'zhipu' for v in auto.values())


def test_malformed_base_cfg_zero_crash(monkeypatch):
    _set(monkeypatch, ZHIPU_API_KEY='z')
    for bad in (None, {}, {'provider': None}, {'provider': ''}, {'provider': 'zhipu'}):
        auto = resolve_auto_pipeline_models(bad)
        assert auto is not None and len(auto) == len(PIPELINE_STAGE_NAMES)


def test_stage_names_include_final(monkeypatch):
    """终稿合成必须参与分配（与 routes._PIPELINE_STAGE_NAMES 对齐）。"""
    assert '终稿合成' in PIPELINE_STAGE_NAMES


# --------------------------------------------------------------------------
# 3. 路由层：pipeline_auto 优先级与回退
# --------------------------------------------------------------------------
def _load_route_helpers():
    import routes.ai_assistant_routes as R
    return R


def test_route_auto_preferred_over_explicit(monkeypatch):
    R = _load_route_helpers()
    _set(monkeypatch, ZHIPU_API_KEY='z')
    data = {
        'pipeline_auto': True,
        'pipeline_models': {'市场分析师': {'provider': 'groq', 'model': 'llama-3.1-70b-versatile'}},
    }
    out = R._resolve_pipeline_models(data, None)
    assert out['市场分析师']['provider'] == 'zhipu'   # 自动结果胜出


def test_route_explicit_used_when_auto_off(monkeypatch):
    R = _load_route_helpers()
    _set(monkeypatch, ZHIPU_API_KEY='z')
    data = {'pipeline_models': {'市场分析师': {'provider': 'groq', 'model': 'llama-3.1-70b-versatile'}}}
    out = R._resolve_pipeline_models(data, None)
    assert out == {'市场分析师': {'provider': 'groq', 'model': 'llama-3.1-70b-versatile'}}


def test_route_auto_falls_back_when_no_credentials(monkeypatch):
    R = _load_route_helpers()
    data = {
        'pipeline_auto': True,
        'pipeline_models': {'市场分析师': {'provider': 'groq', 'model': 'llama-3.1-70b-versatile'}},
    }
    out = R._resolve_pipeline_models(data, None)
    assert out == {'市场分析师': {'provider': 'groq', 'model': 'llama-3.1-70b-versatile'}}


def test_route_auto_none_when_nothing(monkeypatch):
    R = _load_route_helpers()
    assert R._resolve_pipeline_models({'pipeline_auto': True}, None) is None


def test_route_stage_names_match_pipeline():
    R = _load_route_helpers()
    assert set(R._PIPELINE_STAGE_NAMES) == set(PIPELINE_STAGE_NAMES)


def test_auto_endpoint_registered():
    """自动分配预览接口已挂到 AI 助手蓝图（源码级断言，避免误删路由）。"""
    R = _load_route_helpers()
    src = open(R.__file__, encoding='utf-8').read()
    assert "/api/assistant/pipeline-auto-models" in src
    assert "def api_pipeline_auto_models" in src


# --------------------------------------------------------------------------
# 4. 前端接线（静态断言：避免前端改动漏接）
# --------------------------------------------------------------------------
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(_ROOT, rel), encoding='utf-8') as f:
        return f.read()


def test_frontend_template_has_auto_toggle():
    html = _read(os.path.join('templates', 'tabs', 'tab-ai-assistant.html'))
    assert 'id="pipeline-auto"' in html
    assert 'id="ai-pipe-auto-summary"' in html
    assert 'id="ai-pipe-manual"' in html
    assert 'onPipelineAutoChange()' in html


def test_frontend_js_persists_auto_flag():
    js = _read(os.path.join('static', 'js', 'core.js'))
    assert 'pipeline_auto' in js                      # 请求体字段
    assert re.search(r"auto:\s*auto", js)             # 持久化 auto
    assert 'refreshAutoPipelineSummary' in js
    assert '/api/assistant/pipeline-auto-models' in js
    # 新用户（或旧版本存档重置）默认开启自动模式； stages 清空走新默认
    assert re.search(r"enabled:\s*saved \? !!saved\.enabled : true", js)
    assert re.search(r"typeof saved\.auto === 'boolean' \? saved\.auto : true", js)
    assert 'stages: {}' in js


def test_auto_summary_renders_pinned_marker():
    """自动分配面板必须展示钉扎标记（📌 + 图例）——
    用户指定量化建模师固定 sensenova-6.8-flash-lite，前端需可辨识（2026-09-18）。
    """
    js = _read(os.path.join('static', 'js', 'core.js'))
    assert "d.pinned || {}" in js                     # 读取钉扎标记
    assert "pinned[n] ? ' 📌' : ''" in js             # 阶段行渲染 📌
    assert '优先于轮换分配' in js                      # 图例说明
    routes_src = _read(os.path.join('routes', 'ai_assistant_routes.py'))
    assert "'pinned'" in routes_src, '自动分配接口未返回钉扎标记'
