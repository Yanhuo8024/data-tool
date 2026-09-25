"""意图识别加强项测试：Layer3 LLM 精排 / 后置校验 / DST 多轮联动 / 标注闭环"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


# ==================== _extract_first_json（JSON 校验清洗） ====================

def test_extract_first_json_variants():
    from intent_router import _extract_first_json
    assert _extract_first_json('{"intent": "nlq"}') == {'intent': 'nlq'}
    assert _extract_first_json('```json\n{"intent": "nlq", "confidence": 0.9}\n```') == {
        'intent': 'nlq', 'confidence': 0.9}
    assert _extract_first_json('好的，判断如下：{"intent": "analysis"} 以上。') == {'intent': 'analysis'}
    assert _extract_first_json('不是 JSON') is None
    assert _extract_first_json('') is None


# ==================== Layer3 LLM 精排 ====================

def test_refine_with_llm_parses_and_restricts(monkeypatch):
    """Layer3 只接受候选内意图；围栏/前后缀 JSON 能解析；带置信度与依据"""
    import llm_client
    import intent_router

    def fake_chat(messages, cfg, max_tokens=None, **kw):
        assert '候选意图' in messages[0]['content']
        return {'content': '```json\n{"intent": "analysis", "confidence": 0.85, "rationale": "要求解读评价"}\n```'}
    monkeypatch.setattr(llm_client, 'chat', fake_chat)

    r = intent_router.refine_with_llm('帮我评估一下', ['nlq', 'analysis'], {'provider': 'ollama'})
    assert r['intent'] == 'analysis'
    assert r['confidence'] == 0.85
    assert r['rationale']


def test_refine_rejects_out_of_candidates(monkeypatch):
    """候选之外意图 → 两次尝试后返回 None（回退前两层）"""
    import llm_client
    import intent_router

    monkeypatch.setattr(llm_client, 'chat',
                        lambda *a, **kw: {'content': '{"intent": "troubleshoot", "confidence": 0.9}'})
    r = intent_router.refine_with_llm('帮我评估一下', ['nlq', 'analysis'], {'provider': 'ollama'})
    assert r is None


def test_refine_low_confidence_flags_clarification(monkeypatch):
    import llm_client
    import intent_router

    monkeypatch.setattr(llm_client, 'chat',
                        lambda *a, **kw: {'content': '{"intent": "nlq", "confidence": 0.4}'})
    r = intent_router.refine_with_llm('看一眼', ['nlq', 'analysis'], {'provider': 'ollama'})
    assert r['intent'] == 'nlq'
    assert r['needs_clarification'] is True


def test_refine_without_llm_config_returns_none():
    from intent_router import refine_with_llm
    assert refine_with_llm('随便', ['nlq'], None) is None


# ==================== 后置校验 ====================

def test_validate_intent_unknown_falls_back():
    from intent_router import validate_intent
    r = validate_intent('x', {'intent': 'unknown_xx', 'confidence': 0.9})
    assert r['intent'] == 'chat' and r['needs_clarification'] is True


def test_validate_intent_high_risk_floor():
    """高风险意图（agent/computation）置信度低于下限 → 澄清而非硬走流程"""
    from intent_router import validate_intent
    r = validate_intent('x', {'intent': 'agent', 'confidence': 0.6})
    assert r['needs_clarification'] is True
    assert any('高风险' in m for m in r['missing_info'])
    r2 = validate_intent('x', {'intent': 'agent', 'confidence': 0.9})
    assert not r2.get('needs_clarification')


# ==================== DST 多轮联动 ====================

def test_continuation_boost_short_followup():
    """上一轮 analysis + 短句弱信号续接 → 继承 analysis（而非 chat）"""
    from intent_router import classify_intent
    hist = [{'intent': 'analysis'}]
    r = classify_intent('再看看它的回撤呢', hist)
    assert r['intent'] in ('analysis', 'nlq')


def test_continuation_no_override_on_strong_signal():
    """强信号（明确关键词）时不被续接覆盖"""
    from intent_router import classify_intent
    hist = [{'intent': 'analysis'}]
    r = classify_intent('画个柱状图', hist)
    assert r['intent'] == 'visualization'


def test_continuation_needs_history():
    """无历史时，无信号的纯语气短句不应被强行赋予任务意图"""
    from intent_router import classify_intent
    assert classify_intent('然后呢', None)['intent'] == 'chat'


# ==================== route() 接线 Layer3 ====================

def test_route_uses_layer3_on_ambiguity(monkeypatch):
    """歧义（提高分差阈值制造）+ 有 llm_config → route 采用 Layer3 精排结果"""
    import llm_client
    import intent_router

    def fake_chat(messages, cfg, max_tokens=None, **kw):
        return {'content': '{"intent": "knowledge", "confidence": 0.9, "rationale": "问公式原理"}'}
    monkeypatch.setattr(llm_client, 'chat', fake_chat)
    monkeypatch.setattr(intent_router, 'MARGIN_THRESHOLD', 99.0)  # 强制歧义 → 触发 Layer3

    r = intent_router.route('夏普比率怎么算的', llm_config={'provider': 'ollama'})
    assert r['intent'] == 'knowledge'
    assert r.get('layer3_rationale') == '问公式原理'


def test_route_without_llm_config_skips_layer3():
    """无 llm_config → Layer3 不触发（零额外成本），规则结果直接返回"""
    from intent_router import route
    r = route('帮我分析一下这个数据')
    assert r['intent'] in ('analysis', 'agent')


# ==================== 标注闭环：expected → 硬回归 ====================

def test_eval_user_case_with_expected_is_hard(tmp_path, monkeypatch):
    import eval_intent
    path = tmp_path / 'cases.jsonl'
    path.write_text(
        json.dumps({'q': '线上怪问题A', 'pred': 'chat', 'expected': 'analysis'}, ensure_ascii=False) + '\n' +
        json.dumps({'q': '线上怪问题B', 'pred': 'chat'}, ensure_ascii=False) + '\n',
        encoding='utf-8')
    cases = eval_intent._load_user_cases(str(path))
    by_q = {c['q']: c for c in cases}
    assert by_q['线上怪问题A']['intent'] == 'analysis'   # 已标注 → 硬回归
    assert by_q['线上怪问题B']['intent'] is None          # 未标注 → 软判定
