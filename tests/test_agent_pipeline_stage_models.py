# -*- coding: utf-8 -*-
"""多Agent协作 · 分阶段分模型 —— 单测

覆盖 2026-09-14 从 F 盘副本合并进 D 盘的「AI 模式重设计 + 分阶段分模型」功能簇：

  1) agent_pipeline._stage_llm_config        零回归 / 同 provider 换模型 / 换 provider 清凭证
  2) agent_pipeline.run_pipeline_stream      pipeline_models 逐阶段生效 + status 事件透出 provider/model
  3) routes.ai_assistant_routes._sanitize_pipeline_models   外部输入清洗（白名单 / 丢非法 / 剥离凭证）
  4) /api/assistant/chat 路由接线            pipeline_models 确实透传到 stream_chat_message
  5) llm_client 429 缓解参数                 节流间隔 / kimi-k3 温度约束 / 默认模型
  6) 跨文件一致性                            前端 PIPE_STAGES / 模板 DOM id ↔ 后端阶段名

设计原则：全部离线，不发起任何真实 LLM 调用（monkeypatch 掉 llm_client.chat 与 ai_agent.agent_loop）。
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 与前端 PROVIDER 默认值一致的全阶段基础配置（含凭证，用于验证"换 provider 必须清凭证"）
BASE_CFG = {
    'provider': 'sensenova',
    'model': 'kimi-k3',
    'apiKey': 'SECRET-KEY-SHOULD-NOT-LEAK',
    'apiUrl': 'https://example.invalid/v1/chat/completions',
    'temperature': 0.3,
}

# 四个阶段分别指定不同 provider（跨 Provider 轮换，规避单点 429）
PIPE_MODELS = {
    '市场分析师': {'provider': 'sensenova', 'model': 'glm-5.2'},   # 同 provider，仅换模型
    '量化建模师': {'provider': 'zhipu', 'model': 'glm-4.7-flash'},  # 换 provider
    '风控合规官': {'provider': 'groq', 'model': 'llama-3.1-70b-versatile'},
    '终稿合成': {'provider': 'dashscope', 'model': 'qwen-turbo'},
}


# ============================================================
# 1) _stage_llm_config —— 配置派生
# ============================================================

class TestStageLlmConfig:

    def test_none_map_equals_base(self):
        """pipeline_models=None → 该阶段配置与全局完全等价（老路径零回归）"""
        from agent_pipeline import _stage_llm_config
        for stage in ('市场分析师', '量化建模师', '风控合规官', '终稿合成'):
            assert _stage_llm_config(BASE_CFG, stage, None) == BASE_CFG

    def test_empty_map_equals_base(self):
        from agent_pipeline import _stage_llm_config
        assert _stage_llm_config(BASE_CFG, '市场分析师', {}) == BASE_CFG

    def test_unlisted_stage_equals_base(self):
        """配置里没有的阶段 → 沿用全局"""
        from agent_pipeline import _stage_llm_config
        for stage in ('风控合规官2', '不存在的阶段', '', 'DROP'):
            assert _stage_llm_config(BASE_CFG, stage, PIPE_MODELS) == BASE_CFG

    def test_non_dict_pm_map_does_not_crash(self):
        """pm_map 本身不是 dict（脏入参）→ 回落 base，不得抛异常"""
        from agent_pipeline import _stage_llm_config
        for bad_map in ([], 'str', 123, True, {'市场分析师': 'not-a-dict'},
                        {'市场分析师': None}, {'市场分析师': 456}):
            assert _stage_llm_config(BASE_CFG, '市场分析师', bad_map) == BASE_CFG

    def test_returns_shallow_copy_not_same_object(self):
        """必须是副本，避免阶段改写污染全局 llm_config"""
        from agent_pipeline import _stage_llm_config
        out = _stage_llm_config(BASE_CFG, '市场分析师', None)
        assert out is not BASE_CFG
        out['model'] = 'MUTATED'
        assert BASE_CFG['model'] == 'kimi-k3'

    def test_same_provider_only_switches_model(self):
        """同 provider → 只换 model，凭证原样保留"""
        from agent_pipeline import _stage_llm_config
        out = _stage_llm_config(BASE_CFG, '市场分析师', PIPE_MODELS)
        assert out['provider'] == 'sensenova'
        assert out['model'] == 'glm-5.2'
        assert out['apiKey'] == BASE_CFG['apiKey']
        assert out['apiUrl'] == BASE_CFG['apiUrl']
        assert out['temperature'] == 0.3

    def test_different_provider_clears_credentials(self):
        """换 provider → 必须清空 apiUrl/apiKey，交由 llm_client 用官方端点+服务端密钥补全"""
        from agent_pipeline import _stage_llm_config
        out = _stage_llm_config(BASE_CFG, '量化建模师', PIPE_MODELS)
        assert out['provider'] == 'zhipu'
        assert out['model'] == 'glm-4.7-flash'
        assert out['apiUrl'] == ''
        assert out['apiKey'] == ''

    def test_malformed_entry_falls_back_to_base(self):
        """残缺配置（缺 provider 或缺 model）→ 不生效，回落全局"""
        from agent_pipeline import _stage_llm_config
        for bad in ({'market': 1}, {'provider': '', 'model': 'x'}, {'provider': 'zhipu'},
                    {'provider': 'zhipu', 'model': ''}, 'not-a-dict', 123, None):
            assert _stage_llm_config(BASE_CFG, '市场分析师', {'市场分析师': bad}) == BASE_CFG

    def test_none_base_does_not_crash(self):
        from agent_pipeline import _stage_llm_config
        assert isinstance(_stage_llm_config(None, '市场分析师', None), dict)
        assert isinstance(_stage_llm_config(None, '市场分析师', PIPE_MODELS), dict)

    def test_api_key_never_leaks_on_provider_switch(self):
        """安全不变式：换 provider 后阶段配置里不得残留原 provider 的密钥"""
        from agent_pipeline import _stage_llm_config
        out = _stage_llm_config(BASE_CFG, '终稿合成', PIPE_MODELS)
        assert 'SECRET-KEY-SHOULD-NOT-LEAK' not in str(out.values())


# ============================================================
# 2) run_pipeline_stream —— 逐阶段生效
# ============================================================

def _drive_pipeline(monkeypatch, message, base_cfg, pipeline_models):
    """跑一次管线（离线）：stub 掉 agent_loop 与 chat，回收各阶段实际使用的 llm_config"""
    import agent_pipeline
    import ai_agent
    import llm_client

    stage_cfgs, final_cfgs = [], []

    def fake_agent_loop(user_message, llm_config=None, file_name=None,
                        max_iterations=3, session_id=None, progress_callback=None,
                        trace_run_id=None, **kw):
        stage_cfgs.append(dict(llm_config or {}))
        if progress_callback:
            progress_callback({'type': 'tool_end', 'tool': 'web_search',
                               'tool_status': 'ok', 'elapsed_ms': 5, 'params': {}})
        return {'ok': True, 'output': f'第{len(stage_cfgs)}阶段产出', 'calls': [],
                'usage': {'total_tokens': 1}, 'elapsed_ms': 1}

    def fake_chat(messages, config=None, **kw):
        final_cfgs.append(dict(config or {}))
        return {'content': '# 终稿\n首席合成结果'}

    monkeypatch.setattr(ai_agent, 'agent_loop', fake_agent_loop)
    monkeypatch.setattr(llm_client, 'chat', fake_chat)

    events, done = [], None
    for evt in agent_pipeline.run_pipeline_stream(
            message, base_cfg, None, session_id='unit', pipeline_models=pipeline_models):
        if evt.get('type') == 'done':
            done = evt['data']
        else:
            events.append(evt)
    return stage_cfgs, final_cfgs, events, done


class TestRunPipelineStageModels:

    def test_stages_use_their_own_models(self, monkeypatch):
        stage_cfgs, final_cfgs, events, done = _drive_pipeline(
            monkeypatch, '全面分析宁德时代', BASE_CFG, PIPE_MODELS)

        assert len(stage_cfgs) == 3, f'应执行 3 个阶段，实际 {len(stage_cfgs)}'

        # 三阶段并行执行（2026-09-18），完成/记录顺序不定 → 按 provider 查找断言
        by_provider = {c.get('provider'): c for c in stage_cfgs}

        # 阶段 1：同 provider 换模型，凭证保留
        assert by_provider['sensenova']['model'] == 'glm-5.2'
        assert by_provider['sensenova']['apiKey'] == BASE_CFG['apiKey']

        # 阶段 2：换 provider，凭证被清空
        assert by_provider['zhipu']['model'] == 'glm-4.7-flash'
        assert by_provider['zhipu']['apiKey'] == '' and by_provider['zhipu']['apiUrl'] == ''

        # 阶段 3：换 provider
        assert by_provider['groq']['model'] == 'llama-3.1-70b-versatile'

        # 终稿合成走独立配置
        assert len(final_cfgs) == 1
        assert final_cfgs[0]['provider'] == 'dashscope'
        assert final_cfgs[0]['model'] == 'qwen-turbo'

        # status 事件透出当前 provider/model，便于用户看到"正在用哪个模型"
        statuses = [e.get('message', '') for e in events if e.get('type') == 'status']
        assert any('sensenova/glm-5.2' in s for s in statuses), statuses
        assert any('zhipu/glm-4.7-flash' in s for s in statuses), statuses
        assert any('groq/llama-3.1-70b-versatile' in s for s in statuses), statuses

        assert done['source'] == 'agent_pipeline'
        assert '首席合成' in done['reply']

    def test_zero_regression_without_pipeline_models(self, monkeypatch):
        """不传 pipeline_models → 全部阶段（含终稿）沿用全局配置"""
        stage_cfgs, final_cfgs, _events, done = _drive_pipeline(
            monkeypatch, '全面分析宁德时代', BASE_CFG, None)

        assert len(stage_cfgs) == 3
        for cfg in stage_cfgs:
            assert cfg == BASE_CFG, f'阶段配置偏离全局：{cfg}'
        # 终稿合成额外关闭思考（2026-09-18 晚修复：glm-5.2 等 reasoning 系不关思考会吃满
        # max_tokens，正文只剩几十字）；其余字段仍逐字节沿用全局配置。
        assert final_cfgs and final_cfgs[0] == dict(BASE_CFG, reasoning_effort='none')
        assert done['source'] == 'agent_pipeline'

    def test_partial_config_only_affects_listed_stage(self, monkeypatch):
        """只配了 1 个阶段 → 其余阶段沿用全局（并行下完成顺序不定，按 provider 判别）"""
        partial = {'量化建模师': {'provider': 'zhipu', 'model': 'glm-4.7-flash'}}
        stage_cfgs, final_cfgs, _events, _done = _drive_pipeline(
            monkeypatch, '全面分析电器行业', BASE_CFG, partial)

        assert len(stage_cfgs) == 3
        overridden = [c for c in stage_cfgs if c.get('provider') == 'zhipu']
        kept = [c for c in stage_cfgs if c.get('provider') != 'zhipu']
        assert len(overridden) == 1 and overridden[0]['provider'] == 'zhipu'  # 量化建模师 → 覆盖
        assert all(c == BASE_CFG for c in kept)                              # 其余两阶段 → 全局
        assert final_cfgs[0] == dict(BASE_CFG, reasoning_effort='none')  # 终稿合成 → 全局 + 关思考

    def test_backward_compatible_signature(self):
        """既有调用方（不传 pipeline_models）签名不变"""
        import inspect
        from agent_pipeline import run_pipeline_stream
        params = inspect.signature(run_pipeline_stream).parameters
        assert 'pipeline_models' in params
        assert params['pipeline_models'].default is None


# ============================================================
# 3) _sanitize_pipeline_models —— 外部输入清洗
# ============================================================

class TestSanitizePipelineModels:

    @staticmethod
    def _fn():
        from routes.ai_assistant_routes import _sanitize_pipeline_models
        return _sanitize_pipeline_models

    def test_rejects_falsy_and_non_dict(self):
        sp = self._fn()
        for bad in (None, {}, [], '', 0, 'str', 123, {'市场分析师': 'not-a-dict'}):
            assert sp(bad) is None, f'应拒绝：{bad!r}'

    def test_accepts_valid_config(self):
        sp = self._fn()
        out = sp({'市场分析师': {'provider': 'sensenova', 'model': 'kimi-k3'}})
        assert out == {'市场分析师': {'provider': 'sensenova', 'model': 'kimi-k3'}}

    def test_drops_unknown_stage_names(self):
        """阶段名白名单：防注入任意 key"""
        sp = self._fn()
        assert sp({'乱写的阶段': {'provider': 'zhipu', 'model': 'm'}}) is None
        assert sp({'市场分析师': {'provider': 'zhipu', 'model': 'm'},
                   'DROP TABLE': {'provider': 'zhipu', 'model': 'm'}}) is not None

    def test_invalid_provider_falls_back_to_sensenova(self):
        sp = self._fn()
        out = sp({'市场分析师': {'provider': 'evil-provider', 'model': 'm'}})
        assert out['市场分析师']['provider'] == 'sensenova'

    def test_provider_case_normalized(self):
        sp = self._fn()
        out = sp({'市场分析师': {'provider': 'ZhiPu', 'model': 'm'}})
        assert out['市场分析师']['provider'] == 'zhipu'

    def test_blank_model_dropped(self):
        """空/None/非字符串 model 一律丢弃（JSON null 不得变成 'None' 模型名）"""
        sp = self._fn()
        for blank in ('', '   ', '\t', None, 123, 0, True, [], {}):
            assert sp({'市场分析师': {'provider': 'zhipu', 'model': blank}}) is None, \
                f'model={blank!r} 应被丢弃'

    def test_none_model_must_not_become_string_None(self):
        """回归：str(None) == 'None' 曾被当作合法模型名放行"""
        sp = self._fn()
        out = sp({'市场分析师': {'provider': 'zhipu', 'model': None}})
        assert out is None, f'应返回 None，实际 {out!r}'

    def test_no_credential_passthrough(self):
        """安全不变式：前端塞进来的 apiUrl/apiKey 一律不得透传到下游"""
        sp = self._fn()
        out = sp({'市场分析师': {'provider': 'zhipu', 'model': 'm',
                                'apiUrl': 'http://169.254.169.254/latest/meta-data',
                                'apiKey': 'STOLEN'}})
        entry = out['市场分析师']
        assert set(entry) == {'provider', 'model'}, f'不应含多余字段：{entry}'
        assert 'STOLEN' not in str(out)
        assert '169.254' not in str(out)

    def test_mixed_valid_and_invalid(self):
        sp = self._fn()
        out = sp({
            '市场分析师': {'provider': 'sensenova', 'model': 'kimi-k3'},
            '隔壁老王': {'provider': 'zhipu', 'model': 'x'},
            '量化建模师': {'provider': 'zhipu', 'model': '  '},
            '风控合规官': {'provider': 'groq', 'model': 'llama-3.1-70b-versatile'},
        })
        assert set(out) == {'市场分析师', '风控合规官'}


# ============================================================
# 4) /api/assistant/chat 路由接线
# ============================================================

class TestChatRouteWiring:

    def test_pipeline_models_reaches_stream_chat_message(self, client, admin_headers, monkeypatch):
        """POST /api/assistant/chat(stream=true) 时，清洗后的配置必须透传到 stream_chat_message"""
        import ai_assistant

        seen = {}

        def fake_stream(message, llm_config=None, current_file=None, mode='think',
                        images=None, session_id=None, pipeline_models=None, **kw):
            seen['pipeline_models'] = pipeline_models
            seen['mode'] = mode
            yield {'type': 'done', 'data': {'ok': True, 'reply': 'stub', 'source': 'stub'}}

        monkeypatch.setattr(ai_assistant, 'stream_chat_message', fake_stream, raising=False)

        resp = client.post('/api/assistant/chat',
                           json={'message': '做一份研究报告', 'stream': True, 'mode': 'think',
                                 'pipeline_models': {
                                     '市场分析师': {'provider': 'sensenova', 'model': 'kimi-k3'},
                                     '非法阶段': {'provider': 'zhipu', 'model': 'x'},
                                     '量化建模师': {'provider': 'zhipu', 'model': 'glm-4.7-flash',
                                                    'apiKey': 'STOLEN'},
                                 }},
                           headers=admin_headers)
        if resp.status_code in (401, 403):
            pytest.skip('该路由需要认证，当前测试环境未取到 token')

        assert resp.status_code == 200, resp.get_data(as_text=True)[:400]
        resp.get_data()  # 触发 SSE 生成器
        assert seen.get('pipeline_models') == {
            '市场分析师': {'provider': 'sensenova', 'model': 'kimi-k3'},
            '量化建模师': {'provider': 'zhipu', 'model': 'glm-4.7-flash'},
        }, seen

    def test_absent_pipeline_models_passes_none(self, client, admin_headers, monkeypatch):
        """未开启多Agent分模型 → 传 None，下游完全走全局配置"""
        import ai_assistant

        seen = {}

        def fake_stream(message, llm_config=None, current_file=None, mode='think',
                        images=None, session_id=None, pipeline_models=None, **kw):
            seen['pipeline_models'] = pipeline_models
            yield {'type': 'done', 'data': {'ok': True, 'reply': 'stub'}}

        monkeypatch.setattr(ai_assistant, 'stream_chat_message', fake_stream, raising=False)
        resp = client.post('/api/assistant/chat',
                           json={'message': '你好', 'stream': True},
                           headers=admin_headers)
        if resp.status_code in (401, 403):
            pytest.skip('该路由需要认证，当前测试环境未取到 token')
        assert seen.get('pipeline_models') is None


# ============================================================
# 5) llm_client —— 429 缓解参数
# ============================================================

class TestLlmClientRateLimitTuning:

    def test_cloud_throttle_is_adaptive_not_fixed(self):
        """2026-09-17 起节流改为**自适应**，不再是固定 8s。

        固定间隔的问题：agent 多轮调用是串行消费的，健康态每轮空等
        (间隔 - 请求耗时)。实测 4 轮 × 净请求 2.5s 时空等 16.5s，与模型快慢无关。
        现行设计：基础间隔轻量 → 仅在收到 429 时按 provider 指数抬升（封顶）
        → 连续成功后衰减回基础值。
        """
        import llm_client as lc
        KEY = '_t_throttle_probe'
        try:
            assert lc._CLOUD_MIN_INTERVAL <= 3.0, '健康态基础间隔应轻量（旧值 8.0 已废弃）'
            assert lc._CLOUD_MAX_INTERVAL >= lc._CLOUD_MIN_INTERVAL
            # 未被限流 → 无动态档，间隔即基础值
            lc._ADAPT_INTERVAL.pop(KEY, None)
            assert lc._current_interval(KEY) == lc._CLOUD_MIN_INTERVAL
            # 429 抬升
            lc._OK_STREAK.pop(KEY, None)
            first = lc.note_rate_limited(KEY)
            assert first > lc._CLOUD_MIN_INTERVAL
            # 封顶
            for _ in range(10):
                top = lc.note_rate_limited(KEY)
            assert top == lc._CLOUD_MAX_INTERVAL
            # Retry-After 被尊重
            assert lc.note_rate_limited(KEY, 7.0) >= 7.0
            # 连续成功后衰减并最终清除动态档
            for _ in range(lc._CLOUD_DECAY_AFTER * 12):
                lc.note_success(KEY)
            assert KEY not in lc._ADAPT_INTERVAL, '衰减到底后应移除动态档'
            assert lc._current_interval(KEY) == lc._CLOUD_MIN_INTERVAL
        finally:
            lc._ADAPT_INTERVAL.pop(KEY, None)
            lc._OK_STREAK.pop(KEY, None)
            lc._LAST_CLOUD_CALL.pop(KEY, None)

    def test_sensenova_default_model_is_glm_5_2(self):
        """sensenova 默认模型：2026-09-17 由 kimi-k3 改为 glm-5.2。

        kimi-k3 在新 key 下连续 429（ModelAccountRpmRateLimitExceeded，间隔 75s 复测仍 429），
        同账号 glm-5.2 / deepseek-v4-flash / deepseek-v4-pro / 6.8-flash-lite 均实测 200。
        """
        import llm_client
        assert llm_client._PROVIDER_DEFAULT_MODELS['sensenova'] == 'glm-5.2'
        assert llm_client._PROVIDER_DEFAULT_MODELS['minimax'] == 'glm-5.2'
        assert llm_client._resolve_model('sensenova', {}) == 'glm-5.2'

    def test_zhipu_default_model_no_longer_glm_4_7_flash(self):
        """glm-4.7-flash 已从模型库移除，默认值不得再指向它（否则必 404）。"""
        import llm_client
        assert llm_client._PROVIDER_DEFAULT_MODELS['zhipu'] == 'glm-4-flash'
        assert 'glm-4.7-flash' not in llm_client._PROVIDER_DEFAULT_MODELS.values()

    def test_kimi_k3_forces_temperature_1(self):
        """kimi-k3 只接受 temperature=1，传 0.3 会 400"""
        import llm_client
        payload = llm_client._build_payload(
            'sensenova', {'model': 'kimi-k3', 'temperature': 0.3},
            [{'role': 'user', 'content': 'hi'}])
        assert payload['temperature'] == 1.0
        assert payload['model'] == 'kimi-k3'

    def test_other_models_keep_configured_temperature(self):
        """非 kimi-k3 不得被改动（零回归）"""
        import llm_client
        payload = llm_client._build_payload(
            'zhipu', {'model': 'glm-4.7-flash', 'temperature': 0.3},
            [{'role': 'user', 'content': 'hi'}])
        assert payload['temperature'] == 0.3

    def test_temperature_default_when_unset(self):
        import llm_client
        payload = llm_client._build_payload(
            'zhipu', {'model': 'glm-4.7-flash'},
            [{'role': 'user', 'content': 'hi'}])
        assert payload['temperature'] == 0.3


# ============================================================
# 6) agent 轮次上限（429 缓解的第三件套）
# ============================================================

def test_agent_loop_iteration_ceiling_is_6():
    """回归绊线：agent_loop 的 'auto' 轮次封顶必须 ≤6。

    背景：免费额度下轮次过高会显著抬升 429 概率，该值在 2026-09-14 由 8 收紧为 6。
    若将来有人重新放宽，本用例会失败以提醒同步评估限流影响。
    """
    import inspect
    import ai_agent
    src = inspect.getsource(ai_agent.agent_loop)
    assert 'min(6, max_iterations)' in src, 'auto 轮次封顶应为 6'
    assert 'min(8, max_iterations)' not in src, '不应存在旧的 8 轮封顶'


# ============================================================
# 7) 跨文件一致性 —— 前后端阶段名 / DOM id 不得漂移
# ============================================================

class TestFrontBackendConsistency:

    @staticmethod
    def _read(*parts):
        with open(os.path.join(_PROJECT_ROOT, *parts), encoding='utf-8') as f:
            return f.read()

    def test_frontend_stage_names_match_backend(self):
        """core.js 的 PIPE_STAGES.name 必须与后端阶段名完全一致"""
        from agent_pipeline import STAGE_DEFS
        from routes.ai_assistant_routes import _PIPELINE_STAGE_NAMES

        js = self._read('static', 'js', 'core.js')
        m = re.search(r'const PIPE_STAGES\s*=\s*\[(.*?)\];', js, re.S)
        assert m, 'core.js 中未找到 PIPE_STAGES 定义'
        fe_names = re.findall(r"name:\s*'([^']+)'", m.group(1))

        assert fe_names, 'PIPE_STAGES 未解析出 name'
        assert fe_names == list(_PIPELINE_STAGE_NAMES), (
            f'前端阶段名 {fe_names} 与后端白名单 {list(_PIPELINE_STAGE_NAMES)} 不一致')
        # 前三阶段必须与管线 STAGE_DEFS 同名，终稿合成为固定第四阶段
        assert fe_names[:3] == [s['name'] for s in STAGE_DEFS]

    def test_frontend_stage_keys_have_matching_dom_ids(self):
        """core.js 的 PIPE_STAGES.key 必须在模板中存在对应的 producer/model 下拉 id"""
        js = self._read('static', 'js', 'core.js')
        html = self._read('templates', 'tabs', 'tab-ai-assistant.html')
        m = re.search(r'const PIPE_STAGES\s*=\s*\[(.*?)\];', js, re.S)
        keys = re.findall(r"key:\s*'([^']+)'", m.group(1))
        assert keys, 'PIPE_STAGES 未解析出 key'
        for k in keys:
            for suffix in ('provider', 'model'):
                dom_id = f'pipe-{k}-{suffix}'
                assert f'id="{dom_id}"' in html, f'模板缺少 {dom_id}'

    def test_stage_default_models_exist_in_catalog(self):
        """各阶段 defModel 必须存在于该 defProv 的前端模型目录内——
        否则模型下拉渲染不出该默认值，界面显示与实际请求不符（2026-09-18 绊线）。
        """
        js = self._read('static', 'js', 'core.js')
        m = re.search(r'const PIPE_STAGES\s*=\s*\[(.*?)\];', js, re.S)
        assert m, 'core.js 中未找到 PIPE_STAGES 定义'
        # 解析前端模型目录 PROVIDER_MODELS
        pm_m = re.search(r'PROVIDER_MODELS\s*=\s*\{(.*?)\n\};', js, re.S)
        assert pm_m, 'core.js 中未找到 PROVIDER_MODELS 定义'
        catalog = {}
        for prov_m in re.finditer(r"^\s{2}(\w+):\s*\[(.*?)\n\s{2}\],", pm_m.group(1), re.S | re.M):
            ids = re.findall(r"id:\s*'([^']+)'", prov_m.group(2))
            catalog[prov_m.group(1)] = set(ids)
        for prov, model in re.findall(
                r"defProv:\s*'(\w+)'\s*,\s*defModel:\s*'([^']+)'", m.group(1)):
            assert model in catalog.get(prov, set()), (
                f'阶段默认模型 {prov}/{model} 不在前端模型目录内')

    def test_pipeline_config_versioned(self):
        """阶段配置必须带版本号（PIPE_CFG_VERSION + 保存时写 _v）——
        否则改 PIPE_STAGES 默认值后，老用户 localStorage 存档会永久覆盖新默认，
        出现「源码已改但界面不变」（2026-09-18 实证缺陷）。
        """
        js = self._read('static', 'js', 'core.js')
        assert 'const PIPE_CFG_VERSION' in js, '缺少 PIPE_CFG_VERSION 定义'
        assert re.search(r"_v:\s*PIPE_CFG_VERSION", js), '保存时未写入 _v 版本号'
        assert 'saved._v !== PIPE_CFG_VERSION' in js, '加载时未做版本校验/迁移'

    def test_removed_legacy_symbols_are_gone(self):
        """2026-09-14 重构已移除：Agent 独立开关 + 快捷 chips（勿回退）"""
        js = self._read('static', 'js', 'core.js')
        html = self._read('templates', 'tabs', 'tab-ai-assistant.html')
        for sym in ('agentMode', 'toggleAgentMode', 'updateAgentModeButton', 'quickAsk'):
            assert sym not in js, f'core.js 仍残留已移除符号：{sym}'
        for sym in ('agent-mode-btn', 'ai-chips'):
            assert sym not in html, f'模板仍残留已移除标记：{sym}'

    def test_ai_mode_is_single_switch(self):
        """单开关不回归：mode 直接取 aiMode，不再与 agentMode 做三元"""
        js = self._read('static', 'js', 'core.js')
        assert 'mode: aiMode' in js
        assert 'agentMode ?' not in js

    def test_provider_select_default_matches_js_default(self):
        """#llm-provider 的 selected 必须与 core.js 的 DEFAULT_LLM_CONFIG.provider 一致。

        真实缺陷（2026-09-14 发现）：HTML 默认 selected=sensenova，
        而 core.js DEFAULT_LLM_CONFIG.provider='zhipu'；且 syncAIModelSelect() 只同步
        #ai-model-select、不回写 #llm-provider。
        结果：界面显示"商汤日日新"，请求实际发 zhipu —— 误导性极强。
        """
        html = self._read('templates', 'tabs', 'tab-ai-assistant.html')
        js = self._read('static', 'js', 'core.js')

        m_sel = re.search(r'<select[^>]*id="llm-provider".*?</select>', html, re.S)
        assert m_sel, '模板中未找到 #llm-provider'
        m_checked = re.search(r'<option\s+value="([^"]+)"\s+selected', m_sel.group(0))
        assert m_checked, '#llm-provider 中没有任何 selected 选项'

        m_def = re.search(
            r"const DEFAULT_LLM_CONFIG\s*=\s*\{[^}]*?provider:\s*'([^']+)'", js, re.S)
        assert m_def, 'core.js 中未找到 DEFAULT_LLM_CONFIG.provider'

        assert m_checked.group(1) == m_def.group(1), (
            f'界面默认 provider={m_checked.group(1)!r} 与 '
            f'JS 实际生效默认={m_def.group(1)!r} 不一致（界面会显示与实际请求不符）')

    def test_pipeline_default_providers_are_selectable(self):
        """PIPE_STAGES 各阶段 defProv 必须出现在 #llm-provider 的可选列表里。

        否则用户展开"多Agent协作"后无法看到/切换该 provider，默认值形同暗坑。
        """
        js = self._read('static', 'js', 'core.js')
        html = self._read('templates', 'tabs', 'tab-ai-assistant.html')

        m = re.search(r'const PIPE_STAGES\s*=\s*\[(.*?)\];', js, re.S)
        assert m, 'core.js 中未找到 PIPE_STAGES 定义'
        def_provs = re.findall(r"defProv:\s*'([^']+)'", m.group(1))
        assert len(def_provs) == 4, f'PIPE_STAGES 应有 4 个 defProv，实得 {def_provs}'

        m_sel = re.search(r'<select[^>]*id="llm-provider".*?</select>', html, re.S)
        options = set(re.findall(r'<option\s+value="([^"]+)"', m_sel.group(0)))
        for p in def_provs:
            assert p in options, f'阶段默认 provider {p!r} 不在 #llm-provider 可选列表中'
