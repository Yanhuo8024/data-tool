#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
trace_otel.py - Agent 追踪的 OpenTelemetry 兼容槽位
=====================================================
自 trace_store.py 拆出（2026-09-22）：OTel 导出是**完全自洽子域**——可选依赖、独立开关、
失败绝不影响主流程，与 JSONL 落盘零耦合（trace_store → trace_otel 单向依赖，无环）。
拆分同时让 trace_store.py 回到行数门禁上限之内。

启用方式（默认关闭、零依赖）：
    设置 TRACE_OTEL_ENDPOINT=<collector 地址> 或 TRACE_EXPORTER=otel，
    且环境安装了 opentelemetry 相关包。
未启用 / 未安装依赖时 emit_span() 安全跳过，调用方无需判空。

注意：开关判定在**本模块**内进行。要运行时改开关请改 trace_otel.OTEL_ENABLED；
改 trace_store.OTEL_ENABLED 只影响那个 re-export 出来的名字副本，不改变实际行为。
"""
import os
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

# OpenTelemetry 兼容槽位开关：默认关闭（本地零依赖）；设置 TRACE_OTEL_ENDPOINT 后开启
OTEL_ENDPOINT = os.environ.get("TRACE_OTEL_ENDPOINT", "").strip()
OTEL_ENABLED = bool(OTEL_ENDPOINT) or os.environ.get("TRACE_EXPORTER", "") == "otel"


def _otel_emit(run_id: str, span_attrs: Dict[str, Any]) -> None:
    """将一次事件发射为一个 OTel span（仅当启用时生效）。

    输入为"已含 name/attrs/seq/error 的事件"，这里只做极轻的适配转发。
    """
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.trace import Status, StatusCode

        _otel_emit._provider = getattr(_otel_emit, "_provider", None)
        if _otel_emit._provider is None:
            provider = TracerProvider(resource=Resource.create({"service.name": "data-agent"}))
            provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint=OTEL_ENDPOINT)))
            trace.set_tracer_provider(provider)
            _otel_emit._provider = provider
        tracer = trace.get_tracer("data-agent", "1.0")
        with tracer.start_as_current_span(f"agent.{span_attrs.get('name', 'event')}") as span:
            span.set_attribute("run_id", run_id)
            span.set_attribute("seq", span_attrs.get("seq", 0))
            for k, v in (span_attrs.get("attrs") or {}).items():
                try:
                    span.set_attribute(str(k), str(v)[:2000])
                except Exception as e:
                    logger.debug("span 属性写入跳过 %s: %s", k, e)
            if span_attrs.get("error"):
                span.set_status(Status(StatusCode.ERROR, str(span_attrs["error"])[:300]))
    except Exception as e:  # 远端导出失败绝不影响主流程
        logger.debug("OTel 导出跳过: %s", e)


def emit_span(run_id: str, name: str, attrs: Dict[str, Any] = None,
              seq: int = 0, error: str = None) -> None:
    """OpenTelemetry 兼容槽位入口：业务代码统一调用此钩子。

    未启用 OTel、或未安装依赖时安全跳过。
    """
    if not OTEL_ENABLED:
        return
    try:
        _otel_emit(run_id, {"name": name, "attrs": attrs or {}, "seq": seq, "error": error})
    except Exception as e:
        logger.debug("emit_span 转发失败: %s", e)
