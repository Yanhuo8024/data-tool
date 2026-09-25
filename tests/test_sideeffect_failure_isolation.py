# -*- coding: utf-8 -*-
"""
副作用失败隔离 回归测试
=======================

背景（真实缺陷 #5）
------------------
E2E-4 真实 LLM 跑通后暴露：终稿已生成，却在最后一步崩溃 ——

    _lg_finish  →  _remove_checkpoint  →  cp_file.unlink()
                                     →  SystemExit: 1     （环境删除守卫）

旧代码 `_remove_checkpoint` 只写 `except Exception`，而 **SystemExit 继承自
BaseException 而非 Exception**，因此直接穿透，把*已完成的终稿整个丢弃*。

同类风险点（均属旁路增强，失败绝不该影响主流程）：
  * ai_agent._save_checkpoint / _load_checkpoint / _remove_checkpoint
  * trace_store.event / end_run       （trace 落盘）
  * agent_long_memory.LongTermMemory._save  （长期记忆落盘）
  * _lg_finish 中的 trace / 长期记忆写入块

修复原则：显式捕获 `(Exception, SystemExit)`，但**不捕获 BaseException**，
以便 KeyboardInterrupt 仍能正常传播。

注意：本文件**刻意不使用 pytest 的 tmp_path 夹具、也不执行任何删除**。
原因是本机沙箱的批量删除守卫会拦截 pytest 的临时目录清理（逐轮累计计数），
属于环境限制而非被测代码问题；用项目内 .workbuddy/ 下的自有目录规避。
"""
import os
import sys
import uuid
from pathlib import Path

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import ai_agent  # noqa: E402
import trace_store  # noqa: E402
import agent_long_memory  # noqa: E402

# 项目内自有临时根（.workbuddy 被 U 盘同步脚本排除，不会污染产物）
_TMP_ROOT = Path(_ROOT) / ".workbuddy" / "unit_tmp"


def _fresh_dir(name: str) -> Path:
    d = _TMP_ROOT / f"{name}-{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True, exist_ok=True)
    return d


class _StubLT:
    def record_interaction(self, **kwargs):
        return None


def _sys_exit_unlink(self, missing_ok=False):
    raise SystemExit(1)


# --------------------------------------------------------------------------
# 1. checkpoint 三件套：SystemExit 必须被吞
# --------------------------------------------------------------------------
def test_remove_checkpoint_swallows_systemexit(monkeypatch):
    d = _fresh_dir("rm-cp")
    monkeypatch.setattr(ai_agent, "_checkpoint_dir", lambda: d)
    ai_agent._save_checkpoint("s-unit", {"messages": []})
    assert (d / "s-unit.json").exists()

    monkeypatch.setattr(Path, "unlink", _sys_exit_unlink)
    ai_agent._remove_checkpoint("s-unit")  # 不得抛出


def test_save_checkpoint_swallows_systemexit(monkeypatch):
    d = _fresh_dir("save-cp")
    monkeypatch.setattr(ai_agent, "_checkpoint_dir", lambda: d)

    def boom(self, *a, **k):
        raise SystemExit(1)

    monkeypatch.setattr(Path, "write_text", boom)
    ai_agent._save_checkpoint("s-unit", {"messages": []})  # 不得抛出


def test_load_checkpoint_swallows_systemexit(monkeypatch):
    d = _fresh_dir("load-cp")
    monkeypatch.setattr(ai_agent, "_checkpoint_dir", lambda: d)
    (d / "s-unit.json").write_text('{"session_id":"s-unit"}', encoding="utf-8")

    def boom(self, *a, **k):
        raise SystemExit(1)

    monkeypatch.setattr(Path, "read_text", boom)
    assert ai_agent._load_checkpoint("s-unit") is None  # 不得抛出


# --------------------------------------------------------------------------
# 2. 核心回归：终稿生成完成后，清理失败不得丢弃结果
# --------------------------------------------------------------------------
def test_lg_finish_survives_unlink_systemexit(monkeypatch):
    """复现 E2E-4 场景：unlink 抛 SystemExit，_lg_finish 仍须返回终稿。"""
    monkeypatch.setattr(ai_agent.llm_client, "chat",
                        lambda *a, **k: {"content": "这是最终分析报告正文。", "usage": {}})
    monkeypatch.setattr(ai_agent, "get_long_term", lambda uid="default": _StubLT())

    d = _fresh_dir("finish")
    monkeypatch.setattr(ai_agent, "_checkpoint_dir", lambda: d)
    cp = d / "unit-cp-finish.json"
    cp.write_text('{"session_id":"unit-cp-finish"}', encoding="utf-8")

    monkeypatch.setattr(Path, "unlink", _sys_exit_unlink)
    state = {
        "messages": [{"role": "user", "content": "分析一下"}],
        "calls_made": [],
        "last_content": "草稿内容",
        "usage_total": {},
        "iteration": 1,
        "cfg": {
            "llm_config": {}, "file_name": None, "session_id": "unit-cp-finish",
            "max_iterations": 1, "user_message": "分析一下", "runtime_token": None,
        },
    }
    out = ai_agent._lg_finish(state, truncated=False)  # 不得抛出

    assert out.get("ok") is True
    assert out.get("output"), "终稿不得因清理失败而丢失"
    assert out.get("end_status") == "done"


# --------------------------------------------------------------------------
# 3. trace 落盘：SystemExit 必须被吞
# --------------------------------------------------------------------------
def test_trace_event_swallows_systemexit(monkeypatch):
    monkeypatch.setattr(trace_store, "emit_span", lambda *a, **k: None)

    def boom(*a, **k):
        raise SystemExit(1)

    monkeypatch.setattr(trace_store.json, "dumps", boom)
    trace_store.event("run-unit", "unit_event", {"a": 1})  # 不得抛出


def test_trace_end_run_swallows_systemexit(monkeypatch):
    monkeypatch.setattr(trace_store, "event", lambda *a, **k: 0)
    monkeypatch.setattr(Path, "unlink", _sys_exit_unlink)
    trace_store.end_run("run-unit", "ok", {})  # 不得抛出


# --------------------------------------------------------------------------
# 4. 长期记忆落盘：SystemExit 必须被吞
# --------------------------------------------------------------------------
def test_long_term_save_swallows_systemexit(monkeypatch):
    d = _fresh_dir("ltm")
    monkeypatch.setattr(agent_long_memory, "_MEMORY_ROOT", d)
    monkeypatch.setattr(Path, "replace", lambda self, t: (_ for _ in ()).throw(SystemExit(1)))
    mem = agent_long_memory.LongTermMemory("unit-user")
    mem._save()  # 不得抛出


# --------------------------------------------------------------------------
# 5. 静态护栏
# --------------------------------------------------------------------------
def test_checkpoint_helpers_explicitly_catch_systemexit():
    src = open(os.path.join(_ROOT, "ai_agent.py"), encoding="utf-8").read()
    assert src.count("except (Exception, SystemExit)") >= 4, \
        "checkpoint / trace / 长期记忆块应显式捕获 SystemExit"


def test_no_bare_baseexception_swallow():
    """不得用裸 BaseException 吞掉 KeyboardInterrupt。"""
    for fn in ("ai_agent.py", "trace_store.py", "agent_long_memory.py"):
        src = open(os.path.join(_ROOT, fn), encoding="utf-8").read()
        assert "except BaseException" not in src, f"{fn} 不应出现裸 except BaseException"
