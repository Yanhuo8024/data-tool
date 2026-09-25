#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pipeline.py - DAG Pipeline Engine
"""
import json, time, uuid, threading
from typing import Dict, Optional, List, Any, Callable
from dataclasses import dataclass, field


@dataclass
class PipelineNode:
    id: str
    node_type: str
    config: Dict
    dependencies: List[str] = field(default_factory=list)
    status: str = "pending"
    output: Any = None
    error: str = None

    def to_dict(self):
        return {"id": self.id, "node_type": self.node_type, "config": self.config,
                "dependencies": self.dependencies, "status": self.status, "error": self.error}


@dataclass
class Pipeline:
    id: str
    name: str
    description: str
    nodes: Dict[str, PipelineNode]
    schedule: Optional[str] = None
    enabled: bool = True
    last_run: float = None
    last_status: str = "never"

    def to_dict(self):
        return {"id": self.id, "name": self.name, "description": self.description,
                "nodes": {k: v.to_dict() for k, v in self.nodes.items()},
                "schedule": self.schedule, "enabled": self.enabled,
                "last_run": self.last_run, "last_status": self.last_status}


class PipelineExecutor:
    def __init__(self, pipeline: Pipeline):
        self.pipeline = pipeline
        self.running = False
        self.logs: List[str] = []

    def log(self, msg):
        from datetime import datetime
        self.logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
        print(f"[Pipeline {self.pipeline.name}] {msg}")

    def validate(self):
        v, temp = set(), set()
        def has_cycle(nid):
            if nid in temp: return True
            if nid in v: return False
            temp.add(nid)
            for dep in self.pipeline.nodes[nid].dependencies:
                if dep not in self.pipeline.nodes or has_cycle(dep): return True
            temp.discard(nid); v.add(nid)
            return False
        return not any(has_cycle(nid) for nid in self.pipeline.nodes)

    def get_execution_order(self) -> List[List[str]]:
        layers, executed, remaining = [], set(), set(self.pipeline.nodes.keys())
        while remaining:
            layer = [nid for nid in remaining if all(d in executed for d in self.pipeline.nodes[nid].dependencies)]
            if not layer: raise Exception("Cannot resolve DAG")
            layers.append(layer); executed.update(layer); remaining -= set(layer)
        return layers

    def execute(self):
        self.running = True; self.logs = []; self.log("Started")
        try:
            for idx, layer in enumerate(self.get_execution_order()):
                self.log(f"Layer {idx+1}: {layer}")
                for nid in layer:
                    node = self.pipeline.nodes[nid]
                    node.status = "running"
                    self.log(f"  Running: {nid} ({node.node_type})")
                    try:
                        ctx = {d: self.pipeline.nodes[d].output for d in node.dependencies}
                        node.output = NODE_REGISTRY[node.node_type](node.config, ctx)
                        node.status = "completed"
                        self.log(f"  Done: {nid}")
                    except Exception as e:
                        node.status = "failed"; node.error = str(e)
                        self.log(f"  Failed: {nid}: {e}"); raise
            self.pipeline.last_status = "completed"
        except Exception as e:
            self.pipeline.last_status = "failed"; self.log(f"Failed: {e}")
        finally:
            self.running = False; self.pipeline.last_run = time.time(); self.log("Finished")


def _get_input(ctx: dict):
    if not ctx: return None
    return ctx[list(ctx.keys())[-1]]


def node_fetch(config: dict, ctx: dict):
    import akshare as ak
    symbol = config.get("symbol", "000001")
    df = ak.stock_zh_a_hist(symbol=symbol, period=config.get("period", "daily"),
                            start_date=config.get("start", "").replace("-", ""),
                            end_date=config.get("end", "").replace("-", ""),
                            adjust=config.get("adjust", "qfq"))
    if df is None or df.empty: raise Exception(f"No data for {symbol}")
    cm = {"日期": "date", "开盘": "open", "收盘": "close", "最高": "high",
          "最低": "low", "成交量": "volume", "成交额": "amount", "涨跌幅": "pct_change"}
    df = df.rename(columns={k: v for k, v in cm.items() if k in df.columns})
    return {"data": df.to_dict(orient="records"), "rows": len(df), "symbol": symbol}


def node_clean(config: dict, ctx: dict):
    import pandas as pd, numpy as np
    inp = _get_input(ctx)
    df = pd.DataFrame(inp["data"] if isinstance(inp, dict) and "data" in inp else inp)
    if df.empty: raise Exception("Empty data")
    method = config.get("method", "dropna"); before = len(df)
    if method == "dropna": df = df.dropna()
    elif method == "fill_mean":
        for c in df.select_dtypes(include=np.number).columns: df[c] = df[c].fillna(df[c].mean())
    elif method == "fill_zero": df = df.fillna(0)
    return {"data": df.to_dict(orient="records"), "rows": len(df), "removed": before - len(df)}


def node_model(config: dict, ctx: dict):
    from sklearn.linear_model import LinearRegression
    from sklearn.ensemble import RandomForestRegressor, GradientBoostingRegressor
    from sklearn.metrics import mean_absolute_error
    import pandas as pd
    inp = _get_input(ctx)
    df = pd.DataFrame(inp["data"] if isinstance(inp, dict) and "data" in inp else inp)
    if df.empty: raise Exception("Empty data")
    model_type = config.get("model_type", "linear")
    target = config.get("target", "close")
    features = config.get("features", [c for c in df.columns if c != target and df[c].dtype in ['float64', 'int64']])
    if target not in df.columns: raise Exception(f"Target '{target}' not found")
    df = df.dropna(subset=features + [target]); X, y = df[features].values, df[target].values
    if len(X) < 5: raise Exception("Not enough data")
    model = {"linear": LinearRegression(), "rf": RandomForestRegressor(n_estimators=30, random_state=42),
             "gbdt": GradientBoostingRegressor(n_estimators=30, random_state=42)}.get(model_type, LinearRegression())
    model.fit(X, y); score = model.score(X, y); mae = mean_absolute_error(y, model.predict(X))
    imp = getattr(model, 'feature_importances_', [1.0/len(features)]*len(features))
    return {"model_type": model_type, "score": round(score, 4), "mae": round(mae, 4),
            "feature_importance": dict(zip(features, [round(abs(x), 4) for x in imp]))}


def node_predict(config: dict, ctx: dict):
    inp = _get_input(ctx)
    if not inp or "score" not in inp: raise Exception("No trained model")
    days = config.get("days", 5); score, mae = inp["score"], inp.get("mae", 0)
    predictions = [{"day": i, "predicted": round(score + (i * mae * 0.01), 4)} for i in range(1, days + 1)]
    return {"forecast_days": days, "model_score": score, "predictions": predictions}


NODE_REGISTRY: Dict[str, Callable] = {
    "fetch": node_fetch, "clean": node_clean, "model": node_model, "predict": node_predict,
}

NODE_INFO = {
    "fetch":   {"label": "数据获取", "params": ["symbol", "period", "start", "end", "adjust"]},
    "clean":   {"label": "数据清洗", "params": ["method"]},
    "model":   {"label": "模型训练", "params": ["model_type", "target", "features"]},
    "predict": {"label": "预测推理", "params": ["days"]},
}


class PipelineStore:
    def __init__(self, filepath="output/pipelines.json"):
        self.filepath = filepath; self.pipelines: Dict[str, Pipeline] = {}; self._load()

    def _load(self):
        if os.path.exists(self.filepath):
            try:
                data = json.load(open(self.filepath, 'r', encoding='utf-8'))
                for pid, p in data.items():
                    nodes = {n: PipelineNode(id=n, node_type=d["node_type"],
                            config=d["config"], dependencies=d.get("dependencies", []))
                             for n, d in p.get("nodes", {}).items()}
                    self.pipelines[pid] = Pipeline(id=pid, name=p.get("name", ""),
                        description=p.get("description", ""), nodes=nodes,
                        schedule=p.get("schedule"), enabled=p.get("enabled", True),
                        last_run=p.get("last_run"), last_status=p.get("last_status", "never"))
            except Exception as e: print(f"[PipelineStore] Load error: {e}")

    def _save(self):
        os.makedirs(os.path.dirname(self.filepath), exist_ok=True)
        data = {pid: p.to_dict() for pid, p in self.pipelines.items()}
        json.dump(data, open(self.filepath, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)

    def create(self, name, description="", schedule=None):
        pid = f"pipe_{uuid.uuid4().hex[:8]}"
        p = Pipeline(id=pid, name=name, description=description, nodes={}, schedule=schedule)
        self.pipelines[pid] = p; self._save(); return p

    def get(self, pid): return self.pipelines.get(pid)

    def delete(self, pid):
        if pid in self.pipelines: del self.pipelines[pid]; self._save()

    def list_all(self): return list(self.pipelines.values())

    def add_node(self, pid, node_type, config, dependencies=None):
        p = self.pipelines.get(pid)
        if not p: raise ValueError("Pipeline not found")
        nid = f"node_{len(p.nodes)}_{node_type}"
        p.nodes[nid] = PipelineNode(id=nid, node_type=node_type, config=config, dependencies=dependencies or [])
        self._save(); return p.nodes[nid]

    def remove_node(self, pid, nid):
        p = self.pipelines.get(pid)
        if p and nid in p.nodes:
            del p.nodes[nid]
            for n in p.nodes.values():
                if nid in n.dependencies: n.dependencies.remove(nid)
            self._save()
            return True
        return False

    def run_pipeline(self, pid):
        p = self.pipelines.get(pid)
        if not p: raise ValueError("Pipeline not found")
        executor = PipelineExecutor(p)
        if not executor.validate(): raise Exception("DAG validation failed")
        executor.execute(); self._save(); return executor


import os
pipeline_store = PipelineStore()
