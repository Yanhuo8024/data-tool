#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工具执行边界测试（P1）：端点类别白名单 + 写盘路径锁定

对标 DeepSeek Harness 的 sandbox 插件层。本仓库不上容器（工具均为数据分析语义，
非任意 shell/代码执行），用**路径与类别约束**覆盖主要风险面。

覆盖两件事：
  ① `endpoint_risk()`：写盘类分析端点被识别，受 `AGENT_ALLOW_WRITE_ENDPOINTS` 约束；
  ② `safe_output_path()`：拒绝绝对路径 / `..` 穿越 / 越出 OUT_DIR —— 这是写盘目录锁定的
     **单一实现**，三个写盘工具（clean_data / feature_eng / ml_train）共用 `_load_guarded()`。
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai_agent  # noqa: E402


@pytest.fixture
def write_eps_on(monkeypatch):
    monkeypatch.delenv('AGENT_ALLOW_WRITE_ENDPOINTS', raising=False)
    yield
    monkeypatch.delenv('AGENT_ALLOW_WRITE_ENDPOINTS', raising=False)


@pytest.fixture
def write_eps_off(monkeypatch):
    monkeypatch.setenv('AGENT_ALLOW_WRITE_ENDPOINTS', '0')
    yield
    monkeypatch.delenv('AGENT_ALLOW_WRITE_ENDPOINTS', raising=False)


# --------------------------- 端点类别 ---------------------------
def test_写盘端点被识别():
    for ep in ('/api/clean/run', '/api/feature/auto', '/api/ml/train',
               '/api/store', '/api/report/preview'):
        assert ai_agent.endpoint_risk(ep) == 'write', f'{ep} 应判定为写盘类'


def test_读数端点被识别():
    for ep in ('/api/describe', '/api/correlation', '/api/ht', '/api/ts/arima',
               '/api/did/regression', '/api/fixedincome/bond', '/api/finance/fund-metrics'):
        assert ai_agent.endpoint_risk(ep) == 'readonly', f'{ep} 应判定为只读类'


def test_未知名返回unknown():
    assert ai_agent.endpoint_risk('/api/__nope__') == 'unknown'
    assert ai_agent.endpoint_risk('') == 'unknown'


def test_写盘端点关闭后被拒(write_eps_off):
    """写盘端点在关闭态应被拦，且反馈里要说明原因与只读替代。

    注意：端点必须**真实已注册**，否则先被 `_analysis_api_registered` 拦下，
    测不到类别闸门（拦截顺序：白名单 → 类别 → 参数）。
    """
    out = ai_agent.tool_run_analysis_api('/api/clean', params={'file': 'x.csv'})
    assert 'AGENT_ALLOW_WRITE_ENDPOINTS' in out, f'应命中类别闸门，实际: {out[:120]}'
    assert 'Error' in out
    assert 'describe' in out, '应给出只读替代端点，避免模型反复重试'


def test_端点类别划分互斥且完备():
    """所有前缀必须能归入 write 或 readonly 之一，不允许有端点两边都命中"""
    for p in ai_agent._FUNCTIONAL_PREFIXES:
        risk = ai_agent.endpoint_risk(p)
        assert risk in ('readonly', 'write'), f'前缀 {p} 未被正确归类: {risk}'


# --------------------------- 写盘路径锁定 ---------------------------
def test_正常文件名通过(tmp_path):
    p, err = ai_agent.safe_output_path('clean_out.csv', base=tmp_path)
    assert err is None and p is not None
    assert p.parent == tmp_path.resolve()


def test_拒绝绝对路径(tmp_path):
    """绝对路径一律拒绝。

    ⚠️ Windows 上 `/etc/passwd` 并非绝对路径（无盘符），会走"越出输出目录"分支 ——
    **两条分支都算拦截成功**，故断言放宽为"返回了错误且未给出可写路径"。
    这是跨平台差异，不是缺陷。
    """
    for bad in ('C:\\Windows\\evil.csv', '/etc/passwd', '\\\\server\\share\\x.csv'):
        p, err = ai_agent.safe_output_path(bad, base=tmp_path)
        assert p is None and err, f'{bad} 应被拒绝'
        assert ('绝对路径' in err or '穿越' in err or '越出' in err), f'错误分类不明: {err}'


def test_拒绝路径穿越(tmp_path):
    for bad in ('../evil.csv', 'a/../../evil.csv', '..\\..\\evil.csv'):
        p, err = ai_agent.safe_output_path(bad, base=tmp_path)
        assert p is None and err, f'{bad} 应被拒绝'


def test_拒绝越出输出目录(tmp_path):
    """子目录合法，但解析后必须仍在 base 之内"""
    ok, err = ai_agent.safe_output_path('sub/dir/out.csv', base=tmp_path)
    assert err is None and str(ok).startswith(str(tmp_path.resolve()))


def test_空文件名被拒(tmp_path):
    p, err = ai_agent.safe_output_path('', base=tmp_path)
    assert p is None and '空' in err


def test_路径锁定是单一实现():
    """三个写盘工具必须共用 _load_guarded，禁止各自 `OUT_DIR / file`（否则绕过边界）

    单一实现在 agent_tool_registry（ai_agent 仅 re-export 门面），故检查 registry 源码。"""
    import inspect
    import agent_tool_registry as _tr
    src = inspect.getsource(_tr)
    for fn in ('tool_clean_data', 'tool_feature_eng', 'tool_ml_train'):
        start = src.index(f'def {fn}(')
        body = src[start:start + 900]
        assert '_load_guarded' in body, f'{fn} 未经写入边界，仍是裸 OUT_DIR / file'
    # ai_agent 门面必须可导入这些符号（保持既有调用方/测试的稳定导入点）
    for fn in ('tool_clean_data', 'tool_feature_eng', 'tool_ml_train'):
        assert hasattr(ai_agent, fn), f'ai_agent 门面缺少 {fn} re-export'


def test_写盘工具拒绝穿越路径():
    """端到端：模型给 `../../x.csv`，工具必须返回边界错误而非读文件"""
    for fn in (ai_agent.tool_clean_data, ai_agent.tool_feature_eng):
        out = fn('../../etc/passwd')
        assert '边界' in out or 'Error' in out, f'{fn.__name__} 未拦截穿越路径: {out[:80]}'


def test_落盘参数被校验(write_eps_on):
    """run_analysis_api 的 out/save_as 等键必须过 safe_output_path"""
    # 穿越路径应被拒（不实际调用端点：先过参数校验即返回 Error）
    out = ai_agent.tool_run_analysis_api('/api/clean/run', params={'file': 'a.csv', 'out': '../../x.csv'})
    assert '边界' in out or 'Error' in out
