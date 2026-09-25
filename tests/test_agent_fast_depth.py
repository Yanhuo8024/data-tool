# -*- coding: utf-8 -*-
"""快速模式限制思考深度。"""


def test_shallow_llm_config():
    import ai_assistant as A
    cfg, mt = A._shallow_llm_config({'provider': 'sensenova', 'model': 'glm-5.2'})
    assert cfg.get('reasoning_effort') == 'none'
    assert mt <= 400
    cfg2, mt2 = A._shallow_llm_config({'provider': 'zhipu'}, max_tokens=2000)
    assert cfg2.get('reasoning_effort') == 'none'
    assert mt2 <= 800


def test_fast_mode_blocks_deep():
    import ai_assistant as A
    assert A._depth_for_mode('deep', True) == 'balanced'
    assert A._depth_for_mode('deep', False) == 'deep'


def test_light_paths_use_shallow_config():
    import inspect
    import ai_assistant as A
    for fn in (A._direct_answer, A._web_search_answer, A._rag_answer, A._fast_chat_answer):
        src = inspect.getsource(fn)
        assert '_shallow_llm_config' in src, fn.__name__
