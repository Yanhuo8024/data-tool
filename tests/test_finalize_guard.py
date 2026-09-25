"""_finalize_agent_result 自校验守卫测试：诚实兜底文案不进自校验"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _mk_result(output):
    return {'ok': True, 'output': output, 'calls': [], 'usage': {}, 'elapsed_ms': 100}


def _fake_result_ok(output):
    """构造通过 _ensure_final_text 的原始输出（模拟 agent_loop 返回）"""
    from ai_agent import _ensure_final_text
    return {'ok': True, 'output': _ensure_final_text(output, [
        {'tool': 'web_search', 'params': {'query': 'q'}, 'status': 'ok'}]),
        'calls': [{'tool': 'web_search', 'params': {'query': 'q'}, 'status': 'ok'}],
        'usage': {}, 'elapsed_ms': 100}


def test_fallback_reply_skips_self_correct(monkeypatch):
    """初稿为诚实兜底（只有工具记录没报告）→ 不调用自校验，原样返回"""
    import ai_assistant

    called = {'n': 0}
    def _fake_self_correct(*a, **kw):
        called['n'] += 1
        return '（被自校验改写的评论表）'
    monkeypatch.setattr(ai_assistant, '_self_correct_answer', _fake_self_correct)

    prep = {'expert_ctx': '', 'calc_note': ''}
    result = _fake_result_ok('{"tool": "web_search", "params": {"query": "x"}}')
    out = ai_assistant._finalize_agent_result(result, '深入分析', None, None, prep, depth='deep')

    assert out and out['reply']
    assert called['n'] == 0, '兜底文案不应进入自校验'
    assert '已完成的资料查询' in out['reply'] or '未生成文字报告' in out['reply']


def test_real_draft_goes_through_self_correct(monkeypatch):
    """真实草稿（有实质内容）+ 本次出现工具调用失败 → 深度请求进入自校验"""
    import ai_assistant

    called = {'n': 0}
    def _fake_self_correct(*a, **kw):
        called['n'] += 1
        return '（校正后的终稿）'
    monkeypatch.setattr(ai_assistant, '_self_correct_answer', _fake_self_correct)

    prep = {'expert_ctx': '', 'calc_note': ''}
    draft = '## 核心结论\n' + '这是一份有实质内容的深度分析报告，' * 10
    result = _fake_result_ok(draft)
    result['trace'] = {'errors': 1}  # 出现过工具失败 → 触发自校验纠偏分支
    out = ai_assistant._finalize_agent_result(result, '深入分析', None, None, prep, depth='deep')

    assert called['n'] == 1
    assert out['reply'] == '（校正后的终稿）'


def test_clean_draft_skips_self_correct(monkeypatch):
    """真实草稿但无工具失败（干净轨迹）→ 不进入自校验（减少无用 LLM 调用）"""
    import ai_assistant

    called = {'n': 0}
    def _fake_self_correct(*a, **kw):
        called['n'] += 1
        return '（校正后的终稿）'
    monkeypatch.setattr(ai_assistant, '_self_correct_answer', _fake_self_correct)

    prep = {'expert_ctx': '', 'calc_note': ''}
    draft = '## 核心结论\n' + '这是一份有实质内容的深度分析报告，' * 10
    result = _fake_result_ok(draft)
    result['trace'] = {'errors': 0}  # 无工具失败 → 直接返冑，不额外花 LLM
    out = ai_assistant._finalize_agent_result(result, '深入分析', None, None, prep, depth='deep')

    assert called['n'] == 0
    assert '实质内容' in out['reply']
