#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
routes/stream_routes.py — 实时/流式推送（SSE, Server-Sent Events）
- /api/stream/finance : 大盘指数实时推送（复用 dashboard 多源行情，全局节流 30s 抓取）
- /api/stream/tasks   : 异步任务进度实时推送（task_queue）
前端用 EventSource 订阅，服务端主动推送，替代轮询。
"""
import json
import time

from flask import Response, stream_with_context, Blueprint

from core_config import logger
from task_queue import task_queue


# 全局行情节流：多 SSE 连接共享，避免并发打爆外部行情源
_last_quote_fetch = [0.0]
_QUOTE_TTL = 30


def _current_quotes():
    """返回最新指数行情（缓存优先；超过 TTL 由单个连接负责刷新，全局节流）"""
    now = time.time()
    if now - _last_quote_fetch[0] > _QUOTE_TTL:
        try:
            from routes.dashboard_routes import _cache, _fetch_tencent_indices
            q = _fetch_tencent_indices()
            if len(q) >= 3:
                _cache["index"] = q
                _last_quote_fetch[0] = now
        except Exception as e:
            logger.warning(f"stream quote refresh failed: {e}")
    try:
        from routes.dashboard_routes import _cache
        q = _cache.get("index")
        # 缓存里可能是路由返回的错误响应（Response 对象或其元组），不是纯 dict 时直接丢弃，
        # 否则 json.dumps 会抛 "Object of type Response is not JSON serializable" 打断 SSE。
        return q if isinstance(q, dict) else {}
    except Exception:
        return {}


def _sse(data):
    """SSE 帧安全序列化：任何不可序列化对象都不应打断整条流"""
    try:
        body = json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        logger.warning(f"SSE payload not serializable ({type(e).__name__}: {e}), 已降级为空帧")
        body = json.dumps({"type": "error", "ts": time.time(),
                           "error": "payload 序列化失败"}, ensure_ascii=False)
    return f"data: {body}\n\n"


def register_routes(app, limiter):
    bp = Blueprint('stream_routes', __name__)
    # 守卫：SSE 长连接暴露任务队列明细（含任务参数/文件名），远程必须认证；
    # 并附连接数上限（waitress 线程池有限，每个 SSE 永久占用 1 worker）
    from auth import local_guard
    bp.before_request(local_guard)
    _sse_conns = {'n': 0}
    _SSE_MAX_CONNS = 6

    def _sse_slot():
        """连接数闸门：超出上限返回 503，防 SSE 占满线程池（DoS 面）"""
        if _sse_conns['n'] >= _SSE_MAX_CONNS:
            return Response(json.dumps({"error": "SSE 连接数已达上限"}),
                            status=503, mimetype='application/json')
        _sse_conns['n'] += 1
        return None

    def _sse_release():
        _sse_conns['n'] = max(0, _sse_conns['n'] - 1)

    # ============================================================
    #  金融行情实时推送（SSE）
    # ============================================================
    @bp.route("/api/stream/finance")
    def sse_finance():
        """大盘指数 SSE：每 30s 推送一次最新行情（含推数时间戳）"""
        gate = _sse_slot()
        if gate is not None:
            return gate
        def gen():
            try:
                while True:
                    payload = {
                        "type": "quote",
                        "ts": time.time(),
                        "quotes": _current_quotes(),
                    }
                    yield _sse(payload)
                    time.sleep(_QUOTE_TTL)
            finally:
                _sse_release()
        return Response(
            stream_with_context(gen()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ============================================================
    #  异步任务进度推送（SSE）
    # ============================================================
    @bp.route("/api/stream/tasks")
    def sse_tasks():
        """任务进度 SSE：每 3s 推送运行中任务状态"""
        gate = _sse_slot()
        if gate is not None:
            return gate
        def gen():
            try:
                while True:
                    try:
                        running = [t.to_dict() for t in task_queue._tasks.values()
                                   if t.status in ("pending", "running")]
                    except Exception:
                        running = []
                    payload = {"type": "tasks", "ts": time.time(), "tasks": running}
                    yield _sse(payload)
                    time.sleep(3)
            finally:
                _sse_release()
        return Response(
            stream_with_context(gen()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    app.register_blueprint(bp)
