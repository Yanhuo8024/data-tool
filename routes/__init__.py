"""Routes package — 分阶段懒注册（U盘冷启动优化）

设计原则：
- **核心路由（启动必需）**：同步立即注册，让首页 + 关键 API (auth/dashboard/core/ai_assistant) 能立即响应
- **拓展路由（不阻塞启动）**：后台线程注册 + 首次访问 fallback 强制同步加载
  - 后台线程在 waitress 启动后慢慢加载，不会拉长启动阻塞时间
  - 如果用户访问某 lazy 路由时还没注册完，404 fallback 会同步加载剩余所有路由后重试
"""
import threading

# 启动期同步注册（首页 + 关键 API + Agent 链路依赖）
# 2026-09-18：agent/knowledge/llm/scheduler/file 从懒注册挪到同步——
# 懒注册竞态下首批请求可能 404，而这些模块是 AI 助手闭环的硬依赖。
_CORE_REGISTRARS = [
    ('core_routes', 'register_routes', None),
    ('auth_routes', 'auth_bp', '/api'),
    ('dashboard_routes', 'dashboard_bp', None),
    ('ai_assistant_routes', 'ai_assistant_bp', None),
    # 出境开关接口：与 ai_assistant 同批（设置面板一打开就拉，不能等懒注册）
    ('egress_routes', 'egress_bp', None),
    # 业务能力（智能运营 / 风控初审 / 能力自检）：外部系统可直接调用，不能等懒注册
    ('business_routes', 'business_bp', None),
    ('search_routes', 'search_bp', None),
    ('skill_routes', 'skill_bp', None),
    # Agent 硬依赖：任务 API / 知识库 / LLM 配置 / 定时任务 / 文件删除时失效 tool_cache
    ('agent_routes', 'agent_bp', None),
    ('knowledge_routes', 'knowledge_bp', '/api/knowledge'),
    ('llm_routes', 'llm_bp', None),
    ('scheduler_routes', 'scheduler_bp', '/api'),
    ('file_routes', 'register_routes', None),
]

# 后台线程 + 按需 fallback 加载
_LAZY_REGISTRARS = [
    ('data_routes', 'register_routes', None),
    ('finance_routes', 'register_routes', None),
    ('ml_routes', 'register_routes', None),
    ('nlq_routes', 'register_routes', None),
    ('tools_routes', 'register_routes', None),
    ('system_routes', 'register_routes', None),
    ('fixedincome_routes', 'register_routes', None),
    ('lineage_routes', 'register_routes', None),
    ('stream_routes', 'register_routes', None),
    ('share_routes', 'register_routes', None),
    ('plugin_routes', 'register_routes', None),
    ('template_routes', 'register_routes', None),
    ('analytics_routes', 'register_routes', None),
    ('db_routes', 'db_bp', None),
    ('did_routes', 'did_bp', None),
    ('scorecard_routes', 'scorecard_bp', None),
    ('report_market', 'report_market_bp', None),
    ('openapi_routes', 'openapi_bp', None),
]

# Blueprint 名称列表（识别应该用 register_blueprint 而不是函数调用）
_BLUEPRINT_NAMES = {
    'auth_bp', 'knowledge_bp', 'scheduler_bp', 'ai_assistant_bp',
    'report_market_bp', 'openapi_bp', 'llm_bp', 'agent_bp',
    'dashboard_bp', 'did_bp', 'scorecard_bp', 'db_bp',
    'search_bp', 'skill_bp', 'egress_bp', 'business_bp',
}

_REGISTERED = set()
_REGISTER_LOCK = threading.Lock()
_ALL_DONE = False


def _register_one(app, limiter, entry):
    """单条路由注册 — 用 try/except 隔离每个模块的失败。"""
    mod_name, export_name, url_prefix = entry
    with _REGISTER_LOCK:
        if mod_name in _REGISTERED:
            return
    try:
        import importlib
        mod = importlib.import_module(f'routes.{mod_name}')
        if export_name in _BLUEPRINT_NAMES:
            bp = getattr(mod, export_name)
            if url_prefix:
                app.register_blueprint(bp, url_prefix=url_prefix)
            else:
                app.register_blueprint(bp)
        else:
            fn = getattr(mod, export_name)
            fn(app, limiter)
        # 注册成功后才标记，失败可重试
        with _REGISTER_LOCK:
            _REGISTERED.add(mod_name)
    except Exception as e:
        try:
            from core_config import logger as _logger
            _logger.warning(f'Route register failed: {mod_name} ({export_name}) -> {e}')
        except Exception:
            print(f'Route register failed: {mod_name} ({export_name}) -> {e}')


def register_all(app, limiter):
    """注册所有路由（分两阶段）
    - 核心路由：同步立即注册（首页 + 关键 API）
    - 拓展路由：后台线程注册 + 404 fallback 按需同步加载
    """
    # 1. 核心路由
    for entry in _CORE_REGISTRARS:
        _register_one(app, limiter, entry)

    # 2. 拓展路由后台线程（启动期不阻塞；让 waitress 先绑定端口）
    def _bg_load():
        global _ALL_DONE
        try:
            import time as _t
            for entry in _LAZY_REGISTRARS:
                _register_one(app, limiter, entry)
                # 每条之间让出 CPU，避免独占；启动期累计阻塞 > 后台均摊
                _t.sleep(0.02)
        finally:
            _ALL_DONE = True
    threading.Thread(target=_bg_load, daemon=True).start()

    # 3. 404 fallback：lazy 路由未注册（或注册曾失败）时强制补齐后重试派发。
    #    不能只看 _ALL_DONE——后台线程可能已跑完但个别模块 import 失败，
    #    此时 _ALL_DONE=True 却永久 404。改为按 _REGISTERED 差集重试。
    @app.errorhandler(404)
    def _lazy_route_404(e):
        missing = [entry for entry in _LAZY_REGISTRARS
                   if entry[0] not in _REGISTERED]
        if missing or not _ALL_DONE:
            for entry in _LAZY_REGISTRARS:
                _register_one(app, limiter, entry)
            # 重新派发：匹配路由后必须先执行蓝图 before_request（local_guard 等），
            # 否则绕过认证直达高危 API
            try:
                from flask import request as _req
                adapter = app.url_map.bind('')
                endpoint, values = adapter.match(_req.path, _req.method)
                # 执行蓝图级 before_request 钩子（认证守卫）
                bp_name = endpoint.split('.')[0] if '.' in endpoint else None
                if bp_name and bp_name in app.blueprints:
                    bp = app.blueprints[bp_name]
                    for handler in bp.before_request_funcs.get(None, []):
                        rv = handler()
                        if rv is not None:
                            return rv
                # 执行应用级 before_request
                rv = app.preprocess_request()
                if rv is not None:
                    return rv
                view_func = app.view_functions[endpoint]
                return app.ensure_sync(view_func)(**values)
            except Exception:
                pass
        from flask import jsonify as _jsonify
        return _jsonify({"ok": False, "error": "资源不存在", "code": "NOT_FOUND"}), 404