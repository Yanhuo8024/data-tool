#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tool_boundary.py — 工具执行边界（P1，对标 DeepSeek Harness 的 sandbox 插件）
================================================================================
为什么独立成模块：`ai_agent.py` 已达架构门禁 R1 的存量基线（禁止继续增长）。
本模块承载**两个纯函数 + 一张白名单表**，零外部依赖、零副作用、可独立测试。

职责只有两件事：
  ① **端点类别划分**：区分「纯读数」与「会落盘/改库」的分析端点。
     写盘类端点受 `AGENT_ALLOW_WRITE_ENDPOINTS` 约束（默认放行）。
  ② **写盘路径锁定**：`safe_output_path()` 把（可能是模型给出的）文件名解析为
     输出目录内的绝对路径，拒绝绝对路径 / `..` 穿越 / 解析后越界。

为什么不上容器/子进程沙箱：
  本仓库工具均为**数据分析**语义（读表、算指标、出图、训练），不是任意 shell 或代码执行。
  上容器的成本（部署复杂度、便携 Python 适配、与 Flask test_client 调用链的兼容）
  远超其能覆盖的增量风险面；路径与类别约束已覆盖主要威胁（模型构造越界路径、隐式落盘）。

调用点：`ai_agent.registry` 的工具实现（`_load_guarded()` / `tool_run_analysis_api()`），
在 `ai_agent.py` 内**原位置 re-export**，保持外部 `from ai_agent import safe_output_path` 零改动。
"""

from pathlib import Path

# 端点类别白名单。与 `ai_agent._FUNCTIONAL_PREFIXES`（总前缀白名单）配合使用：
# 总白名单管"能不能调"，本表管"调的有没有副作用"。两者都不命中 → unknown（拒绝）。
WRITE_ENDPOINT_PREFIXES = (
    '/api/clean',       # 清洗结果落盘
    '/api/feature/',    # 特征工程产物落盘
    '/api/ml/',         # 训练产出模型文件
    '/api/store',       # 显式落盘
    '/api/report/',     # 生成报告文件
)

# 写盘开关的取值集合（与 egress_policy 同源约定，避免各写各的）
_FALSY = ('0', 'false', 'False', 'no', 'off', '')


def classify_endpoint(endpoint: str, readonly_prefixes) -> str:
    """按给定前缀表判定端点类别：'readonly' / 'write' / 'unknown'。

    `readonly_prefixes` 由调用方传入（即 `_FUNCTIONAL_PREFIXES` 去掉写盘项），
    避免本模块反向 import `ai_agent`（会造成循环 import，触 R4 门禁）。
    """
    ep = (endpoint or '').rstrip('/')
    if not ep:
        return 'unknown'
    for p in WRITE_ENDPOINT_PREFIXES:
        if ep == p.rstrip('/') or ep.startswith(p):
            return 'write'
    for p in (readonly_prefixes or ()):
        if ep == p.rstrip('/') or ep.startswith(p):
            return 'readonly'
    return 'unknown'


def write_endpoints_allowed(env_value=None) -> bool:
    """写盘类端点是否放行（默认放行 —— 本地自用工具，默认拦截会打断工作流）。"""
    import os
    raw = os.environ.get('AGENT_ALLOW_WRITE_ENDPOINTS', '1') if env_value is None else env_value
    return str(raw) not in _FALSY


def safe_output_path(name, base=None):
    """把文件名解析为输出目录内的绝对路径。返回 `(Path, None)` 或 `(None, 错误说明)`。

    拒绝三类：
      · 绝对路径（模型给 `C:\\x`、`/etc/x`、UNC `\\\\srv\\share` 即视为越界意图）
      · 含 `..` 的穿越片段
      · 解析后不在 `base` 之内（软链接解析后仍须在界内）

    这是「写盘目录锁定」的**单一实现** —— 新增写盘类工具必须经此函数取路径，
    不得自行拼 `OUT_DIR / name`（`Path('/a') / '/etc/x'` 会直接返回 `/etc/x`）。

    ⚠️ 跨平台注意：Windows 上 `/etc/passwd` 无盘符、**不算绝对路径**，
    会落到"越出输出目录"分支 —— 两条分支都属**拦截成功**，测试勿只断言前一条。
    """
    if base is None:
        try:
            from core_config import OUT_DIR
            base = OUT_DIR
        except Exception:
            base = Path(__file__).resolve().parent / 'output'
    root = Path(base).resolve()
    raw = str(name or '').strip()
    if not raw:
        return None, '文件名为空'
    cand = Path(raw)
    if cand.is_absolute() or raw.startswith(('\\\\', '//')):
        return None, f'拒绝绝对路径（只允许在输出目录内）: {raw}'
    if '..' in cand.parts:
        return None, f'拒绝路径穿越: {raw}'
    try:
        target = (root / cand).resolve()
    except (OSError, ValueError) as e:
        return None, f'路径解析失败: {e}'
    if root != target and root not in target.parents:
        return None, f'拒绝越出输出目录: {raw}'
    return target, None


def guarded_load(file, loader):
    """带边界校验地加载文件。`loader` 由调用方注入（通常是 `core_config.load_dataframe`）。

    返回 `(df, None)` 或 `(None, 错误说明)`。抽成高阶函数是为了让本模块
    **不依赖 core_config**（避免 import 期加载副作用与循环 import）。
    """
    p, err = safe_output_path(file)
    if err:
        return None, f'Error: 文件参数被边界拒绝 —— {err}'
    try:
        return loader(str(p)), None
    except Exception as e:
        return None, f'Error: 读取失败 —— {e}'
