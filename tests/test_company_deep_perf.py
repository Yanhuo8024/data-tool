#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_company_deep_perf.py — 企业深析的**性能与健壮性**回归守卫

对应 2026-10-02 的实测优化（基线：茅台全链路 ≈122s 串行）：

| 优化项 | 实测依据 | 本文件守卫 |
|---|---|---|
| `_snapshot` TTL 缓存 | 单次 3.3–5.1s，全管线约 9 次重复取 | 缓存命中/TTL=0/不固化失败 |
| 竞品并行取数 | `get_competitors` 串行 **30.06s** | 并行加速 + 顺序保持 + 失败不丢条目 |
| 草稿关思考 | 不关思考单次 24.83s 且**输出截断** | `reasoning_effort='none'` 必须透传 |
| 草稿识别截断 | `finish_reason=='length'` 未被读取 | 截断时**提高预算**重试 |
| 阶段超时真正生效 | `with ThreadPoolExecutor` 的 `__exit__` 会 `wait=True` | 超时耗时 < 阶段实际耗时 |
| 草稿超时不留空 | 原 60s 超时把整个阶段砍掉 | 退回规则草稿，字段非空 |
| `ctx["market"]` 类型陷阱 | 超时后滞留为字符串 → assemble 崩 | 非 dict 一律免疫 |

全部离线：网络与 LLM 均以 stub 注入。
"""
import sys
import time
import os
import importlib

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import company_analysis as CA
import company_deep_assemble as ASSEMBLE
import company_deep_draft as DRAFT
import company_deep_pipeline as PIPE
import company_deep_sources as SOURCES
import market_snapshot as MS
import valuation as V


# ── 通用桩：把外部取数全部换成确定性小函数 ──────────────
@pytest.fixture()
def offline(monkeypatch):
    """桩掉企业深析的全部外部依赖（网络 + LLM），返回可调用的安装器。"""
    def _install(market=None, sleep_market=0.0):
        monkeypatch.setattr(CA, "resolve_ticker", lambda c: ("600519", "a"))
        monkeypatch.setattr(CA, "code_to_name", lambda c: "测试公司")
        monkeypatch.setattr(CA, "get_company_profile",
                            lambda c, m: {"name": "测试公司", "industry": "白酒Ⅱ"})
        monkeypatch.setattr(CA, "get_financial_analysis", lambda c, m: {
            "ok": True,
            "ratios": {"profitability": {"revenue": 100.0, "net_profit": 5.0,
                                         "gross_margin": 91.0, "roe": 31.0},
                       "growth": {"revenue_growth": 5.0, "profit_growth": 6.0}},
            "statements": {"assets": 300.0, "equity": 200.0, "ocf": 10.0}})
        monkeypatch.setattr(CA, "get_news", lambda *a, **k: [])
        monkeypatch.setattr(CA, "get_competitors", lambda c: [])
        # 🚨 竞品为空会触发 stage_news 的「同业兜底」→ industry_peers 打东财全市场业绩报表
        #    （实测 23 页进度条、数十秒）→ 必须一并桩掉，否则用例会真的联网。
        monkeypatch.setattr(SOURCES, "industry_peers", lambda *a, **k: [])
        # 🚨 draft 阶段会取分部数据（akshare stock_zygc_em）—— **必须桩掉**，
        #    否则这个「离线」夹具仍会真联网，超时用例的耗时断言会随网络抖动而假失败。
        import segment_data as SD
        monkeypatch.setattr(SD, "fetch_segments", lambda *a, **k: {
            "ok": False, "segments": [], "period": None,
            "gap": {"reason": "离线测试已桩掉分部接口",
                    "source": "年报「分部资料」附注",
                    "fields": ["收入", "同比", "占比", "毛利率"]}})
        monkeypatch.setattr(CA, "get_industry_stats_em", lambda c: {})
        monkeypatch.setattr(CA, "get_industry_aggregates", lambda i, n: {})
        monkeypatch.setattr(PIPE, "_valuation_percentiles", lambda c: (None, None))
        if sleep_market:
            monkeypatch.setattr(PIPE, "stage_market",
                                lambda c, ctx: (time.sleep(sleep_market), {})[1])
        elif market is not None:
            monkeypatch.setattr(CA, "get_market_data", lambda c: dict(market))
            monkeypatch.setattr(CA, "get_dcf_valuation", lambda c: {})
    _install()
    return _install


# ── `_snapshot` TTL 缓存 ──────────────────────────────
def _stub_snapshot(monkeypatch, price=10.0):
    """把底层取数桩成计数函数，返回调用计数字典。"""
    calls = {"n": 0}

    def _fake(sym):
        calls["n"] += 1
        return {"price": price, "code": sym, "name": "N-" + str(sym)}
    monkeypatch.setattr(V, "_snapshot_uncached", _fake)
    monkeypatch.setattr(V, "_SNAP_CACHE", {})
    return calls


def test_快照缓存命中不重复取数(monkeypatch):
    calls = _stub_snapshot(monkeypatch)
    for _ in range(3):
        V._snapshot("600519")
    assert calls["n"] == 1, f"缓存未生效：底层被调 {calls['n']} 次（期望 1）"


def test_快照缓存_不同标的互不干扰(monkeypatch):
    calls = _stub_snapshot(monkeypatch)
    V._snapshot("600519")
    V._snapshot("000858")
    V._snapshot("600519")
    assert calls["n"] == 2


def test_快照缓存_TTL为零时禁用(monkeypatch):
    calls = _stub_snapshot(monkeypatch)
    monkeypatch.setattr(V, "_SNAP_TTL", 0.0)
    V._snapshot("600519")
    V._snapshot("600519")
    assert calls["n"] == 2, "TTL=0 应关闭缓存（便于排查线上陈旧数据）"


def test_快照缓存不固化失败结果(monkeypatch):
    """取数失败（无 price）不得写缓存，否则一次抖动会污染整个 TTL 窗口。"""
    failed = {"n": 0}

    def _fail(sym):
        failed["n"] += 1
        return {"price": None, "code": sym, "name": sym}
    monkeypatch.setattr(V, "_snapshot_uncached", _fail)
    monkeypatch.setattr(V, "_SNAP_CACHE", {})
    V._snapshot("600519")
    V._snapshot("600519")
    assert failed["n"] == 2, "失败结果被缓存了（应每次重试）"


def test_快照缓存返回副本不被外部改写(monkeypatch):
    """缓存命中必须返回副本：否则调用方一次原地修改会污染后续所有读取。"""
    _stub_snapshot(monkeypatch)
    a = V._snapshot("600519")
    a["price"] = 999.0
    b = V._snapshot("600519")
    assert b["price"] == 10.0, "缓存被调用方就地改写了"


# ── 竞品并行取数 ──────────────────────────────────────
def test_竞品并行取数快于串行(monkeypatch):
    def _slow(sym):
        time.sleep(0.30)
        return {"price": 1.0, "code": sym, "name": "N-" + str(sym)}
    monkeypatch.setattr(V, "_snapshot_uncached", _slow)
    monkeypatch.setattr(V, "_SNAP_CACHE", {})
    peers = ["000858", "000568", "600809"]
    t0 = time.time()
    rows = MS.competitor_rows("600519", peers, lambda c: c)
    dt = time.time() - t0
    assert len(rows) == 3
    assert dt < 0.30 * 3 * 0.75, f"疑似仍串行：3 个 0.3s 任务耗时 {dt:.2f}s"


def test_竞品并行取数保持入参顺序(monkeypatch):
    """「营收前 N」的次序本身有信息量，并发不得打乱。"""
    def _v(sym):
        return {"price": 1.0, "code": sym, "name": "N-" + str(sym)}
    monkeypatch.setattr(V, "_snapshot_uncached", _v)
    monkeypatch.setattr(V, "_SNAP_CACHE", {})
    peers = ["600809", "000568", "000858"]          # 故意非字典序
    rows = MS.competitor_rows("600519", peers, lambda c: c)
    assert [r["code"] for r in rows] == peers


def test_竞品取数失败不丢条目(monkeypatch):
    """宁可只留代码，也不能因为一家取不到就让竞品列表少一家。"""
    def _boom(sym):
        if sym == "000568":
            raise RuntimeError("取数失败")
        return {"price": 1.0, "code": sym, "name": "N-" + str(sym)}
    monkeypatch.setattr(V, "_snapshot_uncached", _boom)
    monkeypatch.setattr(V, "_SNAP_CACHE", {})
    rows = MS.competitor_rows("600519", ["000858", "000568", "600809"], lambda c: "名-" + c)
    assert [r["code"] for r in rows] == ["000858", "000568", "600809"]
    assert rows[1]["name"] == "名-000568", "失败项应回落到 code_to_name"


def test_竞品为空列表时零调用(monkeypatch):
    calls = _stub_snapshot(monkeypatch)
    assert MS.competitor_rows("600519", [], lambda c: c) == []
    assert calls["n"] == 0


# ── AI 草稿：关思考 / 识别截断 / 递增预算 ──────────────
def _install_draft_ctx(monkeypatch):
    ctx = {"name": "测试公司", "code": "600519",
           "profile": {"industry": "白酒Ⅱ"}, "market": {"pe": 22.0},
           "financial_analysis": {"ratios": {"profitability": {"revenue": 100.0}}},
           "statements": {}}
    return ctx


def test_草稿必须关思考(monkeypatch):
    """不关思考时 reasoning 会吃满 max_tokens → 正文截断（实测 24.83s 且不可解析）。"""
    seen = {}

    def _chat(messages, config=None, **kw):
        seen.update(config or {})
        seen["_max_tokens"] = kw.get("max_tokens")
        return {"content": '{"company": {"oneliner": "ok"}}', "finish_reason": "stop"}
    monkeypatch.setattr("llm_client.chat", _chat)
    DRAFT._ai_draft(_install_draft_ctx(monkeypatch))
    assert seen.get("reasoning_effort") == "none", "草稿未关思考"


def test_草稿尊重调用方显式指定的思考档(monkeypatch):
    seen = {}
    monkeypatch.setattr("llm_client.chat",
                        lambda m, c=None, **k: (seen.update(c or {}),
                                                {"content": '{"a": 1}', "finish_reason": "stop"})[1])
    DRAFT._ai_draft(_install_draft_ctx(monkeypatch), {"reasoning_effort": "medium"})
    assert seen.get("reasoning_effort") == "medium", "不应覆盖调用方显式设置"


def test_草稿截断时提高预算重试(monkeypatch):
    """`finish_reason=='length'` = 被 max_tokens 截断 → 重试必须加大预算，而非原样重发。"""
    budgets = []

    def _chat(messages, config=None, **kw):
        budgets.append(kw.get("max_tokens"))
        if len(budgets) == 1:
            return {"content": '{"company": {"oneli', "finish_reason": "length"}
        return {"content": '{"company": {"oneliner": "ok"}}', "finish_reason": "stop"}
    monkeypatch.setattr("llm_client.chat", _chat)
    DRAFT._ai_draft(_install_draft_ctx(monkeypatch))
    assert len(budgets) == 2, "截断后应重试一次"
    assert budgets[1] > budgets[0], f"重试预算未提高：{budgets}"


def test_草稿格式错误时不提高预算(monkeypatch):
    """非截断的格式失败 → 不值得加大预算，只需再要一次纯 JSON。"""
    budgets = []

    def _chat(messages, config=None, **kw):
        budgets.append(kw.get("max_tokens"))
        return {"content": "抱歉，我无法完成", "finish_reason": "stop"}
    monkeypatch.setattr("llm_client.chat", _chat)
    with pytest.raises(ValueError):
        DRAFT._ai_draft(_install_draft_ctx(monkeypatch))
    assert len(budgets) == 2
    assert budgets[1] == budgets[0], f"非截断不应改预算：{budgets}"


def test_草稿两次都截断时抛出并交由上层降级(monkeypatch):
    monkeypatch.setattr("llm_client.chat",
                        lambda m, c=None, **k: {"content": "{", "finish_reason": "length"})
    with pytest.raises(ValueError) as ei:
        DRAFT._ai_draft(_install_draft_ctx(monkeypatch))
    assert "截断" in str(ei.value), "错误信息应说明是截断而非格式问题"


# ── 阶段超时：必须真的不阻塞 ──────────────────────────
def test_阶段超时必须真的不阻塞(offline, monkeypatch):
    """🚨 回归守卫：`with ThreadPoolExecutor(...)` 的 `__exit__` 会 `shutdown(wait=True)`，
    把"超时"变成"照样等满"。一旦有人改回 `with`，本用例会因耗时过大而失败。"""
    offline(sleep_market=1.0)
    monkeypatch.setattr(PIPE, "STAGE_TIMEOUT_S", 0.2)
    t0 = time.time()
    r = PIPE.run_pipeline("测试公司", use_ai=False)
    dt = time.time() - t0
    assert dt < 0.9, f"超时未生效（耗时 {dt:.2f}s ≈ 阶段自身 1.0s，说明仍在等待）"
    assert r["ok"] is True
    mk = next(s for s in r["stages"] if s["id"] == "market")
    assert mk["status"] == "warn" and mk["error_code"] == "STAGE_TIMEOUT"


def test_草稿超时后必须退回规则草稿而非留空(offline, monkeypatch):
    """原先 60s 超时会把整个草稿阶段砍掉 → 用户白等且字段全空。"""
    monkeypatch.setattr(PIPE, "stage_draft", lambda *a, **k: (time.sleep(1.0), {})[1])
    monkeypatch.setattr(PIPE, "STAGE_TIMEOUT_S", 0.2)
    r = PIPE.run_pipeline("测试公司", use_ai=False)
    dr = next(s for s in r["stages"] if s["id"] == "draft")
    assert dr["status"] == "warn" and dr["error_code"] == "AI_DRAFT_TIMEOUT"
    assert r["ok"] is True
    assert r["state"]["company"]["oneliner"], "草稿超时后字段为空（应有规则草稿兜底）"


# ── `ctx["market"]` 类型陷阱（曾经的 assemble 崩溃根因）──
@pytest.mark.parametrize("bad_market", ["a", "hk", None, 0, [], "A股"])
def test_assemble对market非dict免疫(bad_market):
    ctx = {"name": "测试公司", "code": "600519", "market_kind": "a",
           "market": bad_market, "profile": {"industry": "白酒"},
           "financial_analysis": {"ratios": {}}, "statements": {}}
    out = ASSEMBLE.stage_assemble("测试公司", ctx)
    assert isinstance(out.get("state"), dict)
    assert out["state"]["company"]["market"] in ("A股", "港股")


def test_assemble在market键缺失时也能跑通():
    ctx = {"name": "测试公司", "code": "600519", "market_kind": "hk",
           "profile": {}, "financial_analysis": {"ratios": {}}, "statements": {}}
    out = ASSEMBLE.stage_assemble("测试公司", ctx)
    assert out["state"]["company"]["market"] == "港股", "缺失时应按 market_kind 判定"


def test_resolve不得把市场标签写进ctx_market(offline):
    """根治点：`ctx["market"]` 专供行情 dict，只能由 stage_market 写入。
    一旦 resolve 又把标签写进去，类型多态陷阱就会复活。"""
    ctx = {}
    PIPE.stage_resolve("600519", ctx)
    assert ctx.get("market_kind") == "a"
    assert "market" not in ctx, "resolve 不得写入 ctx['market']（会与行情 dict 撞键）"


def test_市场阶段失败时ctx_market恒为dict(offline, monkeypatch):
    """不变量：任何阶段跑完后 `ctx["market"]` 要么缺失、要么是 dict，绝不能是字符串。"""
    offline(sleep_market=1.0)
    monkeypatch.setattr(PIPE, "STAGE_TIMEOUT_S", 0.2)
    seen = {}
    orig_risk = PIPE.stage_risk

    def _spy(company, ctx):
        seen["type"] = type(ctx.get("market")).__name__
        return orig_risk(company, ctx)
    monkeypatch.setattr(PIPE, "stage_risk", _spy)
    PIPE.run_pipeline("测试公司", use_ai=False)
    assert seen.get("type") == "dict", f"market 阶段超时后类型为 {seen.get('type')}"
