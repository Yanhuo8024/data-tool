"""AI 助手意图明确性 / 深度自适应 / 防幻觉护栏测试（全部离线，不打真实 LLM）"""
import pytest


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    """屏蔽真实 LLM 调用：任何 chat/chat_stream 一律抛错，走离线兜底/护栏路径"""
    import llm_client
    def _boom(*a, **kw):
        raise RuntimeError("llm disabled in test")
    monkeypatch.setattr(llm_client, 'chat', _boom, raising=False)
    monkeypatch.setattr(llm_client, 'chat_stream', _boom, raising=False)


# ==================== 深度自适应 ====================

def test_depth_style_varies_by_depth():
    from ai_assistant import _depth_style
    deep = _depth_style('deep')
    concise = _depth_style('concise')
    balanced = _depth_style('balanced')
    # deep 要求反面视角与适用边界（有理有据、不一面之词）
    assert '反方' in deep and '边界' in deep
    # concise 严格收紧
    assert '3 句话' in concise
    # balanced 先结论后依据
    assert '先结论' in balanced or '先给结论' in balanced
    assert deep != concise != balanced


def test_message_depth_detection():
    from ai_assistant import _message_depth
    assert _message_depth("深入分析一下这家公司值不值得买") == 'deep'
    assert _message_depth("简要说说什么是PE") == 'concise'
    assert _message_depth("你好") == 'balanced'


# ==================== 意图明确性：概念问句护栏 ====================

def test_is_concept_question():
    from ai_assistant import _is_concept_question
    assert _is_concept_question("什么是回归分析", {"intent": "knowledge"}) is True
    assert _is_concept_question("夏普比率怎么算的", {"intent": "knowledge"}) is True
    assert _is_concept_question("什么是回归分析", {"intent": "analysis"}) is False
    assert _is_concept_question("什么是回归分析", None) is False


def test_concept_question_not_answered_with_file_demand():
    """『什么是数据分析』在无文件、无 LLM、知识库未命中时，
    不得答非所问地要求用户上传文件（历史缺陷），应走聊天兜底/知识路径。"""
    from ai_assistant import process_chat_message
    r = process_chat_message("什么是数据分析", llm_config=None, current_file=None, mode='fast')
    reply = r.get('reply', '')
    assert '选择数据文件' not in reply and '选择文件' not in reply, f"概念问句被引导上传文件: {reply[:80]}"


# ==================== 防幻觉：真实计算护栏 ====================

def test_real_compute_guard_for_unimplemented_intent():
    """已识别计算意图（t检验/PSI）但无自动计算器时，必须注入显式护栏而非静默跳过"""
    from real_compute import compute_and_render
    r = compute_and_render("做个t检验看看两组差异是否显著", None)
    assert r["kind"] == "hypothesis"
    assert "严禁编造" in r["block"]

    r2 = compute_and_render("看一下这个评分卡的PSI漂移情况", None)
    assert r2["kind"] == "stability"
    assert "严禁编造" in r2["block"]


def test_real_compute_no_file_guard():
    """有计算器但无文件时，护栏必须明确『无法计算真实数值』"""
    from real_compute import compute_and_render
    r = compute_and_render("帮我算一下夏普比率", None)
    assert r["kind"] == "fund_metrics"
    assert "未选择数据文件" in r["block"]


def test_real_compute_no_intent_no_block():
    from real_compute import compute_and_render
    r = compute_and_render("你好呀", None)
    assert r["kind"] is None and r["block"] == ""


# ==================== 无文件行业/标的分析 ====================

def test_industry_intent_detected_as_sector():
    """行业/板块/产业类问题应命中 sector_analysis 子意图（行业研究），而非行情/操作"""
    from finance_intent import detect_finance
    for q in ("全面分析电器行业", "分析一下白酒板块", "电器行业前景怎么样", "半导体产业分析一下"):
        fin = detect_finance(q)
        assert fin["is_finance"] and fin["subintent"] == "sector_analysis", f"{q} -> {fin}"
    # 指数/大盘行情仍是 market_quote
    fin = detect_finance("上证指数现在多少点")
    assert fin["subintent"] == "market_quote"


def test_sector_query_not_blocked_by_clarify():
    """『电器行业前景怎么样』即便通用路由低置信，也不应被澄清拦截（金融路由子意图豁免）"""
    from ai_assistant import _maybe_clarify, _route_intent
    r = _route_intent("电器行业前景怎么样")
    c = _maybe_clarify("电器行业前景怎么样", r, None)
    assert c is None, f"行业分析被澄清拦截: {c}"


def test_has_analyzable_target():
    from ai_assistant import _has_analyzable_target
    assert _has_analyzable_target("全面分析电器行业") is True
    assert _has_analyzable_target("分析一下茅台") is True
    assert _has_analyzable_target("分析一下") is False


def test_industry_analysis_without_file(monkeypatch):
    """『全面分析电器行业』无文件时应直接进入分析（Agent→联网兜底），绝不回'请上传文件'"""
    import ai_assistant

    monkeypatch.setattr(ai_assistant, '_finance_live_context', lambda m: None, raising=False)
    agent_called = {}
    def _fake_agent(message, llm_config, current_file, depth=None, **kw):
        # **kw 必须保留：新增 fast 形参（⚡快速档）后，签名过窄的桩会以 TypeError
        # 伪装成业务回归（与 _throttle_cloud 的 model 参数同源陷阱）。
        agent_called['file'] = current_file
        return {'ok': True, 'reply': '（模拟 Agent 行业研究报告）', 'source': 'agent',
                'suggestions': [], 'intent': {'action': 'analyze_data'}}
    monkeypatch.setattr(ai_assistant, '_run_agent_analysis', _fake_agent)

    r = ai_assistant.process_chat_message("全面分析电器行业", llm_config=None,
                                          current_file=None, mode='fast')
    assert r.get('source') == 'agent'
    assert '选择数据文件' not in r.get('reply', '')
    assert agent_called.get('file') is None  # 无文件也执行


def test_industry_analysis_agent_failure_falls_back_to_web(monkeypatch):
    """Agent 不可用（额度/超时）时，无文件行业分析兜底走联网研究，不回'要文件'提示"""
    import ai_assistant

    monkeypatch.setattr(ai_assistant, '_finance_live_context', lambda m: None, raising=False)
    monkeypatch.setattr(ai_assistant, '_run_agent_analysis',
                        lambda *a, **kw: None)  # Agent 失败
    monkeypatch.setattr(ai_assistant, '_web_search_answer',
                        lambda msg, cfg, role_prompt='', depth='balanced', extra_context='':
                        {'ok': True, 'reply': '（模拟联网行业研究）', 'source': 'web'})

    r = ai_assistant.process_chat_message("全面分析电器行业", llm_config=None,
                                          current_file=None, mode='fast')
    assert r.get('source') == 'web'
    assert '选择数据文件' not in r.get('reply', '')


def test_generic_analysis_without_target_still_guided(monkeypatch):
    """无文件且无任何分析目标（如只说'帮我分析'）→ 仍给引导，但文案告知可直接分析行业/股票"""
    import ai_assistant

    monkeypatch.setattr(ai_assistant, '_finance_live_context', lambda m: None, raising=False)

    r = ai_assistant.process_chat_message("帮我分析", llm_config=None,
                                          current_file=None, mode='fast')
    reply = r.get('reply', '')
    assert '选择文件' in reply or '选择数据文件' in reply
    # 文案应告知无文件也能分析行业/标的
    assert '电器行业' in reply and '没有文件' in reply


def test_deep_finance_question_not_shortcircuited(monkeypatch):
    """思考模式下『深入分析茅台值不值得买』应绕过 200 字快答短路，进入 Agent 全报告链路。
    Agent 不可用时回退规则流程（有文件引导），但绝不能返回 finance_live 短答案。"""
    import ai_assistant

    called = {'agent': False}
    def _fake_agent(message, llm_config, current_file, depth=None, **kw):
        called['agent'] = True
        return None  # 模拟 Agent 不可用
    monkeypatch.setattr(ai_assistant, '_run_agent_analysis', _fake_agent)
    # 屏蔽真实金融接口（离线）
    monkeypatch.setattr(ai_assistant, '_finance_live_context', lambda m: None, raising=False)

    r = ai_assistant.process_chat_message("深入分析茅台值不值得买", llm_config=None,
                                          current_file=None, mode='think')
    # 若走到路由层，说明未被 finance_live 200 字快答拦截
    assert r is not None
    assert r.get('source') != 'finance_live'
