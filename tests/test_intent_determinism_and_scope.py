# -*- coding: utf-8 -*-
"""意图识别 P0/P1/P2 优化项的回归测试（2026-09-15）。

覆盖本轮修复的 8 类真实缺陷：
  P0-1 判定不可复现（set() 迭代 + 无 tie-break → 跨进程哈希随机漂移）
  P0-2 分层量纲失控（L2 可 +2.0，整层推翻 L1）
  P0-3 chat 兜底分 0.8 > 澄清阈值 → 永不澄清 / 混合意图被寒暄吞掉
  P0-4 无 out_of_scope 意图 + 关键词裸子串污染
  P1   断词变体缺失、normalize_query 自膨胀（夏普比率比率 / DIDDID）
  P2   评测分母虚高（54/48 = 112.5%）、测试污染生产难例回流文件

设计为完全离线、零额度、零文件删除（沙箱批量删除守卫环境下可安全运行）。
"""
import json
import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

import intent_router as IR  # noqa: E402


# ==================== P0-1 判定确定性 ====================

def test_ranking_has_explicit_tiebreak():
    """同分时必须按显式优先级判定，而不是受 dict/set 迭代顺序影响"""
    scores = {'nlq': 2.0, 'analysis': 2.0, 'agent': 2.0}
    order = [k for k, _ in IR._rank_scores(scores)]
    assert order[0] == 'nlq', f"nlq 应在 analysis/agent 之前胜出，实际 {order}"
    # 同分结果必须可重复
    assert [k for k, _ in IR._rank_scores(scores)] == order


def test_layer2_no_set_iteration():
    """回归探针：Layer2 融合不得再用 set(...) 建字典（哈希敏感的根源）"""
    src = open(os.path.join(_ROOT, 'intent_router.py'), encoding='utf-8').read()
    body = src.split('def classify_intent_layer2', 1)[1].split('\ndef ', 1)[0]
    assert 'set(' not in body, 'Layer2 又出现了 set() 迭代（会跨进程漂移）'
    assert '_ordered_intents' in body


@pytest.mark.parametrize('query,expected', [
    ('基金', 'nlq'),
    ('指标', 'knowledge'),
])
def test_ambiguous_short_input_is_stable_across_hash_seeds(query, expected):
    """同一句在 6 个不同 PYTHONHASHSEED 的独立进程里必须得到同一意图。

    修复前实测：'基金' 在 nlq/analysis/agent 之间随机漂移。"""
    code = ("import intent_router as IR,json;"
            f"print(IR.classify_intent({query!r})['intent'])")
    seen = set()
    for seed in ('0', '1', '2', '3', '7', '12345'):
        env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONDONTWRITEBYTECODE='1')
        out = subprocess.run([sys.executable, '-c', code], cwd=_ROOT, env=env,
                             capture_output=True, text=True, timeout=90)
        assert out.returncode == 0, out.stderr[-400:]
        seen.add(out.stdout.strip().splitlines()[-1])
    assert seen == {expected}, f"{query} 跨哈希种子漂移: {seen}"


# ==================== P0-2 分层量纲：L2 只能微调 ====================

def test_layer2_cannot_overturn_clear_leader():
    """L1 有明确领先者时，L2 相似度再高也不得翻盘"""
    # visualization L1 已 1.0，其余为 0
    kw = {'visualization': 1.0, 'analysis': 0.0, 'nlq': 0.0}
    merged = IR.classify_intent_layer2('可视化一下这个数据', kw)
    assert merged['visualization'] >= merged['analysis']
    assert merged['visualization'] > merged['analysis']


def test_layer2_only_breaks_ties_within_band():
    """与领先者同档（分差 ≤ 带宽）时才允许 L2 参与"""
    far = IR.classify_intent_layer2('画个柱状图', {'visualization': 1.0, 'chat': 0.0})
    assert far['chat'] == 0.0, "分差远大于带宽时 L2 不应给分"


def test_layer2_budget_bounded_by_l1():
    """L2 加成必须受预算约束，不能出现旧实现的 +2.0 量纲碾压"""
    merged = IR.classify_intent_layer2('帮我分析一下这个数据', {'analysis': 1.0})
    assert merged['analysis'] <= 1.0 + IR._L2_TIE_ADD


def test_no_layer2_dictatorship_when_l1_silent():
    """L1 全零时 L2 仍能定胜负，但预算必须低于澄清阈值（触发澄清而非硬猜）"""
    assert IR._L2_SILENT_ADD < IR.CLARIFICATION_THRESHOLD
    r = IR.classify_intent('指标')
    assert r['needs_clarification'] is True


# ==================== P0-3 兜底与混合意图 ====================

def test_acknowledgement_is_chat_without_clarification():
    """明确社交应答语 → 闲聊且不澄清（不能被兜底改成澄清）"""
    for q in ('嗯', '好的', '谢谢', '收到'):
        r = IR.classify_intent(q)
        assert r['intent'] == 'chat', q
        assert r['needs_clarification'] is False, q


def test_no_signal_triggers_clarification_not_guess():
    """无信号输入不得硬猜闲聊（旧实现 chat=0.8 > 阈值 → 永不澄清）"""
    for q in ('它呢', '内个', '然后呢'):
        r = IR.classify_intent(q)
        assert r['intent'] == 'chat', q
        assert r['needs_clarification'] is True, f"{q} 应当澄清而不是硬猜"


def test_mixed_greeting_and_business_prefers_business():
    """寒暄 + 业务诉求共存 → 业务诉求优先（旧实现被判 chat）"""
    r = IR.classify_intent('你好，帮我看看这个数据')
    assert r['intent'] != 'chat'
    assert r['scores']['chat'] < r['scores'][r['intent']]


def test_pure_greeting_stays_chat():
    r = IR.classify_intent('你好')
    assert r['intent'] == 'chat' and r['needs_clarification'] is False


# ==================== P0-4 域外意图 + 关键词边界 ====================

def test_out_of_scope_category_wired():
    """新意图必须完成全部映射（否则 validate_intent 会把它打回 chat）"""
    for intent in ('out_of_scope', 'computation'):
        assert intent in IR.INTENT_CATEGORIES
        assert intent in IR.INTENT_SPECS
        assert intent in IR.INTENT_TO_ACTION
        assert intent in IR.ROLE_PROMPTS
        assert intent in IR._CLARIFY_TEMPLATES


def test_out_of_scope_detected():
    for q in ('今天天气怎么样', '讲个笑话', '帮我写个作文', '现在几点了'):
        assert IR.classify_intent(q)['intent'] == 'out_of_scope', q


def test_out_of_scope_veto_on_domain_words():
    """含业务诉求时不得判域外（否则误伤真实分析请求）"""
    assert IR.classify_intent('分析一下天气对销量的影响')['intent'] != 'out_of_scope'
    assert IR.classify_intent('天气数据里的缺失值有多少')['intent'] != 'out_of_scope'


@pytest.mark.parametrize('kw,text,hit', [
    ('hi', 'which file', False),      # 英文子串污染
    ('hi', 'hi', True),
    ('chart', 'charts', True),        # 容忍复数
    ('500', '2020/5000', False),      # 数字子串污染
    ('500', '出现 500 错误', True),
    ('值', '这个项目的价值', False),    # 中文语境污染
    ('值', '这个指标的值', True),
    ('个数', '可视化一下这个数据', False),
    ('个数', '一共几个数据点', True),
    ('是什么', '是什么原因导致的', False),
    ('是什么', 'DID 是什么', True),
])
def test_keyword_boundary_and_context(kw, text, hit):
    assert IR.kw_hit(kw, text) is hit, f"kw_hit({kw!r},{text!r}) 期望 {hit}"


def test_dominant_trouble_signal_beats_operation():
    """硬故障词命中 → 排障优先于功能教程"""
    r = IR.classify_intent('上传文件报错了')
    assert r['intent'] == 'troubleshoot'
    assert IR.classify_intent('怎么上传文件')['intent'] == 'operation'


# ==================== P1 断词变体 / 归一化 ====================

def test_normalize_query_no_self_expansion():
    """normalize_query 不得自膨胀（'夏普比率'→'夏普比率比率'、'DID'→'DIDDID'）"""
    assert IR.normalize_query('夏普比率') == '夏普比率'
    assert 'DIDDID' not in IR.normalize_query('做个DID政策评估')
    assert '夏普比率比率' not in IR.normalize_query('夏普比率怎么算的')


def test_normalize_still_expands_colloquial():
    """口语同义词仍要归一（修复不能把功能删掉）"""
    assert '夏普比率' in IR.normalize_query('夏普是多少')


def test_word_segmentation_variants():
    assert IR.classify_intent('这份数据的质量如何')['intent'] == 'analysis'
    assert IR.classify_intent('推荐几只基金')['intent'] == 'nlq'
    assert IR.classify_intent('怎么把图表保存下来')['intent'] == 'operation'
    assert IR.classify_intent('数据加载不出来')['intent'] == 'troubleshoot'


def test_indicator_lookup_maps_to_nlq():
    """边界用例：'帮我看看收益率' 要数据值（nlq），不是解读（analysis）"""
    assert IR.classify_intent('帮我看看收益率')['intent'] == 'nlq'
    # 有分析语气时仍归分析
    assert IR.classify_intent('为什么收益率这么低')['intent'] == 'analysis'


def test_computation_intent_reachable():
    """计算类此前分类器永远返回不了（只有 eval 特判），现应可直达"""
    r = IR.classify_intent('算一下这个基金的总收益率')
    assert r['intent'] == 'computation'
    # action 必须是 chat，才能进到本地真实计算分支
    assert r['action'] == 'chat'
    # conf 需越过高风险下限，否则每次都被迫澄清
    assert r['confidence'] >= IR.INTENT_CONF_FLOORS['computation']


def test_alternatives_exposed():
    """混合意图候选对外可见（前 3 名）"""
    r = IR.classify_intent('帮我分析一下这个数据')
    alts = r.get('alternatives')
    assert isinstance(alts, list) and alts
    assert alts[0]['intent'] == r['intent']
    assert all(a['score'] > 0 for a in alts)


# ==================== P2 评测口径 + 数据卫生 ====================

def test_eval_denominator_not_inflated():
    """准确率不得 > 100%（此前分母只用内置集 → 112.5% 恒真）"""
    import eval_intent
    acc, fails = eval_intent.run_eval(verbose=False)
    assert 0.0 <= acc <= 1.0, f"准确率 {acc} 越界，分母口径仍有误"
    assert acc >= 0.95, f"意图评测准确率 {acc*100:.1f}% 低于 95%：{fails}"


def test_user_cases_annotated_are_hard():
    """回流难例补 expected 后应升级为硬回归并全部通过"""
    import eval_intent
    cases = eval_intent._load_user_cases()
    hard = [c for c in cases if c['intent'] is not None]
    assert hard, '回流难例未完成人工标注'
    assert 'user-case（已人工标注 → 硬回归）' in hard[0]['note']


def test_case_reflow_disabled_in_test_env():
    """测试环境不得把用例写进生产难例回流文件（P2 数据卫生）"""
    assert IR._CASES_REFLOW_ENABLED is False, 'TESTING=1 下难例回流应为关闭'
    before = open(IR._CASES_PATH, encoding='utf-8').read()
    IR.route('这条绝不会落盘的测试用例')
    after = open(IR._CASES_PATH, encoding='utf-8').read()
    assert before == after


def test_case_reflow_can_be_reenabled(monkeypatch):
    """显式开启后仍能正常落盘（开关本身可用；落盘到 .workbuddy 临时区，不碰生产文件）"""
    scratch = os.path.join(_ROOT, '.workbuddy', 'test_cases_scratch.jsonl')
    monkeypatch.setattr(IR, '_CASES_PATH', scratch)
    IR.set_case_reflow_enabled(True)
    try:
        IR.record_intent_case('测试用难例-不落生产文件', 'chat', 0.4)
        assert '测试用难例-不落生产文件' in open(scratch, encoding='utf-8').read()
    finally:
        IR.set_case_reflow_enabled(False)
    # 关闭后不再写入新条目
    IR.record_intent_case('关闭开关后不应新增的条目', 'chat', 0.4)
    assert '关闭开关后不应新增的条目' not in open(scratch, encoding='utf-8').read()


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-q']))
