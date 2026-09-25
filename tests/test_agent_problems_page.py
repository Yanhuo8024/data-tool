# -*- coding: utf-8 -*-
"""Agent 问题页：problems_dashboard / API / 静态页。"""
import json
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _write_trace(run_id: str, events: list, base: Path):
    d = base / 'traces'
    d.mkdir(parents=True, exist_ok=True)
    p = d / f'{run_id}.jsonl'
    with p.open('w', encoding='utf-8') as f:
        for i, (typ, data) in enumerate(events, 1):
            f.write(json.dumps({'ts': f'2026-09-18T12:00:{i:02d}', 'run_id': run_id,
                                'seq': i, 'type': typ, 'data': data}, ensure_ascii=False) + '\n')


def test_problems_dashboard_shape(monkeypatch, tmp_path):
    import trace_store
    import agent_flow
    monkeypatch.setattr(trace_store, 'TRACE_DIR', str(tmp_path))

    # 快运行：无问题
    _write_trace('run-ok', [
        ('run_start', {'user_message': '描述统计', 'provider': 'sensenova', 'model': 'glm-5.2'}),
        ('agent_config', {'max_iterations': 3, 'file': 'a.csv'}),
        ('llm_call', {'iteration': 1, 'elapsed_ms': 1200}),
        ('tool_result', {'tool': 'describe', 'status': 'ok', 'elapsed_ms': 80, 'iteration': 1}),
        ('run_end', {'outcome': 'ok', 'ok': True,
                     'summary': {'elapsed_ms': 2500, 'tool_calls': 1, 'iterations': 1,
                                 'output_len': 200}}),
    ], tmp_path)

    # 慢 + 有问题：有文件零工具 + 慢 LLM
    _write_trace('run-bad', [
        ('run_start', {'user_message': '分析当前文件', 'provider': 'zhipu', 'model': 'm'}),
        ('agent_config', {'max_iterations': 3, 'file': 'b.csv'}),
        ('llm_call', {'iteration': 1, 'elapsed_ms': 25000}),
        ('llm_call', {'iteration': 2, 'elapsed_ms': 8000}),
        ('run_end', {'outcome': 'ok', 'ok': True,
                     'summary': {'elapsed_ms': 40000, 'tool_calls': 0, 'iterations': 2,
                                 'output_len': 50}}),
    ], tmp_path)

    # 管线阶段失败
    _write_trace('run-pipe', [
        ('run_start', {'user_message': '做一份研究报告', 'provider': 'sensenova', 'model': 'glm-5.2'}),
        ('pipeline.stage_start', {'stage': '量化建模师', 'index': 2}),
        ('pipeline.stage_error', {'stage': '量化建模师', 'index': 2, 'ok': False,
                                  'provider': 'zhipu', 'model': 'glm-4.7-flash',
                                  'elapsed_s': 0.0, 'error': 'API error 429: rate limit exceeded'}),
        ('run_end', {'outcome': 'ok', 'ok': True,
                     'summary': {'elapsed_ms': 6000, 'tool_calls': 0, 'iterations': 1,
                                 'output_len': 10}}),
    ], tmp_path)

    dash = agent_flow.problems_dashboard(50)
    assert dash['runs'] >= 3
    t = dash['time']
    assert t['samples'] >= 1
    assert t['avg_ms'] is not None
    assert t['p50_ms'] is not None
    assert isinstance(t['by_phase'], list)
    assert isinstance(t['by_day'], list)

    assert isinstance(dash['problems'], list)
    codes = {p['code'] for p in dash['problems']}
    assert 'NO_TOOL_CALLS' in codes
    assert 'PIPELINE_STAGE_FAILED' in codes
    assert 'SLOW_LLM_CALL' in codes or 'SLOW_STAGE' in codes

    for p in dash['problems']:
        assert 'count' in p and 'rate' in p
        assert 'avg_elapsed_ms' in p
        assert 'first_at' in p and 'last_at' in p
        assert 'samples' in p

    dirs = dash['directions']
    assert dirs, '应生成优化方向'
    assert all('title' in d and 'actions' in d for d in dirs)
    # 方向应按优先级降序
    prios = [d['priority'] for d in dirs]
    assert prios == sorted(prios, reverse=True)

    assert dash['healthy_rate'] >= 0
    assert dash['rule_catalog']


def test_problems_dashboard_empty_safe(tmp_path, monkeypatch):
    import agent_flow
    import trace_store
    # 自带空轨迹目录：conftest 只在 session 级设一次 TRACE_DIR，同一批里更早的测试
    # （如 test_agent_pipeline 跑真实管线）会往该目录写 jsonl，导致本断言 runs==0 被污染
    # （2026-09-18 实测：与 test_agent_pipeline 同批跑时必挂，单独跑/分批跑却通过）。
    monkeypatch.setattr(trace_store, 'TRACE_DIR', str(tmp_path), raising=False)
    # 无轨迹时不得抛错
    d = agent_flow.problems_dashboard(1)
    assert d['runs'] == 0
    assert d['problems'] == [] or isinstance(d['problems'], list)
    assert d['directions'] == [] or isinstance(d['directions'], list)
    assert d['time']['avg_ms'] is None


def test_problems_api_and_page():
    os.environ.setdefault('TESTING', '1')
    os.environ.setdefault('RATELIMIT_ENABLED', '0')
    os.environ.setdefault('SECRET_KEY', 'test-secret-key')
    os.environ.setdefault('ADMIN_PASSWORD', 'admin123')
    os.environ.setdefault('DISABLE_SCHEDULER', '1')
    os.environ['TRACE_DIR'] = tempfile.mkdtemp(prefix='dt-test-traces-')

    from app import app
    client = app.test_client()
    r = client.get('/api/assistant/flows/problems?limit=20')
    assert r.status_code == 200
    j = r.get_json()
    assert j.get('ok') is True
    assert 'data' in j
    assert 'time' in j['data'] and 'problems' in j['data'] and 'directions' in j['data']

    page = client.get('/static/agent-problems.html')
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert 'Agent 问题页' in body
    assert 'flows/problems' in body
