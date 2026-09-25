# -*- coding: utf-8 -*-
"""多会话（新建对话 + 会话列表切换）测试（2026-09-20）。

设计要点：
  1) 会话列表**必须以本地归档为准** —— 主存 data/chat_history.json 只保留最近 100 条，
     重度使用后老会话会被整段截断；归档永不截断。若会话列表读主存，用户切回旧会话
     会发现它"消失了"。
  2) `_recent_history_block` 必须**按当前会话过滤** —— `_chat_manager.history` 是全局
     扁平列表（conversation_id 只是消息上的标签），不过滤会把 A 会话内容当作 B 会话
     上下文喂给模型（症状：新对话里 AI 莫名接着上一个话题）。
  3) 删除会话时归档清理**无条件执行** —— 老会话可能已不在主存（removed==0），
     但归档文件还在，不删会出现"列表删了、文件夹里还在"。
"""
import re

import pytest

import ai_assistant as aa
import chat_archive as ca


@pytest.fixture
def conv_env(monkeypatch, tmp_path):
    """隔离归档目录 + 落盘路径，并快照/恢复会话相关的全局状态。

    为什么要连 HISTORY_PATH 一起隔离：`_save_history()` 落盘前会调
    `_merge_disk_history()` 把磁盘上"本进程没见过"的消息并回内存。conftest 的
    HISTORY_PATH 是整个测试会话共享的，不隔离的话，前一个测试写过的会话会被
    **回灌进内存**，让"会话按最近写入排序"这类断言随机失败（实测发生）。
    """
    import conversation_memory as cm

    monkeypatch.setattr(ca, 'ARCHIVE_DIR', str(tmp_path / 'arch'))
    monkeypatch.setattr(aa, 'HISTORY_PATH', str(tmp_path / 'hist.json'))

    hist_snapshot = list(aa._chat_manager.history)
    ctx_snapshot = dict(aa._chat_manager._conversation_contexts)
    active_snapshot = getattr(aa._chat_manager, 'active_conversation_id', 'legacy')
    mem = cm.get_memory()
    mem_buckets = dict(mem._buckets)
    mem_active = mem.active_conversation_id
    # 从干净内存开始，避免上一个测试的残留影响排序与计数
    aa._chat_manager.history[:] = []
    try:
        yield aa, ca
    finally:
        aa._chat_manager.history[:] = hist_snapshot
        aa._chat_manager._conversation_contexts.clear()
        aa._chat_manager._conversation_contexts.update(ctx_snapshot)
        aa._chat_manager.active_conversation_id = active_snapshot
        aa._chat_manager.context = aa._chat_manager._conversation_contexts.setdefault(
            active_snapshot, {'last_file': None, 'last_action': None})
        mem._buckets.clear()
        mem._buckets.update(mem_buckets)
        mem.set_active_conversation(mem_active)


# ==================== 归档层：会话聚合与读取 ====================

def test_archive_list_conversations_aggregates_across_days(conv_env):
    """跨天的同一会话必须合并计数；标题取最早那天的开场问题。"""
    _, ca_ = conv_env
    ca_.sync_messages([
        {'role': 'user', 'content': '会话A的第一个问题', 'conversation_id': 'convA',
         'ts': '2026-09-20T10:00:00'},
        {'role': 'assistant', 'content': 'A的回答', 'conversation_id': 'convA',
         'ts': '2026-09-20T10:00:01'},
    ])
    ca_.sync_messages([
        {'role': 'user', 'content': '会话A的第二天追问', 'conversation_id': 'convA',
         'ts': '2026-09-21T09:00:00'},
        {'role': 'assistant', 'content': 'A的回答2', 'conversation_id': 'convA',
         'ts': '2026-09-21T09:00:01'},
    ])
    ca_.sync_messages([
        {'role': 'user', 'content': '会话B的开场', 'conversation_id': 'convB',
         'ts': '2026-09-21T11:00:00'},
    ])

    convs = {c['id']: c for c in ca_.list_conversations()}
    assert 'convA' in convs and 'convB' in convs
    assert convs['convA']['message_count'] == 4                 # 跨天合并
    assert convs['convA']['title'] == '会话A的第一个问题'          # 最早的标题 = 开场问题
    assert convs['convA']['days'] == ['2026-09-20', '2026-09-21']
    assert convs['convB']['message_count'] == 1
    # 最近活动在前
    assert ca_.list_conversations()[0]['id'] == 'convB'


def test_archive_load_conversation_returns_chronological_messages(conv_env):
    """读某会话：跨天合并、按 seq 升序（不是按 ts，同秒会归堆）。"""
    _, ca_ = conv_env
    ca_.sync_messages([
        {'role': 'user', 'content': '第一问', 'conversation_id': 'convL',
         'ts': '2026-09-20T10:00:00'},
        {'role': 'assistant', 'content': '第一答', 'conversation_id': 'convL',
         'ts': '2026-09-20T10:00:00'},        # 同秒：必须靠 seq 保序
    ])
    ca_.sync_messages([
        {'role': 'user', 'content': '第二天的追问', 'conversation_id': 'convL',
         'ts': '2026-09-21T08:00:00'},
    ])
    items = ca_.load_conversation('convL')
    assert [i['content'] for i in items] == ['第一问', '第一答', '第二天的追问']
    assert [i['role'] for i in items] == ['user', 'assistant', 'user']
    assert items[0]['timestamp'] == '10:00'
    assert all(i['conversation_id'] == 'convL' for i in items)
    assert ca_.load_conversation('不存在的会话') == []


# ==================== 历史与会话列表：主存截断后回退归档 ====================

def test_get_chat_history_falls_back_to_archive_after_memory_eviction(conv_env):
    """主存（100 条上限）已丢弃该会话时，仍能从归档读回完整历史。"""
    aa_, ca_ = conv_env
    aa_.record_chat_exchange('老会话的问题', '老会话的回答', conversation_id='convOld')
    assert [m['content'] for m in aa_.get_chat_history('convOld')] == ['老会话的问题', '老会话的回答']

    aa_._chat_manager.history[:] = []          # 模拟主存截断：内存里没有该会话了
    got = aa_.get_chat_history('convOld')
    assert [m['content'] for m in got] == ['老会话的问题', '老会话的回答']
    assert got[0]['conversation_id'] == 'convOld'


def test_get_chat_conversations_includes_archived_after_memory_eviction(conv_env):
    """会话列表以归档为准：内存清空后仍然列出该会话。"""
    aa_, ca_ = conv_env
    ca_.sync_messages([
        {'role': 'user', 'content': '归档里的老会话', 'conversation_id': 'convArch',
         'ts': '2026-09-19T08:00:00'},
    ])
    aa_._chat_manager.history[:] = []
    ids = [c['id'] for c in aa_.get_chat_conversations()]
    assert 'convArch' in ids


def test_get_chat_conversations_keeps_memory_only_conversation(conv_env):
    """归档还没写入的会话（刚建的）也要出现在列表里，否则"点了新建却看不到"。"""
    aa_, _ = conv_env
    aa_._chat_manager.history[:] = [
        aa.ChatMessage(role='user', content='只在内存里的新会话', conversation_id='convMem')
    ]
    ids = [c['id'] for c in aa_.get_chat_conversations()]
    assert 'convMem' in ids


def test_conversation_order_uses_memory_recency_not_filename(conv_env):
    """同一秒内碰过的两个会话（归档 ts 并列）必须按"谁最后写入"排序。

    归档时间戳只到秒级，同秒创建会并列；若只按 ts 排会退化成归档文件名序
    （字母序），实测把后创建的 chat_aaa 排到了 chat_bbb 前面。
    """
    aa_, _ = conv_env
    aa_.record_chat_exchange('会话一的问题', '会话一的回答', conversation_id='chat_aaa')
    aa_.record_chat_exchange('会话二的问题', '会话二的回答', conversation_id='chat_bbb')
    ids = [c['id'] for c in aa_.get_chat_conversations()]
    assert ids[:2] == ['chat_bbb', 'chat_aaa'], f'会话顺序未按写入时间：{ids[:2]}'
    # 两者的归档 last_ts 确实并列（否则本用例失去意义）
    lt = {c['id']: c.get('last_ts') for c in aa_.get_chat_conversations()}
    assert lt.get('chat_aaa') == lt.get('chat_bbb')


def test_conversation_title_prefers_archive_opening_question(conv_env):
    """标题取归档里该会话最早那条用户消息（会话开场问题），不是最近一条。"""
    aa_, ca_ = conv_env
    aa_.record_chat_exchange('开场白问题', '开场白回答', conversation_id='convTitle')
    aa_.record_chat_exchange('后续追问', '后续回答', conversation_id='convTitle')
    item = next(c for c in aa_.get_chat_conversations() if c['id'] == 'convTitle')
    assert item['title'] == '开场白问题'
    assert item['message_count'] == 4


def test_conversation_title_never_leaks_raw_cid(conv_env):
    """归档里没有用户消息时，标题不能退化成原始会话 id。

    否则界面上会出现一个名叫 "legacy" 的会话（升级用户的历史记录就是这样），
    用户完全看不懂。应回退到通用词"历史对话"。
    """
    aa_, ca_ = conv_env
    ca_.sync_messages([
        {'role': 'assistant', 'content': '只有回答没有提问', 'conversation_id': 'convNoUser',
         'ts': '2026-09-20T20:47:58'},
    ])
    aa_._chat_manager.history[:] = []
    item = next(c for c in aa_.get_chat_conversations() if c['id'] == 'convNoUser')
    assert item['title'] == '历史对话', item['title']


# ==================== 删除 / 清空：只作用指定会话 ====================

def test_delete_conversation_purges_archive_even_when_not_in_memory(conv_env):
    """老会话不在主存时（removed==0）也必须清掉归档，否则"删了还在"。"""
    aa_, ca_ = conv_env
    ca_.sync_messages([
        {'role': 'user', 'content': '待删会话', 'conversation_id': 'convDel',
         'ts': '2026-09-18T08:00:00'},
    ])
    aa_._chat_manager.history[:] = []
    assert ca_.load_conversation('convDel')                     # 归档里确实有
    removed = aa_.delete_chat_conversation('convDel')
    assert removed == 0                                        # 主存本来就没有
    assert ca_.load_conversation('convDel') == []              # 归档必须被清掉


def test_clear_scoped_conversation_keeps_others(conv_env):
    """带 conversation_id 的清空只影响该会话。"""
    aa_, _ = conv_env
    aa_.record_chat_exchange('会话1的问题', '会话1的回答', conversation_id='convC1')
    aa_.record_chat_exchange('会话2的问题', '会话2的回答', conversation_id='convC2')

    aa_.clear_chat_history('convC1')
    assert aa_.get_chat_history('convC1') == []
    assert [m['content'] for m in aa_.get_chat_history('convC2')] == ['会话2的问题', '会话2的回答']


def test_delete_one_conversation_does_not_touch_other(conv_env):
    aa_, _ = conv_env
    aa_.record_chat_exchange('甲的提问', '甲的回答', conversation_id='convX')
    aa_.record_chat_exchange('乙的提问', '乙的回答', conversation_id='convY')
    assert aa_.delete_chat_conversation('convX') == 2
    assert aa_.get_chat_history('convX') == []
    assert [m['content'] for m in aa_.get_chat_history('convY')] == ['乙的提问', '乙的回答']


# ==================== 新建会话 ====================

def test_create_chat_conversation_id_valid_and_activated(conv_env):
    """新会话 id 必须落在白名单内（否则被静默降级成 legacy，全部挤一个桶）。"""
    aa_, _ = conv_env
    conv = aa_.create_chat_conversation()
    assert re.fullmatch(r'[A-Za-z0-9_-]{1,80}', conv['id']), conv['id']
    assert conv['id'] != 'legacy'
    assert aa_._chat_manager.active_conversation_id == conv['id']
    assert conv['active'] is True
    # 连续新建必须得到不同 id（同一毫秒也不能撞）
    assert aa_.create_chat_conversation()['id'] != conv['id']


# ==================== 上下文隔离（跨会话不得串台） ====================

def test_recent_history_block_is_scoped_to_active_conversation(conv_env):
    aa_, _ = conv_env
    aa_.record_chat_exchange('A会话的开场问题', 'A会话的回答', conversation_id='convSA')
    aa_.record_chat_exchange('B会话的开场问题', 'B会话的回答', conversation_id='convSB')

    aa_.activate_chat_conversation('convSB')
    block = aa_._recent_history_block()
    assert 'B会话' in block
    assert 'A会话' not in block, '跨会话历史泄漏了：新会话不应看到别的会话内容'


def test_activate_isolates_last_file_context(conv_env):
    """last_file 按会话隔离：A 会话挂的文件不能串到 B 会话。"""
    aa_, _ = conv_env
    aa_.activate_chat_conversation('convFA')
    aa_._chat_manager.context['last_file'] = 'a.csv'
    aa_.activate_chat_conversation('convFB')
    assert aa_._chat_manager.context.get('last_file') is None
    aa_.activate_chat_conversation('convFA')
    assert aa_._chat_manager.context.get('last_file') == 'a.csv'


# ==================== HTTP 路由 ====================

def test_conversation_routes_available(conv_env, client, admin_headers):
    """四个会话接口的返回结构与参数校验。"""
    aa_, _ = conv_env

    r = client.get('/api/assistant/conversations', headers=admin_headers)
    assert r.status_code == 200
    body = r.get_json()
    assert body['ok'] is True and isinstance(body['conversations'], list)

    r2 = client.post('/api/assistant/new-conversation', headers=admin_headers)
    assert r2.status_code == 200
    body2 = r2.get_json()
    assert body2['ok'] is True
    cid = body2['conversation_id']
    assert re.fullmatch(r'[A-Za-z0-9_-]{1,80}', cid)

    r3 = client.get('/api/assistant/history?conversation_id=' + cid, headers=admin_headers)
    assert r3.status_code == 200
    assert r3.get_json()['ok'] is True

    r4 = client.post('/api/assistant/delete-conversation',
                     json={'conversation_id': cid}, headers=admin_headers)
    assert r4.status_code == 200
    assert r4.get_json()['ok'] is True

    # 缺少 conversation_id → 400（不能误删全部）
    r5 = client.post('/api/assistant/delete-conversation', json={}, headers=admin_headers)
    assert r5.status_code == 400


def test_chat_route_accepts_conversation_id(conv_env, monkeypatch, client, admin_headers):
    """POST /api/assistant/chat 必须把 conversation_id 透传到 process_chat_message。"""
    import routes.ai_assistant_routes as R

    seen = {}

    def _fake_process(message, llm_config=None, current_file=None, mode='think',
                      images=None, conversation_id=None, **kw):
        seen['cid'] = conversation_id
        return {'reply': '[stub]', 'source': 'stub'}

    monkeypatch.setattr(R, 'process_chat_message', _fake_process)
    r = client.post('/api/assistant/chat',
                    json={'message': '你好', 'conversation_id': 'convRoute'},
                    headers=admin_headers)
    assert r.status_code == 200
    assert seen.get('cid') == 'convRoute'
