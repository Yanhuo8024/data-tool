# -*- coding: utf-8 -*-
"""tests/test_egress_policy.py — 数据出境开关（联网搜索 / 文件样本）的回归

为什么需要这组测试：这两项决定"本机数据是否离开本机"，而默认值是**开启**——按 ZCode 事件的
教训，默认开启的行为必须有开关、开关位置可见、当前生效值可读。测试要证明的不只是"能关"，
还包括：
  ① 优先级真的是 环境变量 > 面板持久化 > 默认开启；
  ② 关闭后**真的没有外呼**（不是只改了个标志位）；
  ③ 文件样本关闭后**旧索引会被重建**，关闭前索引的样本不会残留可检索。
"""
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import egress_policy as ep  # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    """隔离策略文件 + 清空相关环境变量（避免继承真实部署的强制值）"""
    p = tmp_path / 'egress_policy.json'
    monkeypatch.setenv('EGRESS_POLICY_FILE', str(p))
    for feat in ep.FEATURES.values():
        monkeypatch.delenv(feat['env'], raising=False)
    ep.reset_cache()
    yield p
    ep.reset_cache()


@pytest.fixture
def sample_df(tmp_path, monkeypatch):
    """把 core_config 的文件读取指向一个含哨兵值的 DataFrame（磁盘上也要有同名文件）"""
    pd = pytest.importorskip('pandas')
    import core_config
    df = pd.DataFrame({'city': ['SENTINEL_ALPHA', 'b', 'c'], 'income': [1.0, 2.0, 3.0]})
    (tmp_path / 'x.csv').write_text('city,income\nSENTINEL_ALPHA,1.0\n', encoding='utf-8')
    monkeypatch.setattr(core_config, 'OUT_DIR', tmp_path)
    monkeypatch.setattr(core_config, 'load_dataframe', lambda _p: df)
    import file_rag
    monkeypatch.setattr(file_rag, '_INDEXED', {})
    return df


# ------------------------------ 默认值与优先级 ------------------------------
def test_default_is_enabled_and_source_is_default(store):
    assert ep.web_search_enabled() is True
    assert ep.file_sample_egress_enabled() is True
    snap = ep.snapshot()
    assert snap['ok'] and len(snap['features']) == len(ep.FEATURES)
    for f in snap['features']:
        assert f['enabled'] is True and f['default'] is True and f['source'] == 'default'
        assert f['locked'] is False and f['env'] and f['upstream'] and f['when_off']


def test_env_overrides_panel_and_locks_it(store, monkeypatch):
    monkeypatch.setenv('ALLOW_WEB_SEARCH', '0')
    assert ep.web_search_enabled() is False
    assert ep.flag_source('web_search') == 'env'
    assert ep.set_flag('web_search', True) is False        # 面板改不动被强制的项
    assert ep.web_search_enabled() is False
    feat = next(f for f in ep.snapshot()['features'] if f['key'] == 'web_search')
    assert feat['locked'] is True and feat['source'] == 'env'


def test_panel_value_persists_and_env_beats_it(store, monkeypatch):
    assert ep.set_flag('file_sample_egress', False) is True
    assert store.exists(), '面板开关应写入持久化文件'
    ep.reset_cache()
    assert ep.file_sample_egress_enabled() is False
    assert ep.flag_source('file_sample_egress') == 'file'
    monkeypatch.setenv('ALLOW_FILE_SAMPLE_EGRESS', '1')    # 部署方强制 → 覆盖面板
    assert ep.file_sample_egress_enabled() is True
    assert ep.flag_source('file_sample_egress') == 'env'


def test_tool_gate_and_registry(store):
    """工具闸门与 ToolRegistry 同源：关闭后既不进 prompt，也不可执行"""
    assert ep.tool_allowed('web_search') is True
    assert ep.tool_allowed('describe') is True             # 未受管制的工具永远放行
    ep.set_flag('web_search', False)
    assert ep.tool_allowed('web_search') is False

    import ai_agent
    names = [t['name'] for t in ai_agent.registry.list_tools()]
    assert 'web_search' not in names, '关闭后不应再列出该工具'
    assert 'describe' in names
    assert 'web_search' not in [t['name'] for t in ai_agent.registry.list_tools(['web_search', 'describe'])]
    info = ai_agent.registry.get_tool('web_search')         # 仍可拿到"桩"：误调用有明确原因
    assert info is not None and '禁用' in info['func'](query='x')


def test_web_search_makes_no_outbound_call_when_disabled(store, monkeypatch):
    import web_search as ws
    called = []

    def fake(*_a, **_k):
        called.append(1)
        return {'ok': True, 'results': [{'title': 't', 'url': 'https://example.com/a', 'snippet': 's'}]}

    for fn in ('_baidu_search', '_bing_search', '_ddg_search'):
        monkeypatch.setattr(ws, fn, fake)
    ep.set_flag('web_search', False)
    ep.reset_cache()
    assert ws.web_search('上证指数 最新 行情').get('ok') is False
    assert called == [], f'关闭状态下不得外呼，实际调用了 {called} 次'
    ep.set_flag('web_search', True)
    ep.reset_cache()
    assert ws.web_search('上证指数 行情').get('ok') is True
    assert called, '开启后应恢复外呼流程'


# --------------------------- 文件样本出境与索引重建 ---------------------------
def test_file_rag_sample_mode_off_goes_stats_only(store, sample_df):
    import file_rag
    sample = file_rag._load_as_text('x.csv')
    assert 'SENTINEL_ALPHA' in sample, '默认模式下样本行会进索引（这是要知情的出境内容）'
    ep.set_flag('file_sample_egress', False)
    ep.reset_cache()
    stats = file_rag._load_as_text('x.csv')
    assert 'SENTINEL_ALPHA' not in stats, '仅本地统计模式不得包含任何样本行'
    assert '仅本地统计模式' in stats and '数值分布' in stats
    assert file_rag.egress_status()['mode'] == 'stats'
    assert file_rag.egress_status()['note']


def test_switching_mode_rebuilds_index_and_drops_old_samples(store, monkeypatch, sample_df):
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
    import file_rag

    first = file_rag.index_current_file('x.csv')
    assert any('SENTINEL_ALPHA' in t for t in eng.docs.values())
    ep.set_flag('file_sample_egress', False)
    ep.reset_cache()
    second = file_rag.index_current_file('x.csv')
    assert second != first and first in eng.deleted, '切换模式必须剔除旧索引'
    assert not any('SENTINEL_ALPHA' in t for t in eng.docs.values()), '关闭后不得残留样本行'


# ------------------------------ 图片出境（识图） ------------------------------
def test_image_egress_blocks_before_sending(store):
    """关闭后识图分支在**构造消息体之前**返回：图片不得进入任何请求"""
    assert ep.image_egress_enabled() is True
    assert ep.vision_blocked_reply() is None
    ep.set_flag('image_egress', False)
    ep.reset_cache()
    assert ep.image_egress_enabled() is False
    blocked = ep.vision_blocked_reply()
    assert blocked and blocked['source'] == 'vision' and blocked['llm_synthesized'] is False
    assert '未发送' in blocked['reply'] and 'ALLOW_IMAGE_EGRESS' in blocked['reply']
    ep.set_flag('image_egress', True)
    ep.reset_cache()
    assert ep.vision_blocked_reply() is None


def test_vision_gate_is_shared_by_sync_and_stream(store):
    """同步与流式两处走同一判定，避免「同步拦住了、流式还在发图」"""
    import ai_assistant as A
    ep.set_flag('image_egress', False)
    ep.reset_cache()
    blocked = A._vision_egress_blocked('描述这张图')
    assert blocked and blocked['intent']['raw_text'] == '描述这张图'
    ep.set_flag('image_egress', True)
    ep.reset_cache()
    assert A._vision_egress_blocked('描述这张图') is None


def test_image_egress_route_toggle(client, store):
    body = client.get('/api/assistant/egress-policy').get_json()
    assert 'image_egress' in {f['key'] for f in body['features']}
    r = client.post('/api/assistant/egress-policy', json={'image_egress': False})
    assert r.status_code == 200 and r.get_json()['changed'] == {'image_egress': True}
    assert ep.image_egress_enabled() is False


# --------------------------------- 路由与注册 ---------------------------------
def test_route_serves_snapshot_and_toggle(client, store):
    r = client.get('/api/assistant/egress-policy')
    assert r.status_code == 200
    body = r.get_json()
    assert body['ok'] and {f['key'] for f in body['features']} == set(ep.FEATURES)

    r2 = client.post('/api/assistant/egress-policy', json={'web_search': False})
    assert r2.status_code == 200 and r2.get_json()['changed'] == {'web_search': True}
    assert ep.get_flag('web_search') is False


def test_route_refuses_when_env_locked(client, store, monkeypatch):
    monkeypatch.setenv('ALLOW_WEB_SEARCH', '1')
    r = client.post('/api/assistant/egress-policy', json={'web_search': False})
    assert r.status_code == 409 and r.get_json()['ok'] is False


def test_route_module_is_synchronously_registered():
    """懒注册竞态下首批请求会 404，而设置面板一打开就拉这个接口 → 必须在核心注册表里"""
    import routes
    assert 'egress_routes' in {e[0] for e in routes._CORE_REGISTRARS}
    assert 'egress_bp' in routes._BLUEPRINT_NAMES
    from routes.egress_routes import api_egress_policy
    assert callable(api_egress_policy)
