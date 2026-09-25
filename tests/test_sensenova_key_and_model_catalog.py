# -*- coding: utf-8 -*-
"""sensenova 凭据链路 + 模型库一致性 + reasoning 空回复兜底 回归测试（2026-09-17）。

本轮的三个真实缺陷（均已在源码侧修复，此处锁定）：

1. **凭据库静默失效**：应用实际解释器 python-portable 缺 cryptography →
   ``secure_config._cipher = None`` → ``_load()`` 抛异常被吞成 ``{}``。
   表现：新 Key 写不进、老 Key 读不到，调用方只看到「未配置」→ 反复 401。
   修复：补装 cryptography + ``_save()`` 拒绝静默丢弃 + 新增 ``credentials_diagnostic()``。

2. **reasoning 系模型空回复**：sensenova 的 glm-5.2 / deepseek-v4-* / 6.8-flash-lite
   把思考过程放 ``reasoning_content``，正文才在 ``content``。当 max_tokens 被推理吃满，
   ``content`` 会是空串；而全链路调用点都只读 ``content`` → 表现为「AI 不回复」。
   修复：``llm_client`` 响应解析层统一兜底（非流式 + 流式各一处）。

3. **默认值指向已删除模型**：glm-4.7-flash 已从模型库移除，若 DEFAULT_LLM_CONFIG /
   PIPE_STAGES 仍指向它会直接 404。修复：默认切到实测可用的 sensenova/glm-5.2，
   并新增「默认模型必须存在于模型库」的前后端一致性断言。
"""
import os
import re
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm_client  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(_ROOT, *parts), encoding='utf-8') as f:
        return f.read()


class _FakeResp:
    def __init__(self, status=200, json_data=None):
        self.status_code = status
        self._json = json_data or {}
        self.text = 'fake'

    def json(self):
        return self._json


def _no_throttle(monkeypatch):
    """跳过云端节流 sleep，缩短测试时长（不影响被测逻辑）。

    注意：2026-09-18 起 `_throttle_cloud(provider, model=None)` 按 (provider, model)
    分桶，调用点传 2 个参数；stub 必须接受不定参数，否则 TypeError 会被上层当作
    "调用失败"吞掉，使 reasoning 兜底断言全数误报失败。
    """
    monkeypatch.setattr(llm_client, '_throttle_cloud', lambda *a, **k: None)


def _chat(message, monkeypatch, provider='sensenova', model='glm-5.2'):
    _no_throttle(monkeypatch)
    resp = _FakeResp(200, {'choices': [{'message': message}]})
    with mock.patch('llm_client.requests.post', return_value=resp):
        return llm_client.chat([{'role': 'user', 'content': 'hi'}],
                               {'provider': provider, 'apiKey': 'k', 'model': model})


# ==========================================================================
# 1. reasoning 兜底（非流式）
# ==========================================================================
def test_empty_content_falls_back_to_reasoning_content(monkeypatch):
    """max_tokens 被推理吃满 → content 为空，必须回退 reasoning_content。"""
    r = _chat({'content': '', 'reasoning_content': '思考后的结论：该指标显著为正。'}, monkeypatch)
    assert r['content'] == '思考后的结论：该指标显著为正。'
    assert r['reasoning_fallback'] is True


def test_blank_content_falls_back_to_reasoning_content(monkeypatch):
    """纯空白 content 同样视为空（上游常返回 '' 或 '\n'）。"""
    r = _chat({'content': '   \n ', 'reasoning_content': '结论正文'}, monkeypatch)
    assert r['content'] == '结论正文'


def test_reasoning_alias_field_supported(monkeypatch):
    """部分模型用 reasoning（而非 reasoning_content）承载思考文本。"""
    r = _chat({'content': '', 'reasoning': '别名字段结论'}, monkeypatch)
    assert r['content'] == '别名字段结论'


def test_reasoning_fallback_skipped_when_tool_calls_present(monkeypatch):
    """有 tool_calls 时空 content 是正常语义（模型决定调工具），不得用 reasoning 覆盖。"""
    msg = {'content': '', 'reasoning_content': '我打算先查数据',
           'tool_calls': [{'function': {'name': 'query_data', 'arguments': '{}'}}]}
    r = _chat(msg, monkeypatch)
    assert r['content'] == ''
    assert len(r['tool_calls']) == 1
    assert 'reasoning_fallback' not in r


def test_nonempty_content_is_untouched(monkeypatch):
    """零回归：正文非空时 reasoning 绝不参与（保持原值）。"""
    r = _chat({'content': '正式回答', 'reasoning_content': '思考过程'}, monkeypatch)
    assert r['content'] == '正式回答'
    assert 'reasoning_fallback' not in r


def test_no_reasoning_and_empty_content_does_not_crash(monkeypatch):
    """两者皆空时保持空串，不得抛错（交由上层兜底提示）。"""
    r = _chat({'content': ''}, monkeypatch)
    assert r['content'] == ''


# ==========================================================================
# 2. reasoning 兜底（流式）
# ==========================================================================
def _stream(lines, monkeypatch, provider='sensenova'):
    _no_throttle(monkeypatch)
    resp = _FakeResp(200)
    resp.iter_lines = lambda decode_unicode=True: iter(lines)
    with mock.patch('llm_client.requests.post', return_value=resp):
        return list(llm_client.chat_stream([{'role': 'user', 'content': 'x'}],
                                           {'provider': provider, 'apiKey': 'k',
                                            'model': 'glm-5.2'}))


def test_stream_falls_back_to_reasoning_when_no_content(monkeypatch):
    """整条流只有 reasoning 增量（推理占满 max_tokens）→ 必须兜底输出，否则前端收到空回复。"""
    lines = [
        'data: {"choices":[{"delta":{"reasoning_content":"推理A"}}]}',
        'data: {"choices":[{"delta":{"reasoning_content":"推理B"}}]}',
        'data: [DONE]',
    ]
    assert _stream(lines, monkeypatch) == ['推理A推理B']


def test_stream_does_not_leak_reasoning_when_content_present(monkeypatch):
    """有正文时 reasoning 不得混入（思考过程不应出现在回答里）。"""
    lines = [
        'data: {"choices":[{"delta":{"reasoning_content":"内部推理"}}]}',
        'data: {"choices":[{"delta":{"content":"正"}}]}',
        'data: {"choices":[{"delta":{"content":"文"}}]}',
        'data: [DONE]',
    ]
    assert _stream(lines, monkeypatch) == ['正', '文']


def test_stream_normal_path_unchanged(monkeypatch):
    """零回归：常规纯正文字流行为不变。"""
    lines = [
        'data: {"choices":[{"delta":{"content":"你"}}]}',
        'data: {"choices":[{"delta":{"content":"好"}}]}',
        'data: [DONE]',
    ]
    assert _stream(lines, monkeypatch) == ['你', '好']


# ==========================================================================
# 3. 模型库与默认值一致性（前后端静态断言）
# ==========================================================================
def _js_provider_models():
    js = _read('static', 'js', 'core.js')
    m = re.search(r'const PROVIDER_MODELS\s*=\s*\{(.*?)\n\};', js, re.S)
    assert m, 'core.js 中未找到 PROVIDER_MODELS'
    out = {}
    for pm in re.finditer(r"(\w+):\s*\[(.*?)\]", m.group(1), re.S):
        out[pm.group(1)] = re.findall(r"id:\s*'([^']+)'", pm.group(2))
    return out


def _js_default_llm_config():
    js = _read('static', 'js', 'core.js')
    m = re.search(r'const DEFAULT_LLM_CONFIG\s*=\s*\{([^}]*)\}', js, re.S)
    assert m, 'core.js 中未找到 DEFAULT_LLM_CONFIG'
    return (re.search(r"provider:\s*'([^']+)'", m.group(1)).group(1),
            re.search(r"model:\s*'([^']+)'", m.group(1)).group(1))


def _js_pipe_stages():
    js = _read('static', 'js', 'core.js')
    m = re.search(r'const PIPE_STAGES\s*=\s*\[(.*?)\];', js, re.S)
    assert m, 'core.js 中未找到 PIPE_STAGES'
    return re.findall(r"defProv:\s*'([^']+)',\s*defModel:\s*'([^']+)'", m.group(1))


def test_model_catalog_has_no_glm_4_7_flash():
    """用户要求：智谱 glm-4.7-flash 从模型库删除。"""
    cat = _js_provider_models()
    assert 'glm-4.7-flash' not in cat.get('zhipu', []), 'glm-4.7-flash 仍在 zhipu 模型库中'
    for prov, models in cat.items():
        assert 'glm-4.7-flash' not in models, f'{prov} 仍残留 glm-4.7-flash'


def test_model_catalog_drops_dead_sensenova_models():
    """端点返回 404 model not found 的模型不得留在可选清单里（用户点了必报错）。"""
    cat = _js_provider_models()
    for dead in ('sensenova-u1-fast', 'sensenova-u1.5-lite', 'sensenova-6.7-flash-lite'):
        assert dead not in cat.get('sensenova', []), f'{dead} 实测 404，应从模型库剔除'


def test_default_llm_config_model_exists_in_catalog():
    """关键回归绊线：默认模型必须真实存在于模型库，否则首次请求即 404。"""
    prov, model = _js_default_llm_config()
    cat = _js_provider_models()
    assert prov in cat, f'默认 provider {prov!r} 不在 PROVIDER_PRESETS/PROVIDER_MODELS 中'
    assert model in cat[prov], f'默认模型 {prov}/{model} 不在模型库 {cat.get(prov)} 中'


def test_pipe_stage_defaults_exist_in_catalog():
    """多Agent四阶段默认模型同样必须存在于模型库。"""
    cat = _js_provider_models()
    pairs = _js_pipe_stages()
    assert len(pairs) == 4, f'PIPE_STAGES 应有 4 个阶段默认，实得 {pairs}'
    for prov, model in pairs:
        assert prov in cat, f'阶段默认 provider {prov!r} 不在模型库'
        assert model in cat[prov], f'阶段默认模型 {prov}/{model} 不在模型库 {cat.get(prov)}'


def test_backend_and_frontend_defaults_aligned():
    """后端 _PROVIDER_DEFAULT_MODELS 与前端 DEFAULT_LLM_CONFIG 必须指向同一模型。"""
    prov, model = _js_default_llm_config()
    assert llm_client._PROVIDER_DEFAULT_MODELS[prov] == model, (
        f'后端默认 {prov}/{llm_client._PROVIDER_DEFAULT_MODELS.get(prov)} '
        f'与前端默认 {prov}/{model} 不一致')


def test_html_default_provider_matches_js():
    """#llm-provider 的 selected 必须等于 core.js 默认 provider（防误导性 UI）。"""
    html = _read('templates', 'tabs', 'tab-ai-assistant.html')
    m_sel = re.search(r'<select[^>]*id="llm-provider".*?</select>', html, re.S)
    assert m_sel
    selected = re.search(r'<option\s+value="([^"]+)"\s+selected', m_sel.group(0))
    assert selected, '#llm-provider 无 selected 选项'
    prov, _ = _js_default_llm_config()
    assert selected.group(1) == prov


def test_html_quick_select_has_no_deprecated_models():
    """顶部快捷选择器不得残留已删除的模型选项。"""
    html = _read('templates', 'tabs', 'tab-ai-assistant.html')
    m_sel = re.search(r'<select[^>]*id="ai-model-select".*?</select>', html, re.S)
    assert m_sel, '模板中未找到 #ai-model-select'
    values = re.findall(r'<option\s+value="([^"]+)"', m_sel.group(0))
    for dead in ('zhipu_glm-4.7-flash', 'sensenova_sensenova-u1-fast',
                 'sensenova_sensenova-u1.5-lite'):
        assert dead not in values, f'快捷选择器仍残留 {dead}'


def test_quick_select_options_all_exist_in_catalog():
    """快捷选择器的每个选项都必须能在模型库里找到对应条目。"""
    html = _read('templates', 'tabs', 'tab-ai-assistant.html')
    m_sel = re.search(r'<select[^>]*id="ai-model-select".*?</select>', html, re.S)
    cat = _js_provider_models()
    for val in re.findall(r'<option\s+value="([^"]+)"', m_sel.group(0)):
        if '_' not in val:
            continue
        prov, model = val.split('_', 1)
        if prov == 'custom':
            continue
        assert model in cat.get(prov, []), f'快捷选项 {val} 对应的模型不在模型库中'


# ==========================================================================
# 4. 凭据库诊断（cryptography 缺失不再静默）
# ==========================================================================
def test_credentials_diagnostic_shape():
    import secure_config as sc
    d = sc.credentials_diagnostic()
    for k in ('backend_ok', 'file_exists', 'decrypt_error', 'providers', 'hint'):
        assert k in d, f'诊断信息缺字段 {k}'
    assert isinstance(d['backend_ok'], bool)
    assert isinstance(d['providers'], list)


def test_set_llm_api_key_rejects_empty():
    """空 Key 必须显式报错，不得悄悄写入空值。"""
    import secure_config as sc
    import pytest
    with pytest.raises(ValueError):
        sc.set_llm_api_key('sensenova', '')


def test_save_raises_when_cipher_missing(monkeypatch):
    """加密后端不可用时 _save 必须抛错（早期实现会静默丢弃 Key）。"""
    import secure_config as sc
    import pytest
    monkeypatch.setattr(sc.secure_config, '_cipher', None, raising=False)
    with pytest.raises(RuntimeError):
        sc.secure_config._save()
