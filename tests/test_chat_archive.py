# -*- coding: utf-8 -*-
"""test_chat_archive.py — 聊天记录本地归档单测

覆盖：日期分文件夹 / JSON+MD 双份 / 合并去重 / 删除同步 / 重建 /
旧数据（缺 ts）兼容 / 文件名安全化 / 与 ai_assistant 落盘链路集成。
"""
import json
import os
import re
from datetime import date

import pytest

import chat_archive as ca

DAY_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


@pytest.fixture
def archive_dir(tmp_path, monkeypatch):
    """把归档根目录指到临时目录，避免污染生产 data/chat_history/。"""
    monkeypatch.setattr(ca, 'ARCHIVE_DIR', str(tmp_path))
    return tmp_path


def _msg(role, content, cid='legacy', ts=None, stamp='10:00'):
    m = {'role': role, 'content': content, 'conversation_id': cid, 'timestamp': stamp}
    if ts:
        m['ts'] = ts
    return m


def _days(root):
    return sorted(n for n in os.listdir(root) if DAY_RE.match(n))


# ---------- 基础结构 ----------

def test_sync_creates_dated_folder_with_json_and_md(archive_dir):
    r = ca.sync_messages([_msg('user', '帮我分析这份数据'),
                          _msg('assistant', '好的，先看描述统计')])
    assert r['ok'] is True
    days = _days(archive_dir)
    assert days == [date.today().isoformat()]
    day_dir = archive_dir / days[0]
    assert (day_dir / 'legacy.json').exists()
    assert (day_dir / 'legacy.md').exists()
    assert (archive_dir / 'index.json').exists()


def test_index_reports_counts(archive_dir):
    ca.sync_messages([_msg('user', '问题一'), _msg('assistant', '回答一')])
    idx = json.loads((archive_dir / 'index.json').read_text(encoding='utf-8'))
    assert idx['version'] == ca.ARCHIVE_VERSION
    day = date.today().isoformat()
    assert idx['dates'][day]['legacy']['count'] == 2
    assert idx['total_messages'] == 2
    assert idx['dates'][day]['legacy']['title'] == '问题一'


def test_markdown_is_human_readable(archive_dir):
    ca.sync_messages([_msg('user', '什么是夏普比率', stamp='09:30'),
                      _msg('assistant', '夏普比率衡量单位风险的超额收益', stamp='09:31')])
    md = (archive_dir / date.today().isoformat() / 'legacy.md').read_text(encoding='utf-8')
    assert '# 什么是夏普比率' in md
    assert '### 09:30 · 我' in md
    assert '### 09:31 · AI 助手' in md
    assert '夏普比率衡量单位风险的超额收益' in md


# ---------- 合并与去重 ----------

def test_repeat_sync_does_not_duplicate(archive_dir):
    msgs = [_msg('user', '重复问题'), _msg('assistant', '重复回答')]
    ca.sync_messages(msgs)
    ca.sync_messages(msgs)
    ca.sync_messages(msgs)
    items = json.loads((archive_dir / date.today().isoformat() / 'legacy.json')
                       .read_text(encoding='utf-8'))
    assert len(items) == 2


def test_same_text_same_minute_is_kept_twice(archive_dir):
    """用户连发两次"继续"不能被去重吃掉。"""
    ca.sync_messages([_msg('user', '继续', ts='2026-09-19T10:00:01')])
    ca.sync_messages([_msg('user', '继续', ts='2026-09-19T10:00:05')])
    items = json.loads((archive_dir / '2026-09-19' / 'legacy.json').read_text(encoding='utf-8'))
    assert len(items) == 2


def test_incremental_sync_merges(archive_dir):
    ca.sync_messages([_msg('user', '第一问', ts='2026-09-19T10:00:00')])
    ca.sync_messages([_msg('assistant', '第一答', ts='2026-09-19T10:00:02'),
                      _msg('user', '第二问', ts='2026-09-19T10:01:00')])
    items = json.loads((archive_dir / '2026-09-19' / 'legacy.json').read_text(encoding='utf-8'))
    assert [i['content'] for i in items] == ['第一问', '第一答', '第二问']


def test_messages_split_across_days(archive_dir):
    ca.sync_messages([_msg('user', '昨天问的', ts='2026-09-18T23:59:00'),
                      _msg('user', '今天问的', ts='2026-09-19T00:01:00')])
    assert _days(archive_dir) == ['2026-09-18', '2026-09-19']
    assert len(ca.stats()['days']) == 2


def test_same_second_exchange_keeps_order(archive_dir):
    """同一秒内 ts 完全相同的问答必须保持写入序（问-答-问-答），不能按 role 归堆。"""
    same_ts = '2026-09-19T13:46:37'
    ca.sync_messages([_msg('user', '问一', ts=same_ts), _msg('assistant', '答一', ts=same_ts)])
    ca.sync_messages([_msg('user', '问二', ts=same_ts), _msg('assistant', '答二', ts=same_ts)])
    items = json.loads((archive_dir / '2026-09-19' / 'legacy.json').read_text(encoding='utf-8'))
    assert [i['content'] for i in items] == ['问一', '答一', '问二', '答二']
    assert [i['seq'] for i in items] == [0, 1, 2, 3]
    md = (archive_dir / '2026-09-19' / 'legacy.md').read_text(encoding='utf-8')
    assert md.index('问一') < md.index('答一') < md.index('问二') < md.index('答二')


def test_seq_is_stable_across_repeat_sync(archive_dir):
    same_ts = '2026-09-19T13:46:37'
    msgs = [_msg('user', 'Q', ts=same_ts), _msg('assistant', 'A', ts=same_ts)]
    ca.sync_messages(msgs)
    first = json.loads((archive_dir / '2026-09-19' / 'legacy.json').read_text(encoding='utf-8'))
    ca.sync_messages(msgs + [_msg('user', 'Q2', ts=same_ts)])
    second = json.loads((archive_dir / '2026-09-19' / 'legacy.json').read_text(encoding='utf-8'))
    assert [i['seq'] for i in second] == [0, 1, 2]
    assert [i['id'] for i in first] == [i['id'] for i in second[:2]]


def test_legacy_file_without_seq_still_orders(archive_dir):
    """老归档文件没有 seq 字段：按数组位置补，不能因为缺字段报错或乱序。"""
    day_dir = archive_dir / '2026-09-19'
    day_dir.mkdir(parents=True)
    legacy = [{'id': 'a', 'role': 'user', 'content': '老一', 'time': '10:00',
               'ts': '2026-09-19T10:00:00', 'conversation_id': 'legacy'},
              {'id': 'b', 'role': 'assistant', 'content': '老二', 'time': '10:00',
               'ts': '2026-09-19T10:00:00', 'conversation_id': 'legacy'}]
    (day_dir / 'legacy.json').write_text(json.dumps(legacy, ensure_ascii=False), encoding='utf-8')
    ca.sync_messages([_msg('user', '新三', ts='2026-09-19T10:00:00')])
    items = json.loads((day_dir / 'legacy.json').read_text(encoding='utf-8'))
    assert [i['content'] for i in items] == ['老一', '老二', '新三']


def test_multiple_conversations_separate_files(archive_dir):
    ca.sync_messages([_msg('user', 'A 会话', cid='chat_aaa'),
                      _msg('user', 'B 会话', cid='chat_bbb')])
    day_dir = archive_dir / date.today().isoformat()
    assert (day_dir / 'chat_aaa.json').exists()
    assert (day_dir / 'chat_bbb.json').exists()


# ---------- 删除同步 ----------

def test_purge_single_message(archive_dir):
    ca.sync_messages([_msg('user', '要删掉这条', stamp='10:00'),
                      _msg('user', '这条留着', stamp='10:01')])
    removed = ca.purge(message={'role': 'user', 'content': '要删掉这条', 'time': '10:00'})
    assert removed == 1
    items = json.loads((archive_dir / date.today().isoformat() / 'legacy.json')
                       .read_text(encoding='utf-8'))
    assert [i['content'] for i in items] == ['这条留着']


def test_purge_conversation_removes_files(archive_dir):
    ca.sync_messages([_msg('user', 'A', cid='chat_aaa'),
                      _msg('user', 'B', cid='chat_bbb')])
    ca.purge(conversation_id='chat_aaa')
    day_dir = archive_dir / date.today().isoformat()
    assert not (day_dir / 'chat_aaa.json').exists()
    assert not (day_dir / 'chat_aaa.md').exists()
    assert (day_dir / 'chat_bbb.json').exists()


def test_purge_all(archive_dir):
    ca.sync_messages([_msg('user', 'X', cid='chat_aaa'), _msg('user', 'Y', cid='chat_bbb')])
    ca.purge()
    idx = json.loads((archive_dir / 'index.json').read_text(encoding='utf-8'))
    assert idx['total_messages'] == 0


# ---------- 重建 / 兼容 / 安全 ----------

def test_rebuild_clears_previous(archive_dir):
    ca.sync_messages([_msg('user', '旧记录', cid='chat_old')])
    ca.rebuild([_msg('user', '新记录', cid='chat_new')])
    day_dir = archive_dir / date.today().isoformat()
    assert not (day_dir / 'chat_old.json').exists()
    assert (day_dir / 'chat_new.json').exists()


def test_legacy_message_without_ts_gets_today(archive_dir):
    """旧数据只有 HH:MM：补当天日期，不因为缺 ts 而丢记录。"""
    r = ca.sync_messages([{'role': 'user', 'content': '请继续', 'timestamp': '11:54'}])
    assert r['written'] == 1
    assert _days(archive_dir) == [date.today().isoformat()]


def test_unknown_role_skipped(archive_dir):
    r = ca.sync_messages([{'role': 'tool', 'content': 'x'}, _msg('user', '正常')])
    assert r['written'] == 1


def test_unsafe_conversation_id_is_sanitized(archive_dir):
    ca.sync_messages([_msg('user', '危险 id', cid='../../evil')])
    day_dir = archive_dir / date.today().isoformat()
    names = os.listdir(day_dir)
    assert all('/' not in n and '..' not in n for n in names)
    assert len([n for n in names if n.endswith('.json')]) == 1


def test_empty_input_is_noop(archive_dir):
    r = ca.sync_messages([])
    assert r['ok'] is True and r['written'] == 0
    assert not archive_dir.exists() or not _days(archive_dir)


def test_stats_shape(archive_dir):
    ca.sync_messages([_msg('user', 'Q'), _msg('assistant', 'A')])
    s = ca.stats()
    assert s['exists'] is True
    assert s['total_messages'] == 2
    assert s['days'][0]['messages'] == 2


# ---------- 与 ai_assistant 集成 ----------

def test_record_exchange_writes_archive(archive_dir, tmp_path, monkeypatch):
    import ai_assistant as aa
    monkeypatch.setattr(aa, 'HISTORY_PATH', str(tmp_path / 'chat_history.json'))
    saved = list(aa._chat_manager.history)
    try:
        aa._chat_manager.history.clear()
        aa.record_chat_exchange('归档集成测试', '已写入归档', 'chat_arch_integration')
        day_dir = archive_dir / date.today().isoformat()
        files = [n for n in os.listdir(day_dir) if n.endswith('.json')]
        assert len(files) == 1
        items = json.loads((day_dir / files[0]).read_text(encoding='utf-8'))
        assert [i['content'] for i in items] == ['归档集成测试', '已写入归档']
        assert items[0]['conversation_id'] == 'chat_arch_integration'
    finally:
        aa._chat_manager.history[:] = saved
        aa._chat_manager.flush_history()


def test_clear_history_purges_archive(archive_dir, tmp_path, monkeypatch):
    import ai_assistant as aa
    monkeypatch.setattr(aa, 'HISTORY_PATH', str(tmp_path / 'chat_history.json'))
    saved = list(aa._chat_manager.history)
    try:
        aa._chat_manager.history.clear()
        aa.record_chat_exchange('会被清掉', '答', 'chat_arch_clear')
        aa.clear_chat_history('chat_arch_clear')
        idx = json.loads((archive_dir / 'index.json').read_text(encoding='utf-8'))
        assert idx['total_messages'] == 0
    finally:
        aa._chat_manager.history[:] = saved
        aa._chat_manager.flush_history()


def test_legacy_store_load_gets_stable_ts(tmp_path, monkeypatch):
    """老主存（改造前落盘，无 ts）加载时必须补成"当天+HH:MM"且跨次稳定。

    若用 now() 补，同一条老记录每次重启都会换 id，归档里会越攒越多重复条目。
    """
    import ai_assistant as aa
    store = tmp_path / 'chat_history.json'
    store.write_text(json.dumps(
        [{'role': 'user', 'content': '老记录', 'timestamp': '11:54',
          'metadata': {}, 'conversation_id': 'legacy'}], ensure_ascii=False), encoding='utf-8')
    monkeypatch.setattr(aa, 'HISTORY_PATH', str(store))
    first = aa._chat_manager._load_history()
    second = aa._chat_manager._load_history()
    assert first[0].ts == second[0].ts == f'{date.today().isoformat()}T11:54'
    # 落盘（用加载结果替换内存）后回归带 ts，再加载仍然是同一个值
    saved = list(aa._chat_manager.history)
    try:
        aa._chat_manager.history[:] = first
        aa._chat_manager.flush_history()
    finally:
        aa._chat_manager.history[:] = saved
    reloaded = aa._chat_manager._load_history()
    assert reloaded[0].ts == first[0].ts


def test_history_main_store_keeps_ts(archive_dir, tmp_path, monkeypatch):
    """主存落盘必须带 ts，否则重启后同一条消息会在归档里存成两天。"""
    import ai_assistant as aa
    store = tmp_path / 'chat_history.json'
    monkeypatch.setattr(aa, 'HISTORY_PATH', str(store))
    saved = list(aa._chat_manager.history)
    try:
        aa._chat_manager.history.clear()
        aa.record_chat_exchange('时间戳测试', '答', 'chat_arch_ts')
        raw = json.loads(store.read_text(encoding='utf-8'))
        assert all(m.get('ts') for m in raw), raw
    finally:
        aa._chat_manager.history[:] = saved
        aa._chat_manager.flush_history()


# ---------- 多进程覆盖防护 ----------

def _other_process_writes(store, items):
    """模拟"另一个进程"把历史写进同一个文件。"""
    store.write_text(json.dumps(items, ensure_ascii=False), encoding='utf-8')


def test_save_keeps_disk_increment_from_other_process(tmp_path, monkeypatch):
    """别的进程写进磁盘的消息，不得被本进程的全量回写抹掉。

    背景：任何 import ai_assistant 的脚本都会持有一份内存副本并在退出时回写，
    谁最后退出谁覆盖（实测跑一次探针脚本就抹掉了服务期间新增的整段对话）。
    """
    import ai_assistant as aa
    store = tmp_path / 'chat_history.json'
    monkeypatch.setattr(aa, 'HISTORY_PATH', str(store))
    saved = list(aa._chat_manager.history)
    other = [
        {'role': 'user', 'content': '别的进程写的', 'timestamp': '10:00',
         'ts': '2026-09-19T10:00:00', 'conversation_id': 'other'},
        {'role': 'assistant', 'content': '别的进程的回复', 'timestamp': '10:00',
         'ts': '2026-09-19T10:00:01', 'conversation_id': 'other'},
    ]
    try:
        _other_process_writes(store, other)
        aa._chat_manager.history[:] = []      # 本进程内存里什么都没有
        aa._chat_manager.flush_history()
        contents = [m['content'] for m in json.loads(store.read_text(encoding='utf-8'))]
        assert '别的进程写的' in contents
        assert '别的进程的回复' in contents
    finally:
        aa._chat_manager.history[:] = saved
        aa._chat_manager.flush_history()


def test_repeat_flush_does_not_duplicate(tmp_path, monkeypatch):
    """反复 flush（每次都会读盘合并）不能把同一条消息攒成多份。"""
    import ai_assistant as aa
    store = tmp_path / 'chat_history.json'
    monkeypatch.setattr(aa, 'HISTORY_PATH', str(store))
    saved = list(aa._chat_manager.history)
    other = [{'role': 'user', 'content': '只此一条', 'timestamp': '10:00',
              'ts': '2026-09-19T10:00:00', 'conversation_id': 'other'}]
    try:
        _other_process_writes(store, other)
        aa._chat_manager.history[:] = []
        for _ in range(4):
            aa._chat_manager.flush_history()
        raw = json.loads(store.read_text(encoding='utf-8'))
        assert [m['content'] for m in raw] == ['只此一条']
    finally:
        aa._chat_manager.history[:] = saved
        aa._chat_manager.flush_history()


def test_delete_is_not_resurrected_by_merge(tmp_path, monkeypatch):
    """删除后 flush 不得把刚删的消息从磁盘 merge 回来（实测复活回归）。

    "跨进程合并"保护会读盘把"内存里没有"的消息捡回来，而删除天然满足
    "内存里没有" —— 结果 delete-message 报 removed=1 却一条也没真删掉。
    因此三处删除/清空必须走 flush_history(merge_disk=False)。
    """
    import ai_assistant as aa
    store = tmp_path / 'chat_history.json'
    monkeypatch.setattr(aa, 'HISTORY_PATH', str(store))
    saved = list(aa._chat_manager.history)
    try:
        aa._chat_manager.history[:] = []
        aa.record_chat_exchange('要被删掉的问题', '要被删掉的回答', 'chat_del')
        aa._chat_manager.flush_history()
        assert aa.delete_chat_message('要被删掉的回答', role='assistant') == 1
        contents = [m['content'] for m in json.loads(store.read_text(encoding='utf-8'))]
        assert '要被删掉的回答' not in contents
        assert '要被删掉的问题' in contents
    finally:
        aa._chat_manager.history[:] = saved
        aa._chat_manager.flush_history()
