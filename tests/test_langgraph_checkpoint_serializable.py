# -*- coding: utf-8 -*-
"""
LangGraph MemorySaver checkpoint 可序列化 回归测试
=================================================

背景（真实缺陷）
----------------
`agent_loop` 把整个 `cfg` 放进 LangGraph 的 `initial_state`，而图挂载了
`MemorySaver` checkpointer —— 它在每个 super-step 后对状态做 msgpack 序列化。

旧代码 `cfg` 里含有两个不可序列化对象：
  * `progress_callback` —— 函数（Type is not msgpack serializable: function）
  * `memory_state`      —— 自定义会话记忆对象
结果是：只要环境装了 langgraph（requirements.txt 声明为必需），
真实 LLM 跑 agent 时第 2 轮就必然抛 `TypeError` 崩溃。

修复
----
把二者移出图状态，改挂模块级运行期注册表 `ai_agent._LG_RUNTIME`，
`cfg` 里只留 `runtime_token`（纯字符串）；节点通过 `_lg_cb(cfg)` /
`_lg_memory_state(cfg)` 取回，invoke 结束后在 finally 中回收 token。

本测试用**脚本化假 LLM**离线验证，不产生网络请求。
"""
import json
import os
import sys
from typing import Any, Dict, List

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

langgraph = pytest.importorskip("langgraph")  # 未装 langgraph 时自动跳过

import ai_agent  # noqa: E402


# --------------------------------------------------------------------------
# 假 LLM：按调用次序返回脚本化响应
# --------------------------------------------------------------------------
class _FakeLLM:
    """记录每次调用的 messages，按脚本返回响应。"""

    def __init__(self, script: List[Dict[str, Any]]):
        self.script = list(script)
        self.calls: List[List[Dict[str, Any]]] = []

    def chat(self, messages, llm_config=None, max_tokens=None, tools=None,
             tool_choice=None, **kwargs):
        self.calls.append(list(messages))
        if self.script:
            return self.script.pop(0)
        # 脚本耗尽后统一收敛，避免测试挂死在自环
        return {
            'content': '分析完成。\n<answer>已给出结论。</answer>',
            'tool_calls': None,
            'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15},
        }

    def chat_stream(self, messages, llm_config=None, max_tokens=None, tools=None,
                    tool_choice=None, **kwargs):
        """终稿合成走的是流式路径（`_final_answer_call` 在有 cb 时用 chat_stream），
        必须一并 stub —— 否则这个号称"离线"的测试会真的发网络请求。"""
        resp = self.chat(messages, llm_config, max_tokens, tools, tool_choice)
        text = (resp or {}).get('content') or ''
        if text:
            yield text


@pytest.fixture()
def no_plan(monkeypatch):
    """关闭 planner 的 LLM 规划，让图从 planner 透传 → agent。"""
    monkeypatch.setattr(ai_agent, "_should_plan", lambda *a, **k: False)


def _run_agent(monkeypatch, script, collected):
    fake = _FakeLLM(script)
    monkeypatch.setattr(ai_agent.llm_client, "chat", fake.chat)
    # 终稿合成（_final_answer_call）在**有 progress_callback 时走流式**，必须一并 stub。
    # 漏 stub 的后果不是"很快失败"，而是：真的发网络请求 → 耗时数秒起、且受
    # llm_client 自适应节流档位影响（前序测试触发过 429 就会把间隔抬到 30s），
    # 全量跑时表现为 output 为空 → 本测试误报失败（单独跑却通过）。
    monkeypatch.setattr(ai_agent.llm_client, "chat_stream", fake.chat_stream)
    monkeypatch.setattr(ai_agent, "_should_plan", lambda *a, **k: False)

    def cb(event):
        collected.append(event)

    return fake, ai_agent.agent_loop(
        user_message="帮我看看数据基本情况",
        llm_config={"provider": "zhipu", "model": "glm-4.7-flash",
                    "api_key": "test-key"},
        max_iterations=3,
        file_name=None,
        session_id=None,          # 用 'default' thread，避免写 checkpoint 文件
        progress_callback=cb,
    )


# --------------------------------------------------------------------------
# 1. 修复核心：真实跑通带回调的图，不再抛 msgpack 错误
# --------------------------------------------------------------------------
def test_agent_loop_completes_with_callback(monkeypatch):
    """回归：带 progress_callback 的真实图执行必须跑完（旧版此处必崩）。"""
    collected = []
    _, result = _run_agent(monkeypatch, [
        {'content': '先给出初步分析。\n<answer>数据共 100 行。</answer>',
         'tool_calls': None,
         'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}},
    ], collected)

    assert isinstance(result, dict)
    assert result.get("output"), "终稿输出不应为空"
    # 回调确实被触发（说明节点成功从注册表取回了函数对象）
    assert collected, "progress_callback 未被调用"


def test_callback_receives_progress_events(monkeypatch):
    """回调内容应包含 running/thinking 之类事件，证明外挂取回链路可用。"""
    collected = []
    _run_agent(monkeypatch, [
        {'content': '思考：先算一下统计量。\n<answer>结论 A</answer>',
         'tool_calls': None, 'usage': {}},
    ], collected)

    statuses = [e.get("status") for e in collected if isinstance(e, dict)]
    assert any(s == "running" for s in statuses), f"未收到 running 事件: {collected[:3]}"


# --------------------------------------------------------------------------
# 2. 图状态本身必须纯净（无函数 / 无自定义对象）
# --------------------------------------------------------------------------
def test_cfg_contains_no_callable(monkeypatch):
    """cfg 里不得再出现 progress_callback / memory_state 字段。"""
    captured = {}
    real_invoke = ai_agent._get_langgraph().invoke

    def spy(state, config=None, **kw):
        captured["state"] = state
        return real_invoke(state, config=config, **kw)

    monkeypatch.setattr(ai_agent._get_langgraph(), "invoke", spy)
    _run_agent(monkeypatch, [{'content': 'ok', 'tool_calls': None, 'usage': {}}], [])

    cfg = captured["state"]["cfg"]
    assert "progress_callback" not in cfg, "回调函数不应进入图状态"
    assert "memory_state" not in cfg, "记忆对象不应进入图状态"
    assert isinstance(cfg.get("runtime_token"), str) and cfg["runtime_token"]


def test_state_is_msgpack_serializable(monkeypatch):
    """状态整体必须能被 langgraph 的 serde 序列化 —— 这是 MemorySaver 的实际约束。"""
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    captured = {}
    real_invoke = ai_agent._get_langgraph().invoke

    def spy(state, config=None, **kw):
        captured["state"] = state
        return real_invoke(state, config=config, **kw)

    monkeypatch.setattr(ai_agent._get_langgraph(), "invoke", spy)
    _run_agent(monkeypatch, [{'content': 'ok', 'tool_calls': None, 'usage': {}}], [])

    serde = JsonPlusSerializer()
    try:
        serde.dumps_typed(captured["state"])
    except Exception as e:  # pragma: no cover - 修复失效时命中
        pytest.fail(f"图状态不可序列化，MemorySaver 会崩: {type(e).__name__}: {e}")


def test_module_registry_is_cleaned_after_run(monkeypatch):
    """invoke 结束后注册表必须回收 token，避免长跑进程内存泄漏。"""
    before = set(ai_agent._LG_RUNTIME.keys())
    _run_agent(monkeypatch, [{'content': 'ok', 'tool_calls': None, 'usage': {}}], [])
    after = set(ai_agent._LG_RUNTIME.keys())
    assert after == before, f"注册表未回收: 残留 {after - before}"


def test_registry_cleaned_even_on_failure(monkeypatch):
    """即使图执行抛异常，finally 也要回收 token。"""
    before = set(ai_agent._LG_RUNTIME.keys())

    def boom(*a, **k):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(ai_agent._get_langgraph(), "invoke", boom)
    fake = _FakeLLM([])
    monkeypatch.setattr(ai_agent.llm_client, "chat", fake.chat)
    monkeypatch.setattr(ai_agent, "_should_plan", lambda *a, **k: False)

    with pytest.raises(RuntimeError):
        ai_agent.agent_loop(
            user_message="x", llm_config={"provider": "zhipu", "model": "m"},
            max_iterations=2, file_name=None, session_id=None,
            progress_callback=lambda e: None,
        )

    after = set(ai_agent._LG_RUNTIME.keys())
    assert after == before, f"异常路径未回收: 残留 {after - before}"


def test_graph_invoke_failure_records_root_cause(monkeypatch, tmp_path):
    """图执行抛错时轨迹必须记录真实异常类型+消息。

    回归背景（2026-09-18）：旧版 run_end summary 只写 'graph invoke raised' 五个字，
    真实异常被吞，历史 23 次失败没有任何根因可溯。
    """
    import trace_store
    monkeypatch.setattr(trace_store, "TRACE_DIR", str(tmp_path))

    def boom(*a, **k):
        raise RuntimeError("simulated node crash")

    monkeypatch.setattr(ai_agent._get_langgraph(), "invoke", boom)
    fake = _FakeLLM([])
    monkeypatch.setattr(ai_agent.llm_client, "chat", fake.chat)
    monkeypatch.setattr(ai_agent, "_should_plan", lambda *a, **k: False)

    with pytest.raises(RuntimeError):
        ai_agent.agent_loop(
            user_message="x", llm_config={"provider": "zhipu", "model": "m"},
            max_iterations=2, file_name=None, session_id=None,
            progress_callback=lambda e: None,
        )

    files = list((tmp_path / "traces").glob("*.jsonl"))
    assert files, "异常路径必须落轨迹（P2-7 悬挂运行回归）"
    evs = [json.loads(l) for f in files for l in open(f, encoding="utf-8") if l.strip()]
    ends = [e for e in evs if e.get("type") == "run_end"]
    assert ends and ends[-1]["data"]["outcome"] == "error"
    s = ends[-1]["data"]["summary"]
    assert s.get("error") == "graph invoke raised"
    assert s.get("error_type") == "RuntimeError"
    assert "simulated node crash" in (s.get("error_detail") or "")
    # graph.error 事件同步落轨迹，供诊断页时间线展示
    assert any(e.get("type") == "graph.error" and
               (e.get("data") or {}).get("error_type") == "RuntimeError" for e in evs)


# --------------------------------------------------------------------------
# 3. 辅助函数行为
# --------------------------------------------------------------------------
def test_lg_cb_reads_from_registry():
    token = "lgrun-unit-test"
    fn = lambda e: None  # noqa: E731
    ai_agent._LG_RUNTIME[token] = {"progress_callback": fn, "memory_state": None}
    try:
        assert ai_agent._lg_cb({"runtime_token": token}) is fn
        assert ai_agent._lg_memory_state({"runtime_token": token}) is None
    finally:
        ai_agent._LG_RUNTIME.pop(token, None)


def test_lg_cb_fallback_without_registry():
    """单元测试直连节点的兼容路径：无 registry 时回落到状态内字段。"""
    fn = lambda e: None  # noqa: E731
    assert ai_agent._lg_cb({"progress_callback": fn}) is fn
    assert ai_agent._lg_cb({"runtime_token": "missing-token"}) is None


def test_three_nodes_no_longer_read_callback_from_cfg():
    """静态护栏：三个节点不得再直接 cfg['progress_callback']。"""
    src = open(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "ai_agent.py"),
        encoding="utf-8",
    ).read()
    assert "cfg['progress_callback']" not in src
    assert "cfg['memory_state']" not in src
