# -*- coding: utf-8 -*-
"""问候回复与思考泄漏防护。"""
from text_clean import strip_thinking, strip_meta_chain, looks_like_meta_chain


def test_meta_chain_detected_and_stripped():
    raw = (
        "分析用户输入：用户说\"你好\"。\n"
        "2. 理解上下文：这是一个对话的开始，用户在打招呼。\n"
        "3. 确定回复策略：快而准、中文、口语化、不超过3句话。\n"
        "* 合规要求：虽然只是打招呼，但系统提示要求必须自动附上相关表述。\n"
        "4. 草拟回复：\n"
        "你好呀！👋 我是你的数据助手，有什么数据或金融分析需要帮忙的吗？随时告诉我哦\n"
        "不要暴露思考的过程，直接输出内容"
    )
    assert looks_like_meta_chain(raw) is True
    out = strip_meta_chain(raw)
    assert out.startswith('你好呀') or '数据助手' in out
    assert '分析用户输入' not in out
    assert '确定回复策略' not in out
    assert '合规要求' not in out


def test_strip_thinking_also_drops_meta_chain():
    raw = (
        "分析用户输入：用户说你好。\n理解上下文：打招呼。\n草拟回复：\n你好呀！👋 有什么可以帮你？"
    )
    out = strip_thinking(raw)
    assert '分析用户输入' not in out
    assert '你好呀' in out


def test_plain_reply_untouched():
    text = '你好呀！👋 我是你的数据助手，有什么数据或金融分析需要帮忙的吗？'
    assert strip_meta_chain(text) == text
    assert strip_thinking(text) == text


def test_greeting_is_short_canned():
    import inspect
    import ai_assistant as A
    src = inspect.getsource(A)
    assert '你好呀！👋 我是你的数据助手' in src
    # 本地问候不应再塞长能力清单
    assert "✅ 快速模式秒回 / 🧠 思考模式联网深挖" not in src.split("action == 'greeting'")[1][:500]


def test_fast_chat_forbids_thinking_leak():
    from prompts import get_prompt
    t = get_prompt('fast_chat_system')
    assert '不要输出任何思考' in t or '推理' in t
    assert '寒暄' in t or '招呼' in t
