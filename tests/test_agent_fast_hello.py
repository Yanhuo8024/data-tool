# -*- coding: utf-8 -*-
import time


def test_pure_greeting_detected():
    import ai_assistant as A
    assert A._is_pure_greeting('你好')
    assert A._is_pure_greeting('hi')
    assert A._is_pure_greeting('在吗')
    assert not A._is_pure_greeting('你好，帮我看看这个数据')
    assert not A._is_pure_greeting('分析贵州茅台')


def test_greeting_process_is_local_and_fast():
    import ai_assistant as A
    t = time.time()
    r = A.process_chat_message('你好', {'provider': 'sensenova', 'model': 'glm-5.2'}, mode='fast')
    dt = time.time() - t
    assert r.get('reply', '').startswith('你好呀')
    assert r.get('source') == 'fast'
    assert dt < 0.5, dt


def test_greeting_stream_local():
    import ai_assistant as A
    t = time.time()
    events = list(A.stream_chat_message('你好', {'provider': 'sensenova', 'model': 'glm-5.2'}, mode='fast'))
    dt = time.time() - t
    assert dt < 0.5, dt
    done = [e for e in events if e.get('type') == 'done']
    assert done
    reply = (done[-1].get('data') or {}).get('reply') or ''
    assert '你好呀' in reply


def test_fast_chat_uses_no_reasoning():
    import inspect
    import ai_assistant as A
    src = inspect.getsource(A._fast_chat_answer)
    assert "_shallow_llm_config" in src
    assert 'max_tokens' in src
