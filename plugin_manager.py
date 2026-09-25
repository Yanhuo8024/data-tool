#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
plugin_manager.py — 插件系统（用户上传 Python 脚本扩展图表类型 / 分析模型）

插件规范（plugins/*.py）：
    PLUGIN_META = {"name": "插件名", "type": "chart" | "model", "description": "...", "version": "0.1"}
    - chart 插件：实现 def render(df: pd.DataFrame, params: dict) -> dict
        返回 {"chart": "data:image/png;base64,..."} 或 {"html": "..."} 或 {"table": [...]}
    - model 插件：实现 def run(df: pd.DataFrame, params: dict) -> dict（结果需 JSON 安全）

安全边界（2026-08-25 沙箱化）：
    - 上传时 + 加载时双重 AST 静态审查（plugin_sandbox.audit_plugin_source）：
      import 白名单（数据分析常用库）+ 危险函数黑名单（eval/exec/open/__import__ 等）+ dunder 属性逃逸禁止
    - 运行时超时（RUN_TIMEOUT 秒，daemon 线程，超时返回错误）与输出大小限制（MAX_RESULT_SIZE）
    - 残余风险：AST 审查为静态防线，无法隔离纯字节码级恶意；插件仅限本机管理员上传
"""
import importlib.util
import json
import threading
from pathlib import Path

from plugin_sandbox import SandboxViolation, audit_plugin_source

PLUGIN_DIR = Path(__file__).parent / "plugins"
PLUGIN_DIR.mkdir(exist_ok=True)

ALLOWED_TYPES = ('chart', 'model')
MAX_PLUGIN_SIZE = 200 * 1024  # 200KB
RUN_TIMEOUT = 30              # 插件执行超时（秒）
MAX_RESULT_SIZE = 5 * 1024 * 1024  # 结果 JSON 大小上限（5MB）

_lock = threading.Lock()
_cache = {}  # name -> module


class PluginError(Exception):
    pass


def _scan_files():
    return sorted(p for p in PLUGIN_DIR.glob('*.py') if p.name != '__init__.py')


def _load(name: str):
    """加载并缓存插件模块（AST 沙箱审查 + PLUGIN_META 校验）"""
    with _lock:
        if name in _cache:
            return _cache[name]
        p = PLUGIN_DIR / f"{name}.py"
        if not p.exists():
            raise PluginError(f"插件不存在: {name}")
        # ── 沙箱第一道防线：加载时 AST 审查（防文件被外部篡改）──
        try:
            audit_plugin_source(p.read_text(encoding='utf-8'))
        except SandboxViolation as e:
            raise PluginError(f"插件未通过沙箱审查: {e}")
        spec = importlib.util.spec_from_file_location(f"datatool_plugin_{name}", str(p))
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception as e:
            raise PluginError(f"插件加载失败: {e}")
        meta = getattr(mod, 'PLUGIN_META', None)
        if not meta or not isinstance(meta, dict) or meta.get('type') not in ALLOWED_TYPES:
            raise PluginError(f"插件缺少合法 PLUGIN_META（type 须为 {ALLOWED_TYPES}）")
        _cache[name] = mod
        return mod


def list_plugins() -> list:
    """列出全部插件（含加载错误信息）"""
    out = []
    for p in _scan_files():
        name = p.stem
        try:
            mod = _load(name)
            meta = dict(mod.PLUGIN_META)
            meta['name'] = name
            meta['file'] = p.name
            meta['size_kb'] = round(p.stat().st_size / 1024, 1)
            out.append(meta)
        except Exception as e:
            out.append({'name': name, 'file': p.name, 'error': str(e)})
    return out


def install_plugin(content: str, filename: str) -> str:
    """安装插件：AST 沙箱审查 → 写入 plugins/ → 试加载校验；失败回滚删除"""
    if not filename.endswith('.py'):
        raise PluginError('仅支持 .py 插件文件')
    if len(content.encode('utf-8')) > MAX_PLUGIN_SIZE:
        raise PluginError('插件文件过大（>200KB）')
    name = Path(filename).stem
    safe = ''.join(c for c in name if c.isalnum() or c == '_')
    if not safe:
        raise PluginError('插件名非法')
    # ── 沙箱第一道防线：上传时 AST 审查 ──
    try:
        audit_plugin_source(content)
    except SandboxViolation as e:
        raise PluginError(f"插件未通过沙箱审查: {e}")
    target = PLUGIN_DIR / f"{safe}.py"
    target.write_text(content, encoding='utf-8')
    try:
        _load(safe)
    except Exception:
        try:
            target.unlink()
        except OSError:
            pass
        raise
    with _lock:
        _cache.pop(safe, None)
    return safe


def delete_plugin(name: str):
    with _lock:
        _cache.pop(name, None)
        p = PLUGIN_DIR / f"{name}.py"
        if p.exists():
            p.unlink()
        else:
            raise PluginError(f"插件不存在: {name}")


def run_plugin(name: str, df, params: dict) -> dict:
    """执行插件：chart 走 render，model 走 run
    运行时保护：daemon 线程 + 超时（RUN_TIMEOUT 秒）+ 结果 JSON 大小上限"""
    mod = _load(name)
    meta = mod.PLUGIN_META
    t = meta.get('type')
    fn = getattr(mod, 'render' if t == 'chart' else 'run', None)
    if fn is None:
        raise PluginError(f"插件缺少 {'render' if t == 'chart' else 'run'} 函数")

    box = {}

    def _worker():
        try:
            box['result'] = fn(df, params or {})
        except Exception as e:
            box['error'] = e

    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    th.join(RUN_TIMEOUT)
    if th.is_alive():
        raise PluginError(f"插件执行超时（>{RUN_TIMEOUT}s），已终止本次调用")
    if 'error' in box:
        raise PluginError(f"插件执行失败: {box['error']}")

    result = box.get('result')
    # 结果大小限制（JSON 序列化前粗略估算）
    try:
        raw = json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:
        raise PluginError(f"插件结果无法序列化为 JSON: {e}")
    if len(raw.encode('utf-8')) > MAX_RESULT_SIZE:
        raise PluginError(f"插件结果过大（>{MAX_RESULT_SIZE // 1024 // 1024}MB），已拒绝")
    return {'type': t, 'name': name, 'result': result}
