#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
data.tool  — Web 
Flask + 模块化路由架构
"""

import os, sys, io
from pathlib import Path

# 加载 .env 文件
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# UTF-8 输出 — 必须在所有 import 之前，避免 I/O 冲突
# pytest 环境跳过：替换 sys.stdout 会顶掉 pytest 的捕获流，
# 导致本进程内后续所有测试 "I/O operation on closed file"（与 core_config 同一守卫）
if ('pytest' not in sys.modules
        and hasattr(sys.stdout, 'buffer')
        and not str(getattr(sys.stdout, 'encoding', '')).lower().startswith('utf')):
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except (AttributeError, ValueError, OSError):
        pass

# ---- 路由模块延迟导入 ----
# core_config 包含所有共享状态（DataFrame缓存、日志、工具函数等）
from core_config import (
    logger, HOST, PORT, DEBUG, OUT_DIR, df_cache, load_dataframe, handle_errors,
    memory_monitor, ASSET_VER,
)

# ---- 运行时补丁（必须早于任何业务模块的首次实际调用）----
# 修复 py_mini_racer（akshare 依赖的 V8 引擎）多线程并发创建实例导致**进程 FATAL 崩溃**
# 的缺陷——三阶段并行管线会让它从"理论风险"变成现实故障。详见 runtime_patches 模块说明。
try:
    from runtime_patches import apply_all as _apply_runtime_patches
    _apply_runtime_patches()
except Exception:            # 补丁失败不得阻断启动
    pass

# ---- Flask 应用初始化 ----
from flask import Flask, request, jsonify
from flask_compress import Compress
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

app = Flask(__name__)

# 全局 JSON 编码器：numpy 标量（int/float/bool/数组）统一转 Python 原生类型，
# 根治 "numpy.bool_/float64 is not JSON serializable" 一类序列化崩溃（不再靠各模块局部 bool() 打补丁）
try:
    import numpy as _np
    from flask.json.provider import DefaultJSONProvider

    class _NumpyJSONProvider(DefaultJSONProvider):
        def default(self, o):
            if isinstance(o, _np.integer):
                return int(o)
            if isinstance(o, _np.floating):
                return float(o)
            if isinstance(o, _np.bool_):
                return bool(o)
            if isinstance(o, _np.ndarray):
                return o.tolist()
            return super().default(o)

    app.json = _NumpyJSONProvider(app)
except Exception:
    pass  # numpy 缺失时保持 Flask 默认行为


def _load_or_create_secret_key() -> str:
    """获取 SECRET_KEY：环境变量 > 持久化随机密钥 > 随机生成

    避免硬编码弱默认值：若环境变量未设置，则在 data/secret.key 中生成并
    持久化一个随机密钥，保证重启后会话/Token 仍可验证。
    """
    env_key = os.getenv('SECRET_KEY')
    if env_key:
        return env_key
    # DATATOOL_DATA_DIR 优先（npx 部署时指向 ~/.datatool-web/data，避免写进 npm 缓存）
    data_dir = os.getenv('DATATOOL_DATA_DIR') or str(Path(__file__).parent / 'data')
    key_path = Path(data_dir) / 'secret.key'
    try:
        key_path.parent.mkdir(parents=True, exist_ok=True)
        if key_path.exists():
            key = key_path.read_text(encoding='utf-8').strip()
            if key:
                return key
        import secrets as _secrets
        key = _secrets.token_hex(32)
        key_path.write_text(key, encoding='utf-8')
        logger.warning(
            f'未设置 SECRET_KEY 环境变量，已生成随机密钥并保存到 {key_path}。'
            '生产部署请通过环境变量 SECRET_KEY 显式设置。'
        )
        return key
    except Exception as e:  # 持久化失败时退化为会话级随机密钥
        import secrets as _secrets
        logger.warning(f'无法持久化 SECRET_KEY（{e}），使用会话级随机密钥')
        return _secrets.token_hex(32)


app.config['SECRET_KEY'] = _load_or_create_secret_key()


# ---- 前端资源版本号注入（替代手写 ?v=） ----
@app.context_processor
def inject_asset_ver():
    """模板统一注入 asset_ver，前端 js/css 版本号自动跟随静态资源 mtime"""
    return {"asset_ver": ASSET_VER}

# 禁用模板缓存（开发模式）
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True
app.jinja_env.cache = None

# gzip 压缩配��
app.config['COMPRESS_MIMETYPES'] = [
    'text/html', 'text/css', 'text/xml', 'application/json',
    'application/javascript', 'text/plain'
]
app.config['COMPRESS_LEVEL'] = 6
app.config['COMPRESS_MIN_SIZE'] = 500
app.config['COMPRESS_ALGORITHM'] = ['gzip', 'deflate']
app.config['COMPRESS_ALGORITHM_STREAMING'] = ['gzip']
Compress(app)

# 请求限流
# 2026-09-18：默认 200/分钟 + 2000/小时 被前端轮询打满（X-Ratelimit-Remaining: 0，
# 约 3 分钟后重置，表现为接口集体 429"请求过于频繁"）。提高默认配额并支持环境变量覆盖：
#   RATELIMIT_PER_MINUTE（默认 600）、RATELIMIT_PER_HOUR（默认 6000）。
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=[
        f"{os.environ.get('RATELIMIT_PER_MINUTE', '600')} per minute",
        f"{os.environ.get('RATELIMIT_PER_HOUR', '6000')} per hour",
    ],
    storage_uri="memory://",
    strategy="fixed-window",
    headers_enabled=True,
)

# ---- 数据库 ----
from database import init_db
init_db(app)

# ---- JWT 认证 ----
from auth import init_auth
init_auth(app)


# ---- 全局错误处理器（统一 {ok, error, code} 格式，前端 apiFetch 自动展示） ----
_ERROR_CODES = {
    400: "BAD_REQUEST", 401: "UNAUTHORIZED", 403: "FORBIDDEN",
    404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED", 429: "RATE_LIMITED",
    500: "INTERNAL_ERROR", 502: "BAD_GATEWAY", 503: "SERVICE_UNAVAILABLE",
}


def _err(code, msg):
    return jsonify({"ok": False, "error": msg, "code": _ERROR_CODES.get(code, "ERROR")}), code


@app.errorhandler(429)
def ratelimit_error(e):
    return _err(429, "请求过于频繁，请稍后再试")

@app.errorhandler(404)
def not_found(e):
    return _err(404, "资源不存在")

@app.errorhandler(405)
def method_not_allowed(e):
    return _err(405, "请求方法不允许")

@app.errorhandler(500)
def internal_error(e):
    logger.error(f"Internal Server Error: {str(e)}", exc_info=True)
    return _err(500, "服务器内部错误")

@app.errorhandler(Exception)
def unhandled_exception(e):
    if hasattr(e, 'code') and 100 <= e.code < 600:
        code = int(e.code)
        msg = getattr(e, 'description', None) or str(e)
        return _err(code, msg if isinstance(msg, str) else "请求错误")

    # 客户端入参错误 → 400（而非 500）：
    # 1) JSON 请求体验证失败（缺参数）会抛 KeyError，提示缺少哪个字段
    # 2) file/路径类参数为 None 时，Path 运算会抛 TypeError
    if isinstance(e, KeyError) and request.is_json:
        key = e.args[0] if e.args else "unknown"
        if isinstance(key, str):
            logger.warning(f"Missing required parameter: {key} ({request.method} {request.path})")
            return _err(400, f"缺少必需参数：{key}")
    if isinstance(e, TypeError) and "NoneType" in str(e):
        logger.warning(f"Missing path/file parameter ({request.method} {request.path}): {e}")
        return _err(400, "缺少文件路径或必需参数")

    logger.error(f"Unhandled Exception: {str(e)}", exc_info=True)
    return _err(500, f"服务器内部错误（{type(e).__name__}）")

# ---- 请求钩子 ----
@app.after_request
def log_request(response):
    logger.info(f"{request.method} {request.path} - {response.status_code}")
    return response

@app.before_request
def stability_check():
    if not memory_monitor.is_safe():
        memory_monitor.force_gc()
    if memory_monitor.is_warning():
        status = memory_monitor.get_status()
        app.logger.warning(f"Memory warning: {status['rss_mb']}MB")

@app.after_request
def stability_headers(response):
    response.headers["X-Memory-MB"] = str(round(memory_monitor.get_memory_mb(), 1))
    # Service Worker 必须覆盖整个站点：sw.js 放在 /static/ 下，默认作用域会退化为
    # /static/（实测 getRegistration('/') 为 null、controller 为 null）→ 它接管不了
    # 任何页面，Chromium 因"SW 未覆盖 start_url"判定不可安装，安装成应用无从谈起。
    # 该响应头把作用域放宽到 /；同时 SW 脚本本身绝不能长缓存，否则更新永远不生效。
    if request.path == '/static/sw.js':
        response.headers['Service-Worker-Allowed'] = '/'
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers.pop('Expires', None)
        return response
    # 静态资源缓存优化
    if request.path.startswith('/static/'):
        response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
        response.headers['Expires'] = 'Thu, 31 Dec 2099 23:59:59 GMT'
    elif request.path == '/' or request.path == '/index':
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return response

# ---- 注册所有路由模块 ----
from routes import register_all
register_all(app, limiter)

# ---- 启动定时任务调度器 ----
# 服务拆分：EMBED_SCHEDULER=1（默认）内嵌调度；=0 时由独立进程 run_worker.py 接管
if os.environ.get('EMBED_SCHEDULER', '1') == '1':
    try:
        from scheduler_manager import init_scheduler
        init_scheduler(app)
    except Exception as e:
        logger.warning(f"定时任务调度器启动失败: {e}")
else:
    logger.info("EMBED_SCHEDULER=0：调度器由独立 worker 进程（run_worker.py）运行")

logger.info(f"App initialized with {len(list(app.url_map.iter_rules()))} routes")
try:
    from trace_store import startup_self_check as _trace_self_check
    _trace_self_check(logger)
except Exception as _e:
    logger.debug("trace self-check skipped: %s", _e)

# ---- 启动时预加载仪表盘数据（惰性：仅首次请求时预热）----
_LazyWarmupDone = False

@app.before_request
def _lazy_warmup():
    """首次请求时预热仪表盘缓存，不阻塞启动"""
    global _LazyWarmupDone
    if _LazyWarmupDone:
        return
    _LazyWarmupDone = True
    import threading
    def _warmup():
        import time
        time.sleep(1)
        with app.test_client() as c:
            for ep in ('/api/dashboard/index-quote', '/api/dashboard/fund-flow',
                       '/api/dashboard/exchange-rate', '/api/dashboard/market-movers',
                       '/api/dashboard/sentiment', '/api/dashboard/tasks'):
                try:
                    r = c.get(ep)
                    if r.status_code == 200:
                        logger.info(f"✓ 预热成功: {ep}")
                except Exception:
                    pass
    t = threading.Thread(target=_warmup, daemon=True)
    t.start()

# ---- 启动入口 ----
if __name__ == "__main__":
    import time as _t
    _t0 = _t.time()
    # （app.run 前执行，打印冷启动总耗时帮助诊断）
    print(f"\n  data.tool  Web ")
    print(f"    http://{HOST}:{PORT}")
    print(f"    (Task Queue: async tasks enabled)\n")
    print(f"    [Tip: Use 'start.bat' for production server with waitress]")
    try:
        app.run(host=HOST, port=PORT, debug=DEBUG, threaded=True)
    finally:
        logger.info(f"冷启动总耗时 {_t.time() - _t0:.1f}s（主要来自依赖 import，如 sklearn/pandas/matplotlib）")
