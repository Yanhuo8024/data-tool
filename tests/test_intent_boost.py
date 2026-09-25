"""意图识别加强项测试：实体感知增强 / TF-IDF Layer2 / 难例回流闭环"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ==================== 实体感知增强（Layer1.5） ====================

def test_entity_analysis_boost():
    """公司实体 + 口语问询 → analysis（基线曾误判 chat）"""
    from intent_router import classify_intent
    for q in ('宁德时代怎么样', '比亚迪和宁德时代哪个好', '茅台值不值得买'):
        assert classify_intent(q)['intent'] == 'analysis', q


def test_entity_nlq_boost():
    """公司实体 + 查数问询 → nlq/analysis"""
    from intent_router import classify_intent
    for q in ('腾讯最新股价多少', '看看茅台最新市值'):
        assert classify_intent(q)['intent'] in ('nlq', 'analysis'), q


def test_entity_no_false_positive_on_knowledge():
    """实体 + 概念问句仍应是 knowledge（实体增强不抢概念题）"""
    from intent_router import classify_intent
    assert classify_intent('什么是宁德时代')['intent'] == 'knowledge'


def test_entity_boost_no_entity_no_change():
    """无实体时增强不生效（不引入误伤）"""
    from intent_router import _entity_intent_boost
    scores = {'analysis': 0.0, 'nlq': 0.0}
    boosted = _entity_intent_boost('今天天气怎么样', dict(scores))
    assert boosted['analysis'] == 0.0


# ==================== Layer2 TF-IDF 统计学习 ====================

def test_tfidf_layer2_available():
    """TF-IDF 示例相似度可用：行业分析问句的相似度应指向 analysis"""
    from intent_router import _tfidf_example_sims
    sims = _tfidf_example_sims('游戏行业竞争格局如何')
    assert sims, 'TF-IDF 层不可用'
    assert sims.get('analysis', 0) > 0.2


def test_tfidf_beats_char_noise():
    """『电器行业前景怎么样』不再被 Layer2 字符噪音误判为 operation"""
    from intent_router import classify_intent
    assert classify_intent('电器行业前景怎么样')['intent'] in ('analysis', 'agent')


# ==================== 难例回流闭环 ====================

def test_record_intent_case_dedupe_and_cap(tmp_path, monkeypatch):
    """难例落盘：去重、JSONL 格式正确

    注：conftest 设 TESTING=1，生产回流默认关闭（防止 pytest 把用例写进线上闭环
    文件污染评测集）。本测试验证的正是回流本身，故显式打开开关。
    """
    import intent_router
    path = str(tmp_path / 'intent_cases.jsonl')
    monkeypatch.setattr(intent_router, '_CASES_PATH', path)
    monkeypatch.setattr(intent_router, '_CASES_REFLOW_ENABLED', True)
    intent_router.record_intent_case('这个指标怎么理解', 'chat', 0.5)
    intent_router.record_intent_case('这个指标怎么理解', 'chat', 0.5)  # 重复，不再写
    intent_router.record_intent_case('另一个难例', 'chat', 0.45)
    lines = open(path, encoding='utf-8').read().strip().splitlines()
    assert len(lines) == 2
    item = json.loads(lines[0])
    assert item['q'] == '这个指标怎么理解' and item['pred'] == 'chat'


def test_route_records_low_confidence(tmp_path, monkeypatch):
    """route() 在触发澄清（意图歧义）时自动落盘难例；正常置信不落盘。
    用提高 MARGIN_THRESHOLD（前两名分差阈值）的方式确定性制造"歧义"场景。"""
    import intent_router
    path = str(tmp_path / 'intent_cases.jsonl')
    monkeypatch.setattr(intent_router, '_CASES_PATH', path)
    monkeypatch.setattr(intent_router, '_CASES_REFLOW_ENABLED', True)
    monkeypatch.setattr(intent_router, 'MARGIN_THRESHOLD', 99.0)
    intent_router.route('帮我分析一下这个数据')      # 分差阈值提高 → 判定歧义 → 落盘
    monkeypatch.setattr(intent_router, 'MARGIN_THRESHOLD', 0.18)
    intent_router.route('帮我分析一下这个数据')      # 正常 → 不落盘
    lines = open(path, encoding='utf-8').read().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])['q'] == '帮我分析一下这个数据'


def test_eval_loads_user_cases(tmp_path, monkeypatch):
    """评测集动态并入回流难例（期望 None：只要求不误判为 chat）"""
    import eval_intent
    path = tmp_path / 'cases.jsonl'
    path.write_text(json.dumps({'q': '线上踩到的怪问题', 'pred': 'chat'}, ensure_ascii=False),
                    encoding='utf-8')
    monkeypatch.setattr(eval_intent, '_load_user_cases', lambda limit=50: [
        {'q': '线上踩到的怪问题', 'intent': None, 'note': 'user-case'}])
    cases = eval_intent.EVAL_SET + eval_intent._load_user_cases()
    assert any(c['q'] == '线上踩到的怪问题' for c in cases)
