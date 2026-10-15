# -*- coding: utf-8 -*-
"""
pipeline.py — 简单 DAG Pipeline 存储与执行（轻量重建）

说明：2026-09-30 清理无用代码时误删原实现；本文件按 `routes/system_routes.py`
的 `/api/pipeline/*` 用法重建最小可用版本，保证 Pipeline 页与 API 不 404。

节点类型见 NODE_INFO；执行为顺序拓扑运行，单节点失败即中止并写入 logs。
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Dict, List, Optional

NODE_INFO = {
    "fetch": {"label": "取数", "desc": "加载 CSV / 金融行情等数据源"},
    "clean": {"label": "清洗", "desc": "缺失值、异常值、类型转换"},
    "analyze": {"label": "分析", "desc": "描述统计 / 相关 / 分组"},
    "chart": {"label": "图表", "desc": "生成可视化图表"},
    "model": {"label": "建模", "desc": "机器学习训练与评估"},
    "report": {"label": "报告", "desc": "导出分析报告"},
    "custom": {"label": "自定义", "desc": "脚本/扩展节点（占位）"},
}

_LOCK = threading.Lock()


class PipelineNode:
    def __init__(self, node_id: str, node_type: str, config: dict | None = None,
                 dependencies: list | None = None):
        self.id = node_id
        self.node_type = node_type
        self.config = dict(config or {})
        self.dependencies = list(dependencies or [])

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "node_type": self.node_type,
            "config": self.config,
            "dependencies": self.dependencies,
        }


class Pipeline:
    def __init__(self, pid: str, name: str, description: str = "", schedule: Any = None):
        self.id = pid
        self.name = name
        self.description = description
        self.schedule = schedule
        self.nodes: List[PipelineNode] = []

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "schedule": self.schedule,
            "nodes": [n.to_dict() for n in self.nodes],
        }


class PipelineExecutor:
    def __init__(self):
        self.logs: List[str] = []

    def log(self, msg: str):
        self.logs.append(msg)


def _topo_order(pipeline: Pipeline) -> List[PipelineNode]:
    by_id = {n.id: n for n in pipeline.nodes}
    seen, order = set(), []

    def visit(nid: str, stack: set):
        if nid in seen:
            return
        if nid in stack:
            raise ValueError(f"依赖成环: {nid}")
        node = by_id.get(nid)
        if node is None:
            return
        stack.add(nid)
        for dep in node.dependencies:
            visit(dep, stack)
        stack.discard(nid)
        seen.add(nid)
        order.append(node)

    for n in pipeline.nodes:
        visit(n.id, set())
    return order


class PipelineStore:
    def __init__(self):
        self._items: Dict[str, Pipeline] = {}

    def create(self, name: str = "", description: str = "", schedule: Any = None) -> Pipeline:
        p = Pipeline(uuid.uuid4().hex[:12], name or "未命名流程", description or "", schedule)
        with _LOCK:
            self._items[p.id] = p
        return p

    def list_all(self) -> List[Pipeline]:
        return list(self._items.values())

    def get(self, pid: str) -> Optional[Pipeline]:
        return self._items.get(pid)

    def delete(self, pid: str) -> None:
        self._items.pop(pid, None)

    def add_node(self, pid: str, node_type: str, config: dict | None = None,
                 dependencies: list | None = None) -> PipelineNode:
        p = self.get(pid)
        if p is None:
            raise ValueError(f"Pipeline 不存在：{pid}")
        node = PipelineNode(uuid.uuid4().hex[:8], node_type, config, dependencies)
        p.nodes.append(node)
        return node

    def remove_node(self, pid: str, nid: str) -> bool:
        p = self.get(pid)
        if p is None:
            return False
        before = len(p.nodes)
        p.nodes = [n for n in p.nodes if n.id != nid]
        return len(p.nodes) < before

    def run_pipeline(self, pid: str) -> PipelineExecutor:
        p = self.get(pid)
        if p is None:
            raise ValueError(f"Pipeline 不存在：{pid}")
        ex = PipelineExecutor()
        ex.log(f"开始运行：{p.name}（{len(p.nodes)} 节点）")
        try:
            order = _topo_order(p)
        except ValueError as e:
            ex.log(f"失败：{e}")
            return ex
        for node in order:
            info = NODE_INFO.get(node.node_type, {})
            ex.log(f"[{node.node_type}] {info.get('label', node.node_type)} ...")
            # 轻量执行：真实分析由各功能页/分析 API 完成；此处保证状态机可跑通
            try:
                cfg = node.config or {}
                if cfg.get("_fail"):
                    raise RuntimeError(cfg.get("_fail") or "节点标记失败")
                time.sleep(0.05)
                ex.log(f"  完成：{node.id}")
            except Exception as e:
                ex.log(f"  失败：{e}")
                ex.log("中止后续节点")
                return ex
        ex.log("全部节点完成")
        return ex


pipeline_store = PipelineStore()
