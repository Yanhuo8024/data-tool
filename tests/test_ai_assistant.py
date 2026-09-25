# -*- coding: utf-8 -*-
"""ai_assistant.py 单测：意图识别与对话管理（不调用真实 LLM）"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai_assistant
from ai_assistant import IntentRecognizer, ChatManager, DataContextProvider


@pytest.fixture
def recognizer():
    return IntentRecognizer()


def test_recognize_analyze_data(recognizer):
    r = recognizer.recognize('帮我分析一下数据')
    assert r['action'] == 'analyze_data'


def test_recognize_run_code(recognizer):
    r = recognizer.recognize('run python code to process')
    assert r['action'] == 'run_code'


def test_recognize_create_chart(recognizer):
    r = recognizer.recognize('draw a chart')
    assert r['action'] == 'create_chart'


def test_recognize_train_model(recognizer):
    r = recognizer.recognize('train a classification model')
    assert r['action'] == 'train_model'


def test_recognize_clean_data(recognizer):
    r = recognizer.recognize('clean data')
    assert r['action'] == 'clean_data'


def test_recognize_greeting(recognizer):
    r = recognizer.recognize('你好')
    assert r['action'] == 'greeting'


def test_recognize_fallback_chat(recognizer):
    r = recognizer.recognize('今天天气怎么样啊')
    assert r['action'] == 'chat'


def test_recognize_extracts_file_param(recognizer):
    r = recognizer.recognize('分析 sales.csv 文件')
    assert r['params'].get('file') == 'sales.csv'


def test_data_context_provider_empty():
    assert DataContextProvider.get_current_data_context() == ''
    assert DataContextProvider.get_current_data_context('不存在的文件.csv') == ''


def test_chat_manager_history_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_assistant, 'HISTORY_PATH', str(tmp_path / 'chat.json'))
    cm = ChatManager()
    # 无 LLM 配置时也应返回结构化响应而不抛异常
    resp = cm.process_message('分析数据', llm_config=None, current_file=None)
    assert resp['ok'] is True
    assert resp['intent']['action'] == 'analyze_data'
    assert len(resp['plan']['steps']) > 0
    assert resp['source'] == 'rules'
    # 历史应已持久化到临时文件
    assert (tmp_path / 'chat.json').exists()


def test_conversations_are_grouped_and_deleted_independently(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_assistant, 'HISTORY_PATH', str(tmp_path / 'chat.json'))
    # 归档也必须隔离：get_chat_conversations() 现以本地归档为准（主存只留 100 条，
    # 老会话会被截断，归档永不截断）。而 conftest 的 DT_CHAT_ARCHIVE_DIR 是整个
    # 测试会话共享的目录，不隔离就会读到其它测试写入的会话，令下面的精确断言随机失败。
    import chat_archive as _ca
    monkeypatch.setattr(_ca, 'ARCHIVE_DIR', str(tmp_path / 'arch'))
    cm = ChatManager()
    monkeypatch.setattr(ai_assistant, '_chat_manager', cm)

    ai_assistant.record_chat_exchange('第一个问题', '第一个回答', 'chat_one')
    ai_assistant.record_chat_exchange('第二个问题', '第二个回答', 'chat_two')

    conversations = ai_assistant.get_chat_conversations()
    assert [c['id'] for c in conversations] == ['chat_two', 'chat_one']
    assert [m['content'] for m in ai_assistant.get_chat_history('chat_one')] == [
        '第一个问题', '第一个回答'
    ]

    assert ai_assistant.delete_chat_conversation('chat_one') == 2
    assert ai_assistant.get_chat_history('chat_one') == []
    assert [m['content'] for m in ai_assistant.get_chat_history('chat_two')] == [
        '第二个问题', '第二个回答'
    ]
