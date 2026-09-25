# -*- coding: utf-8 -*-
"""Agent 优化回归：TRACE_DIR 动态解析 / 路由同步注册 / 首轮重试提示 / MiniBM25 / 质量提示。"""
import importlib
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402


def test_trace_dir_late_env_is_honored(monkeypatch, tmp_path):
    """import 之后再设 TRACE_DIR，_trace_dir 也必须跟随（防测试污染生产轨迹）。"""
    import trace_store
    # 模拟“先 import、后 set env”，且模块属性为空
    monkeypatch.setattr(trace_store, "TRACE_DIR", "")
    monkeypatch.setenv("TRACE_DIR", str(tmp_path / "late-traces"))
    resolved = trace_store._trace_dir()
    assert "late-traces" in str(resolved)
    assert resolved.exists()


def test_trace_dir_module_override_wins(monkeypatch, tmp_path):
    """测试 monkeypatch 模块属性时，应优先于环境变量。"""
    import trace_store
    override = tmp_path / "monkey-traces"
    monkeypatch.setattr(trace_store, "TRACE_DIR", str(override))
    monkeypatch.setenv("TRACE_DIR", str(tmp_path / "env-should-lose"))
    resolved = trace_store._trace_dir()
    assert str(override) in str(resolved)
    assert "env-should-lose" not in str(resolved)


def test_resolve_trace_root_priority(monkeypatch, tmp_path):
    import trace_store
    monkeypatch.setattr(trace_store, "TRACE_DIR", "")
    monkeypatch.delenv("TRACE_DIR", raising=False)
    assert trace_store._resolve_trace_root() == ""
    monkeypatch.setenv("TRACE_DIR", "C:/tmp/from-env")
    assert trace_store._resolve_trace_root() == "C:/tmp/from-env"
    monkeypatch.setattr(trace_store, "TRACE_DIR", "C:/tmp/from-mod")
    assert trace_store._resolve_trace_root() == "C:/tmp/from-mod"


def test_agent_hard_routes_are_core():
    """agent/knowledge/llm/scheduler/file 必须同步注册，消除首批请求 404 竞态。"""
    import routes as routes_mod
    core = {e[0] for e in routes_mod._CORE_REGISTRARS}
    lazy = {e[0] for e in routes_mod._LAZY_REGISTRARS}
    for name in ("agent_routes", "knowledge_routes", "llm_routes",
                 "scheduler_routes", "file_routes", "ai_assistant_routes"):
        assert name in core, f"{name} 必须在 _CORE_REGISTRARS"
        assert name not in lazy, f"{name} 不应仍在 _LAZY_REGISTRARS"


def test_first_round_retry_prompt_file_aware():
    """有数据文件时首轮重试提示必须点名文件工具，不得退回通用 native 文案。"""
    from ai_agent import _first_round_retry_prompt, NO_TOOLS_PROMPT, NO_CALLS_NATIVE_RETRY
    p_file = _first_round_retry_prompt(use_native=True, has_file=True)
    assert "columns" in p_file or "describe" in p_file
    assert "tool_calls" in p_file  # 原生路径保留调用格式约束
    assert p_file != NO_CALLS_NATIVE_RETRY

    p_file_text = _first_round_retry_prompt(use_native=False, has_file=True)
    assert p_file_text == NO_TOOLS_PROMPT or "columns" in p_file_text

    p_nofile_native = _first_round_retry_prompt(use_native=True, has_file=False)
    assert p_nofile_native == NO_CALLS_NATIVE_RETRY


def test_no_tools_prompt_lists_file_tools():
    from prompts import get_prompt
    text = get_prompt("no_tools_retry")
    assert "columns" in text
    assert "数据文件" in text or "已挂载" in text


def test_minibm25_scores_prefer_overlap():
    from hybrid_retrieval import _bm25_scores, _MiniBM25, retrieval_status
    docs = [
        "宁德时代动力电池市占率全球第一",
        "贵州茅台白酒估值与批价",
        "某公司营收增长与毛利率",
    ]
    scores = _bm25_scores("宁德时代 市占率", docs)
    assert len(scores) == 3
    assert scores[0] > scores[1]
    assert scores[0] > scores[2]
    st = retrieval_status()
    assert st["bm25"] is True
    # 无 rank_bm25 时 impl 应为 mini
    assert st["bm25_impl"] in ("rank_bm25", "mini")


def test_audit_fact_sources_reads_result_brief():
    from ai_agent import _audit_fact_sources
    calls = [{
        "tool": "web_search",
        "params": {"query": "x"},
        "result_preview": "short",
        "result_brief": "行业规模2845.6亿元，同比增长12.3%",
    }]
    text = "报告显示行业规模约2846亿元，同比约12.3%。"
    note, stats = _audit_fact_sources(text, calls)
    # 约数应能通过 result_brief 溯源，未核验率应较低
    assert stats["total"] >= 2
    assert stats["ratio"] < 0.5


def test_zero_tool_with_file_warns_in_result_contract():
    """有文件却零成功工具时，结果契约必须带 no_tool_with_file 标记（前端可提示）。"""
    import inspect
    import ai_agent
    src = inspect.getsource(ai_agent.agent_loop)
    assert "no_tool_with_file" in src
    assert "数据可信度提示" in src


def test_empty_finalize_no_longer_fake_success():
    """空终稿不得再标 ok=True；_ensure_final_text 必须返回可读兜底文案。"""
    import inspect
    from ai_agent import _ensure_final_text, _lg_finish
    text = _ensure_final_text("", [])
    assert text and "未能生成" in text or "报告" in text
    src = inspect.getsource(_lg_finish)
    assert "empty_output" in src
    assert "'ok': True" not in src.split("return {'messages'")[-1][:400]


def test_conftest_trace_isolation_still_active():
    import trace_store
    assert "dt-test-traces-" in (trace_store.TRACE_DIR or os.environ.get("TRACE_DIR", "")) \
        or "dt-test-traces-" in (os.environ.get("TRACE_DIR") or "")


# ---------------- TF-IDF 矩阵缓存键（2026-09-19） ----------------
# 旧实现用 id(docs) 当缓存键：CPython 复用已回收列表的地址（实测紧循环新建
# 2000 个 list 只产生 1 个不同 id），导致"新语料命中旧矩阵"—— 得分条数与 docs
# 条数不符，少了会被 hybrid_search_with_meta 的 except 静默吞掉（TF-IDF 路
# 悄悄消失）。实测 300 次不同长度调用错配 56 次。改为内容指纹后必须恒为 0。


def test_tfidf_cache_key_matches_doc_count_in_tight_loop():
    """紧循环换语料（长度不同）时，得分条数必须与 docs 条数一致。"""
    import hybrid_retrieval as hr
    hr._tfidf_index.clear()
    for n in (3, 5, 8, 4, 6):
        docs = [f'文档{i} 唯一词{i} 内容' for i in range(n)]
        scores = hr._tfidf_scores('唯一词', docs)
        assert len(scores) == n, f'docs={n} 但返回 {len(scores)} 个得分（缓存键错配）'


def test_tfidf_cache_hits_on_same_content_new_list():
    """内容相同但对象不同（新 list）→ 应命中缓存且结果一致。"""
    import hybrid_retrieval as hr
    hr._tfidf_index.clear()
    docs = ['信用评分卡 WOE 分箱', '长期护理保险 DID 双重差分', '久期凸性 利率风险']
    a = hr._tfidf_scores('双重差分', docs)
    keys_after_first = len(hr._tfidf_index)
    b = hr._tfidf_scores('双重差分', list(docs))     # 内容相同的新对象
    assert keys_after_first == 1
    assert len(hr._tfidf_index) == 1, '相同内容不应新增缓存条目'
    assert a == b


def test_tfidf_cache_is_bounded():
    """缓存必须有上限，长跑不会无界增长。"""
    import hybrid_retrieval as hr
    hr._tfidf_index.clear()
    for i in range(hr._TFIDF_INDEX_CAP + 6):
        hr._tfidf_scores('查询', [f'语料{i}-{j} 内容' for j in range(3)])
    assert len(hr._tfidf_index) <= hr._TFIDF_INDEX_CAP
