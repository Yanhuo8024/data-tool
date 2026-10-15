#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_company_deep.py — 企业深析（企业深度分析）回归测试

覆盖 2026-10-02 优化的 7 项缺陷，防回归：
  P0-1 会话隔离：不同会话键的状态互不可见，且跨线程仍保持
  P0-2 排雷清单回写 state.red（原先恒为 {}）
  P0-3 AI 草稿的 facts 必须含资产负债表/现金流
  P1   护城河量纲 0-10 且区分度足够；moat_avg 不得把 0 当 5
  P1   排雷口径唯一：网页 alerts 集合 == 报告 derive_metrics().alerts 集合
  P1   同业对比回填；AI 失败不得记成成功
  P2   LLM JSON 容错解析；阶段超时

全部离线：所有数据源与 LLM 均以 stub 注入，零网络、零额度。
"""
import concurrent.futures as cf
import copy
import importlib
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import company_analysis as CA
import company_deep_llm as LLM
import company_deep_pipeline as PIPE
import company_deep_rules as RULES
import company_deep_session as SESS
import company_deep_sources as SOURCES

# ── 样本：同一份「教科书级问题公司」数据，供多组用例共用 ──
PROFILE = {"name": "问题公司", "code": "000001", "industry": "通用制造",
           "market_cap_yi": 100.0, "desc": "制造与销售"}
RATIOS = {
    "profitability": {"revenue": 100.0, "net_profit": 5.0, "gross_margin": 15.0,
                      "net_margin": 5.0, "roe": 6.0, "eps": 0.3},
    "solvency": {"debt_ratio": 70.0, "current_ratio": 0.9},
    "growth": {"revenue_growth": -8.0, "profit_growth": 5.0},
    "operational": {"receivable_days": 90.0},
    "dupont": {"equity_multiplier": 3.0, "asset_turnover": 0.6},
}
MARKET = {"price": 10.0, "pe": 55.0, "pb": 2.0, "roe": 6.0, "gross_margin": 15.0,
          "net_margin": 5.0, "debt_ratio": 70.0, "revenue_yi": 100.0,
          "net_profit_yi": 5.0, "market_cap_yi": 100.0, "high_52": 12.0,
          "low_52": 8.0, "mkt": "A股", "rev_growth": -8.0}
STMT = {"report_date": "2025-12-31", "assets": 310.0, "equity": 90.0, "cash": 60.0,
        "gw": 60.0, "inventory": 40.0, "ar": 30.0, "idebt": 120.0,
        "ocf": -10.0, "fcf": -20.0, "arG": 80.0, "invG": 90.0,
        "gmD": -5.0, "payout": 0.0}
STRONG_RATIOS = {
    "profitability": {"revenue": 1500.0, "net_profit": 780.0, "gross_margin": 91.0,
                      "net_margin": 52.0, "roe": 31.0, "eps": 62.0},
    "solvency": {"debt_ratio": 18.0, "current_ratio": 3.2},
    "growth": {"revenue_growth": 16.0, "profit_growth": 19.0},
    "operational": {"receivable_days": 3.0},
    "dupont": {"equity_multiplier": 1.3, "asset_turnover": 1.2},
}
STRONG_MARKET = dict(MARKET, pe=22.0, pb=8.0, roe=31.0, gross_margin=91.0,
                     net_margin=52.0, debt_ratio=18.0, market_cap_yi=21000.0,
                     rev_growth=16.0)
STRONG_STMT = dict(STMT, assets=2700.0, equity=2200.0, gw=0.0, ocf=800.0,
                   fcf=700.0, arG=2.0, invG=3.0, gmD=0.5)


@pytest.fixture()
def stubbed(monkeypatch):
    """注入问题公司样本；返回一个可切换强/弱样本的 setter。"""
    def _install(market=None, ratios=None, profile=None, stmt=None, competitors=None):
        monkeypatch.setattr(CA, "get_company_profile", lambda c, m: dict(profile or PROFILE))
        monkeypatch.setattr(CA, "get_financial_analysis",
                            lambda c, m: {"ok": True, "ratios": ratios or RATIOS,
                                          "dupont": (ratios or RATIOS)["dupont"],
                                          "statements": stmt or STMT})
        monkeypatch.setattr(CA, "get_market_data", lambda c: dict(market or MARKET))
        monkeypatch.setattr(CA, "get_dcf_valuation", lambda c: {"dcf": None, "peer": None})
        monkeypatch.setattr(CA, "get_news", lambda *a, **k: [{"title": "新闻A"}])
        monkeypatch.setattr(CA, "get_competitors",
                            lambda c: competitors if competitors is not None else [])
        monkeypatch.setattr(CA, "get_industry_stats_em",
                            lambda c: {"industry": "通用制造", "report_date": "2025-12-31",
                                       "company_count": 50, "industry_rev_yi": 900.0,
                                       "share_rev": 11.1, "rev_yoy": 4.0, "cr3": 35.0,
                                       "cr5": 52.0,
                                       "peers_top": ["龙头A", "问题公司", "跟随B"]})
        monkeypatch.setattr(CA, "get_industry_aggregates", lambda i, n: {})
        monkeypatch.setattr(CA, "resolve_ticker", lambda c: ("000001", "a"))
        monkeypatch.setattr(CA, "code_to_name", lambda c: (profile or PROFILE)["name"])
        monkeypatch.setattr(PIPE, "_valuation_percentiles", lambda code: (None, None))
        # 🚨 两处真联网源必须桩掉（offline 语义），否则 30 个用例要跑 40+ 秒：
        # ① draft 阶段的分部数据（akshare stock_zygc_em）；
        # ② news 阶段的「同业兜底」（东财全市场业绩报表，实测数十秒）。
        import segment_data as SD
        monkeypatch.setattr(SD, "fetch_segments", lambda *a, **k: {
            "ok": False, "segments": [], "period": None,
            "gap": {"reason": "离线测试已桩掉分部接口",
                    "source": "年报「分部资料」附注",
                    "fields": ["收入", "同比", "占比", "毛利率"]}})
        monkeypatch.setattr(SOURCES, "industry_peers", lambda *a, **k: [])
    _install()
    return _install


def _run(company="问题公司", **kw):
    return PIPE.run_pipeline(company, **kw)


# ── P0-1 会话隔离 ──────────────────────────────────────
def test_会话状态按会话键隔离():
    ta = SESS.set_key("unit:A")
    SESS.set_state({"company": {"name": "甲公司"}}, "甲公司")
    tb = SESS.set_key("unit:B")
    SESS.set_state({"company": {"name": "乙公司"}}, "乙公司")
    assert SESS.get_state()["company"]["name"] == "乙公司"
    SESS.set_key("unit:A")
    assert SESS.get_state()["company"]["name"] == "甲公司", "A 会话被 B 覆盖（跨会话串用回归）"
    assert SESS.get_name() == "甲公司"
    SESS.set_key("unit:B")
    SESS.clear()
    assert SESS.get_state() == {}
    SESS.reset_key(tb)
    SESS.reset_key(ta)


def test_未知会话不得回落到他人数据():
    t = SESS.set_key("unit:ONLY")
    SESS.set_state({"company": {"name": "唯一公司"}})
    SESS.reset_key(t)
    assert SESS.get_state("unit:NEVER") == {}, "未知会话读到了别人的分析结果"


def test_会话键可跨线程传递():
    """线程池不复制 contextvars → 必须靠 wrap() 在工作线程内重绑。"""
    t = SESS.set_key("unit:THR")
    try:
        SESS.set_state({"company": {"name": "线程公司"}})
        with cf.ThreadPoolExecutor(max_workers=1) as ex:
            got = ex.submit(SESS.wrap(lambda: SESS.get_state().get("company", {}).get("name"))).result()
        assert got == "线程公司", "工作线程丢失会话键（会退化成共享 default 桶）"
    finally:
        SESS.reset_key(t)


def test_状态容器有容量上限与诊断不含业务数据():
    st = SESS.stats()
    assert {"keys", "max_keys", "ttl_s", "ages_s", "current_key"} <= set(st)
    assert "state" not in json.dumps(st, ensure_ascii=False), "诊断输出泄漏了业务数据"


# ── P0-2 排雷清单回写 ─────────────────────────────────
def test_排雷清单必须回写state_red(stubbed):
    st = _run()["state"]
    assert st["red"]["fin"], "state.red.fin 仍为空 → 前端 20 项清单不会自动勾选"
    assert isinstance(st["red"]["alerts"], list) and st["red"]["alerts"]
    ids = set(st["red"]["fin"])
    assert ids <= set(RULES.REDFLAG_IDS), f"勾选了未知清单项: {ids - set(RULES.REDFLAG_IDS)}"


def test_有danger信号时结论不得停留在轻度(stubbed):
    st = _run()["state"]
    has_danger = any(a["level"] == "danger" for a in st["red"]["alerts"])
    assert has_danger, "本样本应触发 danger 级信号"
    assert st["red"]["verdict"] == "重度", "存在 danger 级排雷信号，结论却未升级"


# ── P1 排雷口径唯一（网页 == 报告 == AI 工具）──────────
def test_网页与报告的排雷结论必须同源(stubbed):
    st = _run()["state"]
    from company_deep_report import derive_metrics
    web = {a["label"] for a in st["red"]["alerts"]}
    rep = {t for t, _ in derive_metrics(st)["alerts"]}
    assert web == rep, f"网页与导出文档口径不一致，差异={web ^ rep}"


def test_排雷包含利润增长与毛利率背离():
    """该规则原先只在 JS 里有，Python 三个实现都缺 → 导出文档少 1 条。"""
    m = RULES.norm_fin({"npG": 8.0, "gmD": -5.0})
    labels = {a["label"] for a in RULES.redflag_alerts(m)}
    assert "利润增长与毛利率背离" in labels


def test_七条排雷规则均可触发():
    cases = {
        "现金流背离": {"ocf": -10.0, "np": 5.0},
        "疑似存贷双高": {"cash": 60.0, "idebt": 120.0, "assets": 310.0},
        "应收增速超营收": {"arG": 80.0, "revG": -8.0},
        "存货异常膨胀": {"invG": 90.0, "revG": -8.0},
        "商誉占净资产过高": {"gw": 60.0, "equity": 90.0},
        "资产负债率偏高": {"assets": 310.0, "equity": 75.0},
        "利润增长与毛利率背离": {"npG": 8.0, "gmD": -5.0},
    }
    for label, fin in cases.items():
        labels = {a["label"] for a in RULES.redflag_alerts(RULES.norm_fin(fin))}
        assert label in labels, f"规则「{label}」未触发：{fin}"


def test_ai排雷工具与规则层同源(monkeypatch):
    """原先该工具把 6 条规则逐条复制，现已委托单一实现。"""
    import agent_tool_registry as ATR
    monkeypatch.setattr(SESS, "get_state", lambda key=None: {"finance": STMT})
    monkeypatch.setattr(ATR, "_cd_state", lambda: {"finance": STMT})
    out = ATR.tool_company_deep_redflags(ocf=-10.0, net_profit=5.0)
    assert "现金流背离" in out, out[:200]


# ── P0-3 AI 草稿输入 ──────────────────────────────────
def test_ai草稿facts必须含资产负债表与现金流():
    f = LLM.build_facts({"statements": STMT, "profile": PROFILE, "market": MARKET,
                         "financial_analysis": {"ratios": RATIOS},
                         "name": "问题公司", "code": "000001"})
    bs = f.get("balance_sheet_and_cashflow") or {}
    for k in ("assets", "equity", "idebt", "cash", "ocf", "fcf", "gw", "arG", "invG"):
        assert k in bs, f"facts 缺少 {k}（AI 无法评判「账靠不靠谱」）"


def test_ai提示词护城河量纲是0到10():
    """控件 max=10、总分 /10、AI 工具描述 "each 0-10" → 提示词不得写 1-5。"""
    p = LLM.build_prompt("X", {})
    assert "0-10" in p
    assert "`moat` 六维" in p and "1-5" in p, "两类量纲必须在提示词里同时写明"


def test_finance_view与state_finance同源(stubbed):
    """排雷输入必须同时含利润与现金流，否则漏「现金流背离」。"""
    st = _run()["state"]
    ctx_like = {"statements": STMT, "financial_analysis": {"ratios": RATIOS}}
    a = RULES.norm_fin(RULES.finance_view(ctx_like))
    b = RULES.norm_fin(st["finance"])
    assert a["ocf_np"] is not None and b["ocf_np"] is not None
    assert abs(a["ocf_np"] - b["ocf_np"]) < 1e-6


# ── P1 护城河量纲与区分度 ─────────────────────────────
def test_护城河六维必须落在0到10():
    out = RULES.moat_scores(RULES.norm_fin({"gross_margin": 400.0, "net_margin": 300.0,
                                           "roe": 900.0, "debt_ratio": -50.0}))
    for k in RULES.MOAT_KEYS:
        assert 0 <= out[k] <= 10, f"{k}={out[k]} 越界"


def test_护城河强弱样本区分度足够():
    weak = RULES.moat_scores(RULES.norm_fin({"gross_margin": 15.0, "net_margin": 5.0,
                                            "roe": 6.0, "debt_ratio": 70.0,
                                            "asset_turnover": 0.6, "receivable_days": 90.0}),
                             {"industry": "通用制造", "market_cap_yi": 100.0}, {})
    strong = RULES.moat_scores(RULES.norm_fin({"gross_margin": 91.0, "net_margin": 52.0,
                                              "roe": 31.0, "debt_ratio": 18.0,
                                              "asset_turnover": 1.2, "receivable_days": 3.0}),
                               {"industry": "白酒Ⅱ", "market_cap_yi": 21000.0},
                               {"share_rev": 37.5})
    gap = strong["avg"] - weak["avg"]
    assert gap >= 5.0, f"区分度仅 {gap:.1f} 分（修复前 1.0 分）→ 评分无信息量"
    assert strong["avg"] >= 7.0, "「护城河较深」这一档在规则路径必须可达"


def test_moat_avg不得把0当作缺失():
    """`float(m.get(k) or 5)` 会把 0 分（无护城河）误判成中等。"""
    assert RULES.moat_avg({k: 0 for k in RULES.MOAT_KEYS}) == 0.0
    assert RULES.moat_avg({}) == 5.0, "键缺失时才允许回落 5"


def test_报告层护城河均分同样不得把0当5():
    from company_deep_report import _moat_avg
    assert _moat_avg({k: 0 for k in RULES.MOAT_KEYS}) == 0.0


# ── P1 同业回填 / AI 失败状态 ─────────────────────────
def test_竞品为空时回填同业与估值(stubbed):
    st = _run()["state"]
    assert st["pb"]["rival"] and st["pb"]["rival"] not in ("行业主要竞争对手", "最大对手")
    assert st["val"]["peerPe"] or st["val"]["peerPb"] or st["business"].get("peers_top")


def test_ai失败必须记为warn而非ok(stubbed, monkeypatch):
    import llm_client
    monkeypatch.setattr(llm_client, "chat", lambda *a, **k: {"content": "抱歉，我无法完成。"})
    r = _run(use_ai=True)
    d = next(s for s in r["stages"] if s["id"] == "draft")
    assert d["status"] == "warn", "AI 失败被记成成功（违反「不把已有失败写成通过」）"
    assert d.get("error_code") == "AI_DRAFT_FALLBACK"
    deg = r["meta"].get("degraded_stages") or []
    assert any(x["id"] == "draft" for x in deg), "降级阶段未在 meta 中登记"


def test_阶段超时被标记为warn(stubbed, monkeypatch):
    monkeypatch.setattr(PIPE, "STAGE_TIMEOUT_S", 0.001)
    monkeypatch.setattr(PIPE, "stage_news", lambda c, ctx: __import__("time").sleep(0.2) or {})
    r = _run(use_ai=False)
    news = next((s for s in r["stages"] if s["id"] == "news"), None)
    assert news and news["status"] == "warn" and news.get("error_code") == "STAGE_TIMEOUT"
    assert r["ok"] is True, "单个可降级阶段超时不应中断整条管线"


# ── P2 LLM JSON 容错 ──────────────────────────────────
@pytest.mark.parametrize("text,expect", [
    ('{"a": 1}', {"a": 1}),
    ('```json\n{"a": 1}\n```', {"a": 1}),
    ('{"a": 1, "b": [1, 2,],}', {"a": 1, "b": [1, 2]}),
    ('结果如下：\n{"a": {"b": 2}}\n以上。', {"a": {"b": 2}}),
])
def test_json容错解析(text, expect):
    assert LLM.parse_json_block(text) == expect


def test_json解析失败返回None():
    assert LLM.parse_json_block("完全不是 JSON") is None
    assert LLM.parse_json_block("") is None


def test_ai重试一次后仍失败才降级(stubbed, monkeypatch):
    import llm_client
    calls = {"n": 0}

    def _bad(*a, **k):
        calls["n"] += 1
        return {"content": "不是 JSON"}
    monkeypatch.setattr(llm_client, "chat", _bad)
    _run(use_ai=True)
    assert calls["n"] == 2, "应重试一次（首次解析失败 → 追加提示再试）"


def test_ai首次成功则不重试(stubbed, monkeypatch):
    import llm_client
    calls = {"n": 0}

    def _ok(*a, **k):
        calls["n"] += 1
        return {"content": '{"company": {"oneliner": "测试"}}'}
    monkeypatch.setattr(llm_client, "chat", _ok)
    _run(use_ai=True)
    assert calls["n"] == 1


# ── 结构完整性 ────────────────────────────────────────
def test_八个阶段全部产出条目(stubbed):
    r = _run(use_ai=False)
    ids = [s["id"] for s in r["stages"]]
    assert ids == [sid for sid, _ in PIPE.STAGE_NAMES], "阶段顺序/数量与 STAGE_NAMES 不一致"


def test_meta登记阶段统计(stubbed):
    meta = _run(use_ai=False)["meta"]
    assert meta["total_stages"] == len(PIPE.STAGE_NAMES)
    assert meta["ok_stages"] >= 1
    assert isinstance(meta.get("degraded_stages"), list)


def test_离线可用规则不外呼():
    """规则模块必须是叶子：不 import 任何项目模块（避免 R4 循环 import）。"""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "company_deep_rules.py"), encoding="utf-8").read()
    for bad in ("import company_analysis", "import ai_agent", "from company_analysis",
                "from ai_agent", "import company_deep_pipeline"):
        assert bad not in src, f"company_deep_rules 不应出现 `{bad}`（会触 R4）"
