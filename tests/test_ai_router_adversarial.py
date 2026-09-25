# -*- coding: utf-8 -*-
"""AI 助手路由对抗回归测试（P0/P1/P2 修复的固化用例）。

覆盖此前实测暴露的三类问题：
  1. 意图识别不清：双轨派发漂移、"研究报告"落入"请上传文件"死胡同；
  2. 回答格式化：<thinking> 思维链标签泄漏为可见正文；
  3. 思考过度：无关问题无谓联网、depth 误判导致篇幅膨胀。

设计为**离线可运行**：monkeypatch 掉 LLM 与联网，零额度消耗。
可直接运行：  python tests/test_ai_router_adversarial.py
也可用 pytest：pytest tests/test_ai_router_adversarial.py -v

关键设计：stub 必须放在 fixture / `_main()` 中，不能在模块顶层执行。
否则会污染同 session 内其他测试（如 test_llm_client）的 mock 行为，
导致 llm_client.chat 被全局替换，pytest mock.patch('llm_client.requests.post') 不生效。
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.chdir(_ROOT)


# 全局 stub 状态：pytest fixture 会更新，__main__ 模式由 _main() 一次性安装
_A_GLOBAL = None

# __main__ 模式下的快照（pytest 模式改由 monkeypatch 自动还原，不依赖此表）
_STUB_SNAPSHOT = []


def _install_stubs(mp=None):
    """拦截 LLM / 联网 / 金融取数，保证测试离线、确定、快速。返回 ai_assistant 模块。

    mp 为 pytest 的 monkeypatch 时逐项注册（测试结束自动还原，杜绝跨文件污染）；
    为 None 时裸赋值并记录快照（供 __main__ 独立运行，由 _uninstall_stubs 还原）。

    注意：早期版本仅 reload 了 llm_client，却把 A._finance_live_context 等直接
    赋成哨兵且从不还原，导致同 session 内 test_stream_ux / test_ai_assistant_depth
    等用例拿不到真实的 _finance_live_context 而误判。此处统一走 mp 还原。
    """
    import llm_client
    import web_search
    import ai_assistant as A

    def fake_chat(messages, config=None, tools=None, tool_choice='auto', max_tokens=None, **kw):
        return {'content': '【MOCK-LLM回答】模拟回答正文。'}

    def fake_chat_stream(messages, config=None, max_tokens=None, **kw):
        for t in ['【MOCK', '-流式', '回答】']:
            yield t

    def fake_web_search(q, top_k=5, **kw):
        return {
            'ok': True,
            'results': [{'title': '模拟网页A', 'url': 'http://x.com/a', 'snippet': '模拟摘要A'}],
        }

    def fake_fin(*a, **k):
        return None  # 不触发真实金融取数

    def fake_agent(*a, **k):
        # 用哨兵替换 Agent 链路，隔离"分支选择"与"Agent 内部实现"
        return {'ok': True, 'reply': '【AGENT-哨兵】', 'source': 'agent'}

    def fake_nofile(*a, **k):
        return {'ok': True, 'reply': '【NOFILE-哨兵】', 'source': 'web'}

    targets = [
        (llm_client, 'chat', fake_chat),
        (llm_client, 'chat_stream', fake_chat_stream),
        (web_search, 'web_search', fake_web_search),
        (A, '_finance_live_context', fake_fin),
        (A, '_run_agent_analysis', fake_agent),
        (A, '_no_file_analysis_fallback', fake_nofile),
    ]
    for mod, name, val in targets:
        if mp is not None:
            mp.setattr(mod, name, val, raising=False)
        else:
            _STUB_SNAPSHOT.append((mod, name, getattr(mod, name, None)))
            setattr(mod, name, val)

    sys.modules['web_search'] = web_search
    return A


def _uninstall_stubs():
    """仅 __main__ 模式使用：按快照还原裸赋值（pytest 模式由 monkeypatch 自动还原）。"""
    while _STUB_SNAPSHOT:
        mod, name, orig = _STUB_SNAPSHOT.pop()
        try:
            setattr(mod, name, orig)
        except Exception:
            pass


def _reset():
    """清空 ai_assistant 内部缓存状态（每个用例前调用）。"""
    A = _A_GLOBAL
    if A is None:
        return
    try:
        A._chat_manager.history.clear()
    except Exception:
        pass
    try:
        A._chat_manager.context = {'last_file': None, 'last_action': None}
    except Exception:
        pass
    try:
        A._router_cache.clear()
    except Exception:
        pass
    try:
        from conversation_memory import get_memory
        get_memory().reset()
    except Exception:
        pass


# pytest autouse fixture：每个测试前安装 stub（走 monkeypatch，结束自动还原）
try:
    import pytest
    @pytest.fixture(autouse=True)
    def _auto_stubs(monkeypatch):
        global _A_GLOBAL
        _A_GLOBAL = _install_stubs(monkeypatch)
        yield _A_GLOBAL
        _A_GLOBAL = None
except ImportError:
    pass


# ==================== P0-1：思维链标签剥离 ====================

def test_strip_thinking_full_block():
    s = '<thinking>\n1. 拆解\n2. 数据\n</thinking>\n\n**结论**：低估。'
    out = _A_GLOBAL.strip_thinking(s)
    assert '<thinking>' not in out and '拆解' not in out
    assert '低估' in out


def test_strip_thinking_unclosed_and_dangling():
    A = _A_GLOBAL
    assert '<thinking>' not in A.strip_thinking('开头<thinking>进行到一半的推理')
    out = A.strip_thinking('结论在此</thinking>')
    assert '</thinking>' not in out and '结论在此' in out
    assert 'x' not in A.strip_thinking('```thinking\nx\n```\n正文')


def test_strip_thinking_noop_when_clean():
    clean = '这是一段正常回答，含 **加粗** 与 | 表格 |。'
    assert _A_GLOBAL.strip_thinking(clean) == clean


# ==================== P0-2：派发以 router 为准 ====================

def test_dispatch_prefers_router_operation():
    """'怎么导出报告' → operation，应走本地帮助，而不是被旧正则带进联网搜索。"""
    A = _A_GLOBAL
    _reset()
    res = A.process_chat_message('怎么导出报告', llm_config={'provider': 'zhipu'},
                                 current_file=None, mode='think')
    assert res.get('source') != 'web', f"不应联网: {res.get('source')}"
    assert '我可以做这些事' in (res.get('reply') or '')


def test_research_report_not_dead_end():
    """'做一份完整的研究报告' → 应进入分析链路，而不是'请上传文件'死胡同。"""
    A = _A_GLOBAL
    _reset()
    res = A.process_chat_message('做一份完整的研究报告', llm_config={'provider': 'zhipu'},
                                 current_file=None, mode='think')
    assert res.get('source') == 'agent', f"应进入 Agent: {res.get('source')}"
    assert '请上传' not in (res.get('reply') or '')


def test_methodology_question_not_ask_file():
    """'如何做好数据分析' → 方法论问题，给方法性回答而非要求上传数据。"""
    A = _A_GLOBAL
    _reset()
    res = A.process_chat_message('如何做好数据分析', llm_config={'provider': 'zhipu'},
                                 current_file=None, mode='think')
    assert res.get('source') in ('web', 'llm', 'knowledge'), f"意外链路: {res.get('source')}"
    assert '请上传' not in (res.get('reply') or '') and '还没有选择数据文件' not in (res.get('reply') or '')


# ==================== P1：联网必要性闸门 ====================

def test_web_gate_skips_trivial():
    A = _A_GLOBAL
    assert A._needs_web_search('请继续') is False
    assert A._needs_web_search('1+1等于几') is False
    assert A._needs_web_search('嗯') is False


def test_web_gate_allows_timely():
    A = _A_GLOBAL
    assert A._needs_web_search('腾讯最新股价') is True
    assert A._needs_web_search('现在一年期存款利率是多少') is True


def test_continue_not_web():
    A = _A_GLOBAL
    _reset()
    res = A.process_chat_message('请继续', llm_config={'provider': 'zhipu'},
                                 current_file=None, mode='think')
    assert res.get('source') != 'web', f"续接指令不应联网: {res.get('source')}"


# ==================== P1：深度判定精修 ====================

def test_depth_report_export_not_deep():
    from intent_router import determine_depth
    assert determine_depth('报告怎么导出') != 'deep'
    assert determine_depth('如何导出报告') != 'deep'


def test_depth_real_report_is_deep():
    from intent_router import determine_depth
    assert determine_depth('帮我生成一份研究报告') == 'deep'


def test_expert_context_depth_aware():
    """思维链仅在 deep 下注入：balanced 上下文不应包含 <thinking> 指令。"""
    A = _A_GLOBAL
    balanced = A._build_expert_context('分析一下这家公司的估值', depth='balanced')
    deep = A._build_expert_context('深入分析这家公司的估值', depth='deep')
    assert '<thinking>' not in balanced, "balanced 不应注入思维链"
    assert '<thinking>' in deep or '内部思维链' in deep, "deep 应注入思维链指令"


# ==================== P2：方法论识别 ====================

def test_methodology_detector():
    A = _A_GLOBAL
    assert A._is_methodology_question('如何做好数据分析') is True
    assert A._is_methodology_question('怎么做数据清洗') is True
    assert A._is_methodology_question('分析当前文件的销售额') is False


def test_deep_report_detector():
    A = _A_GLOBAL
    assert A._wants_deep_report('做一份完整的研究报告') is True
    assert A._wants_deep_report('全面分析电器行业') is True
    assert A._wants_deep_report('今天天气怎么样') is False


def _main():
    """__main__ 模式：手动安装 stub 后逐个跑测试。"""
    import traceback
    global _A_GLOBAL
    _A_GLOBAL = _install_stubs()
    try:
        tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith('test_') and callable(f)]
        passed, failed = 0, []
        for name, fn in tests:
            try:
                fn()
                passed += 1
                print(f"  PASS  {name}")
            except AssertionError as e:
                failed.append((name, str(e)))
                print(f"  FAIL  {name} :: {e}")
            except Exception:
                failed.append((name, traceback.format_exc().splitlines()[-1]))
                print(f"  ERR   {name}")
        print(f"\n共 {len(tests)} 项：通过 {passed}，失败 {len(failed)}")
        if failed:
            print("\n失败明细：")
            for n, e in failed:
                print(f"  - {n}: {e}")
            return 1
        return 0
    finally:
        _uninstall_stubs()


if __name__ == '__main__':
    sys.exit(_main())