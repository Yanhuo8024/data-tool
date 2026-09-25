# -*- coding: utf-8 -*-
"""Agent 工作流可视化器适配层（flowviz_adapter）回归测试。

覆盖三件事：
  1. 本地直读取数（不再走 HTTP）：list_runs / fetch_trace 直读 trace_store；
  2. 画布契约：build_spec（拓扑）与 build_state（状态快照）的形状；
  3. **语义对齐**（2026-09-18）：fact_audit.ratio 是未核验率（方向别搞反）、
     终稿 force_final（正常收尾）与 truncated（真截断）必须分开报。

测试全部离线：build_* 直接吃内存里的 trace 字典，不依赖真实轨迹文件。
"""
import json

import pytest

import flowviz_adapter as fa


def _ev(etype, data, seq, ts):
    return {"seq": seq, "ts": ts, "type": etype, "data": data}


def _trace(events, run_id="t-run"):
    return {"ok": True, "run": run_id, "count": len(events),
            "file": "/tmp/%s.jsonl" % run_id, "events": events}


def _pipeline_events(ratio=0.08, unverified=2, finalize_extra=None, fin_ok=True):
    """构造一条「多阶段流水线」事件流（含事实溯源与终稿）。"""
    fin = {"ok": fin_ok, "content_len": 4357, "duration_ms": 35000}
    fin.update(finalize_extra or {})
    return [
        _ev("run_start", {"user_message": "全面分析电器行业", "provider": "sensenova",
                          "model": "glm-5.2", "skill": "agent_pipeline"},
            1, "2026-09-18T20:00:00.000"),
        _ev("pipeline.stage_start", {"index": 1, "stage": "市场分析师",
                                     "model": "glm-5.2", "provider": "sensenova", "iters": 0},
            2, "2026-09-18T20:00:00.010"),
        _ev("pipeline.stage_start", {"index": 2, "stage": "量化建模师",
                                     "model": "sensenova-6.8-flash-lite",
                                     "provider": "sensenova", "iters": 0},
            3, "2026-09-18T20:00:00.015"),
        _ev("pipeline.stage_done", {"index": 2, "stage": "量化建模师", "ok": True,
                                    "elapsed_s": 22.9, "chars": 3745,
                                    "model": "sensenova-6.8-flash-lite",
                                    "provider": "sensenova"},
            4, "2026-09-18T20:00:23.900"),
        _ev("pipeline.stage_done", {"index": 1, "stage": "市场分析师", "ok": True,
                                    "elapsed_s": 120.2, "chars": 4841,
                                    "model": "glm-5.2", "provider": "sensenova"},
            5, "2026-09-18T20:02:00.200"),
        _ev("fact_audit", {"total": 27, "unverified": unverified, "ratio": ratio,
                           "policy": "data_must_be_real"}, 6, "2026-09-18T20:02:10.000"),
        _ev("finalize.done", fin, 7, "2026-09-18T20:02:45.000"),
        _ev("run_end", {"ok": fin_ok, "outcome": "ok",
                        "summary": {"elapsed_ms": 211000, "tokens": 48893,
                                    "errors": 0, "failed_stages": []}},
            8, "2026-09-18T20:02:45.100"),
    ]


@pytest.fixture(autouse=True)
def _clear_adapter_caches():
    """适配器有 1s/3s 的进程内缓存；测试之间必须清干净，避免串味。"""
    fa._RUNS_CACHE.drop("local")
    yield
    fa._RUNS_CACHE.drop("local")


# ----------------------------------------------------------------------
# 1. 本地直读取数
# ----------------------------------------------------------------------
def test_list_runs_reads_local_trace_store(tmp_path, monkeypatch):
    import trace_store
    monkeypatch.setattr(trace_store, "TRACE_DIR", str(tmp_path))
    d = tmp_path / "traces"
    d.mkdir()
    (d / "20260918-200000-aa.jsonl").write_text("\n".join([
        json.dumps({"seq": 1, "type": "run_start",
                    "data": {"user_message": "全面分析电器行业",
                             "provider": "sensenova", "model": "glm-5.2"}}),
        json.dumps({"seq": 2, "type": "run_end", "data": {"ok": True}}),
    ]), encoding="utf-8")

    runs = fa.list_runs(None, 10)
    assert len(runs) == 1
    r = runs[0]
    assert r["run_id"] == "20260918-200000-aa"
    assert r["model"] == "glm-5.2"
    assert r["ok"] is True
    assert "全面分析" in r["user_message"]


def test_fetch_trace_reads_events_and_reports_missing(tmp_path, monkeypatch):
    import trace_store
    monkeypatch.setattr(trace_store, "TRACE_DIR", str(tmp_path))
    d = tmp_path / "traces"
    d.mkdir()
    (d / "run-a.jsonl").write_text(
        json.dumps({"seq": 1, "type": "run_start", "data": {}}), encoding="utf-8")

    tr = fa.fetch_trace(None, "run-a")
    assert tr["run"] == "run-a" and tr["count"] == 1

    with pytest.raises(fa.UpstreamMissing):
        fa.fetch_trace(None, "run-does-not-exist")


def test_fetch_json_is_disabled_in_local_mode():
    """取数层已本地化：残留的 HTTP 入口必须显式失败，而不是悄悄再发一次请求。"""
    with pytest.raises(RuntimeError):
        fa.fetch_json()


# ----------------------------------------------------------------------
# 2. 画布契约
# ----------------------------------------------------------------------
def test_build_spec_shape():
    spec = fa.build_spec(_trace(_pipeline_events(), "spec-run"))
    assert spec["run_id"] == "spec-run"
    assert spec["version"] == 1
    assert spec["nodes"] and spec["edges"]
    # stages 必须是「对象数组」，画布读 s.name（字符串数组也能吃，但对象更明确）
    assert spec["stages"]
    assert all(isinstance(s, dict) and s.get("name") for s in spec["stages"])
    ids = {n["id"] for n in spec["nodes"]}
    assert "start" in ids and "end" in ids
    for e in spec["edges"]:
        assert e["from"] in ids and e["to"] in ids
    assert "rev" in spec


def test_build_state_shape_and_topology_push():
    tr = _trace(_pipeline_events(), "state-run")
    st = fa.build_state(tr, cid="cid-1")
    for k in ("run", "phase", "agents", "links", "summary", "logs", "done",
              "timeline", "issues", "totals"):
        assert k in st, "状态快照缺少 %s" % k
    assert st["agents"]
    assert st["phase"] in ("running", "done")
    # 新 cid → 随附 spec，画布可首次建图
    assert "spec" in st
    # 同一 cid 且拓扑未变 → 不再重复下发 spec（省流量）
    st2 = fa.build_state(tr, cid="cid-1")
    assert "spec" not in st2


def test_build_state_keepalive_controls_done():
    tr = _trace(_pipeline_events(), "keepalive-run")
    follow = fa.build_state(tr, cid="k1", keepalive=True)
    pinned = fa.build_state(tr, cid="k2", keepalive=False)
    assert follow["phase"] == "done"
    assert follow["done"] is False, "跟随最新的运行必须保持 done=false，否则画布不再跟新运行"
    assert pinned["done"] is True, "钉住某次运行，跑完就该定格"


# ----------------------------------------------------------------------
# 3. 语义对齐（本轮重点）
# ----------------------------------------------------------------------
def test_fact_audit_ratio_is_unverified_rate_high_ratio_warns():
    """ratio 是**未核验率**：89% 未核验 → 必须报警（旧实现按通过率判，方向反了，不报）。

    注意本适配器的字段约定（与可视化器一致）：
      level = 严重度（error/warn/info），kind = 分类标签，title = 具体文案。
    """
    tr = _trace(_pipeline_events(ratio=0.89, unverified=24), "audit-high")
    issues = fa.build_issues(fa.index_events(tr))
    hit = [i for i in issues if i["at"] == "fact_audit" and i["level"] == "warn"]
    assert hit, "未核验率 89% 却没有任何告警：%r" % issues
    assert "未核验率" in hit[0]["title"]
    # 不得再把它误报成「事实核查不通过」
    assert not [i for i in issues if "事实核查不通过" in (i["kind"] + i["title"])]


def test_fact_audit_low_ratio_does_not_warn():
    """8% 未核验 → 不该报警（旧实现 ratio<0.8 会误报为错误）。"""
    tr = _trace(_pipeline_events(ratio=0.08, unverified=2), "audit-low")
    issues = fa.build_issues(fa.index_events(tr))
    assert not [i for i in issues if i["at"] == "fact_audit" and i["level"] in ("error", "warn")]


def test_finalize_force_final_is_not_truncation():
    """force_final（用满轮数收尾）是正常流程，不得报成「输出截断」。"""
    tr = _trace(_pipeline_events(finalize_extra={"force_final": True, "truncated": False}),
                "fin-force")
    issues = fa.build_issues(fa.index_events(tr))
    assert not [i for i in issues if i["kind"] == "输出截断"]
    info = [i for i in issues if i["kind"] == "达轮数上限收尾"]
    assert info and info[0]["level"] == "info"


def test_finalize_real_truncation_warns():
    tr = _trace(_pipeline_events(finalize_extra={"force_final": False, "truncated": True}),
                "fin-trunc")
    issues = fa.build_issues(fa.index_events(tr))
    assert [i for i in issues if i["kind"] == "输出截断" and i["level"] == "warn"]


def test_legacy_truncated_flag_means_force_final():
    """旧轨迹没有 force_final 键，它的 truncated=true 其实是强制收尾 —— 不得报截断。"""
    tr = _trace(_pipeline_events(finalize_extra={"truncated": True}), "fin-legacy")
    issues = fa.build_issues(fa.index_events(tr))
    assert not [i for i in issues if i["kind"] == "输出截断"]
    assert [i for i in issues if i["kind"] == "达轮数上限收尾"]


def test_timeline_labels_unverified_rate():
    tr = _trace(_pipeline_events(ratio=0.5, unverified=13), "tl-audit")
    tl = fa.build_timeline(fa.index_events(tr))
    audit = [x for x in tl if x["kind"] == "audit"]
    assert audit, "时间线里应有事实核查一步"
    assert "未核验率" in audit[0]["detail"]


# ----------------------------------------------------------------------
# 4. 同源 API 路由
# ----------------------------------------------------------------------
def test_flowviz_routes_registered(app):
    rules = {r.rule for r in app.url_map.iter_rules()}
    for p in ("/api/assistant/flowviz/runs", "/api/assistant/flowviz/spec",
              "/api/assistant/flowviz/state", "/api/assistant/flowviz/raw"):
        assert p in rules, "缺少路由 %s（flowviz 接口必须在核心蓝图里同步注册）" % p


def test_flowviz_runs_endpoint_ok(client):
    resp = client.get("/api/assistant/flowviz/runs")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True and isinstance(data["runs"], list)


def test_flowviz_state_endpoint_shape(client, tmp_path, monkeypatch):
    """端到端：写一条合成轨迹 → 调 /state 接口 → 校验快照。

    轨迹写在**本测试私有的空目录**里（不用 conftest 的 session 级共享 TRACE_DIR），
    否则会与其他断言「轨迹为空」的测试互相污染。
    """
    import trace_store
    monkeypatch.setattr(trace_store, "TRACE_DIR", str(tmp_path))
    d = tmp_path / "traces"
    d.mkdir()
    rid = "pytest-flowviz-state"
    d.joinpath(rid + ".jsonl").write_text(
        "\n".join(json.dumps(e) for e in _pipeline_events()), encoding="utf-8")
    fa._RUNS_CACHE.drop("local")

    resp = client.get("/api/assistant/flowviz/state?run=" + rid)
    assert resp.status_code == 200, resp.data[:400]
    data = resp.get_json()
    assert data["ok"] is True
    assert data["run"] == rid
    assert data["agents"] and "summary" in data and "issues" in data
    # 钉住某次运行 → 跑完即 done（画布定格）
    assert data["done"] is True

    # spec 接口同样可达
    resp2 = client.get("/api/assistant/flowviz/spec?run=" + rid)
    assert resp2.status_code == 200
    spec = resp2.get_json()["spec"]
    assert spec["nodes"] and spec["run_id"] == rid


def test_static_visualizer_page_served(client):
    resp = client.get("/static/agent-flow-viz.html")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8", "replace")
    assert "/api/assistant/flowviz/runs" in body
    assert "/api/assistant/flowviz/state" in body
    # 取数已改为同源 flowviz，不得再引用桥接服务的「运行清单」适配路由
    assert "/api/adapt/datatool/runs" not in body
