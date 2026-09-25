#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
data_lineage.py - 数据血缘图 & 快照回滚
类 Git commit 的快照管理
"""

import json, os, time, hashlib, shutil
from pathlib import Path
from typing import Dict, List, Optional
from dataclasses import dataclass, field


SNAPSHOT_DIR = Path("output/snapshots")
SNAPSHOT_DIR.mkdir(exist_ok=True)


@dataclass
class DataSnapshot:
    id: str
    parent_id: str = ""
    operation: str = ""
    description: str = ""
    file_path: str = ""
    # 快照对应的原始文件名（output 下的文件名，用于按文件过滤快照列表）
    source_file: str = ""
    file_hash: str = ""
    rows: int = 0
    cols: int = 0
    timestamp: float = 0.0
    tags: List[str] = field(default_factory=list)

    def to_dict(self):
        return self.__dict__.copy()


class SnapshotManager:
    def __init__(self):
        self.index_file = SNAPSHOT_DIR / "index.json"
        self.snapshots: Dict[str, DataSnapshot] = {}
        self._load()

    def _load(self):
        if self.index_file.exists():
            try:
                data = json.load(open(self.index_file, 'r', encoding='utf-8'))
                for sid, s in data.items():
                    self.snapshots[sid] = DataSnapshot(**s)
            except Exception as e:
                import logging
                logging.getLogger('data.tool').warning(f'快照索引加载失败: {e}')

    def _save(self):
        json.dump({sid: s.to_dict() for sid, s in self.snapshots.items()},
                  open(self.index_file, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)

    def create_snapshot(self, source_file: str, operation: str = "",
                       description: str = "", parent_id: str = "",
                       tags: List[str] = None) -> Optional[DataSnapshot]:
        source = Path(source_file)
        if not source.exists():
            return None
        
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        content = source.read_bytes()
        snapshot_id = hashlib.sha256(content + str(time.time()).encode()).hexdigest()[:12]
        
        ext = source.suffix or '.csv'
        snapshot_path = SNAPSHOT_DIR / f"{snapshot_id}{ext}"
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, snapshot_path)
        
        import pandas as pd
        try:
            df = pd.read_csv(snapshot_path, encoding='utf-8-sig')
            rows, cols = df.shape
        except Exception:
            rows, cols = 0, 0
        
        snapshot = DataSnapshot(
            id=snapshot_id, parent_id=parent_id, operation=operation,
            description=description, file_path=str(snapshot_path),
            source_file=source.name,
            file_hash=hashlib.sha256(content).hexdigest(),
            rows=rows, cols=cols, timestamp=time.time(), tags=tags or []
        )
        
        self.snapshots[snapshot_id] = snapshot
        self._save()
        return snapshot

    def rollback(self, snapshot_id: str, target_file: str) -> Dict:
        snapshot = self.snapshots.get(snapshot_id)
        if not snapshot:
            return {"ok": False, "error": f"快照 {snapshot_id} 不存在"}
        
        snapshot_path = Path(snapshot.file_path)
        if not snapshot_path.exists():
            return {"ok": False, "error": "快照文件已丢失"}
        
        target = Path(target_file)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(snapshot_path, target)
        
        return {"ok": True, "message": f"已回滚到快照 {snapshot_id[:8]}", "snapshot": snapshot.to_dict()}

    def list_snapshots(self) -> List[DataSnapshot]:
        return sorted(self.snapshots.values(), key=lambda s: s.timestamp, reverse=True)


snapshot_mgr = SnapshotManager()


# ============================================================
#  数据血缘追踪（Data Lineage）
#  记录：数据从哪来（来源）、经过哪些处理（处理链）、被哪些报告/模型引用（下游）
#  持久化：output/data_lineage/events.json
# ============================================================

LINEAGE_DIR = Path(__file__).parent / "output" / "data_lineage"
LINEAGE_DIR.mkdir(parents=True, exist_ok=True)

# 事件类型（op）说明
LINEAGE_OPS = {
    "upload": "上传文件",
    "save": "新建文件",
    "scrape": "网页爬取",
    "fetch": "金融数据拉取",
    "clean": "数据清洗",
    "clean_save": "清洗结果保存",
    "feature": "特征工程",
    "ml_train": "模型训练",
    "report": "报告生成",
    "db_import": "数据库导入",
}


class LineageManager:
    """轻量数据血缘追踪：追加式事件日志，支持上游/下游溯源。"""

    def __init__(self):
        self.events_file = LINEAGE_DIR / "events.json"
        self._events: List[Dict] = []
        self._lock = __import__('threading').Lock()
        self._load()

    def _load(self):
        try:
            if self.events_file.exists():
                data = json.load(open(self.events_file, 'r', encoding='utf-8'))
                if isinstance(data, list):
                    self._events = data
        except Exception as e:
            import logging
            logging.getLogger('data.tool').warning(f'血缘事件加载失败: {e}')

    def _save(self):
        try:
            tmp = self.events_file.with_suffix('.json.tmp')
            json.dump(self._events[-2000:], open(tmp, 'w', encoding='utf-8'),
                      ensure_ascii=False, indent=1)
            tmp.replace(self.events_file)
        except Exception as e:
            import logging
            logging.getLogger('data.tool').warning(f'血缘事件保存失败: {e}')

    def record(self, op: str, src: str = "", dst: str = "",
               detail: str = "", ref: str = "") -> Dict:
        """记录一条血缘事件。src=来源文件/URL，dst=产出文件，ref=引用方（如报告名）"""
        with self._lock:
            ev = {
                "ts": time.time(),
                "op": op,
                "src": src or "",
                "dst": dst or "",
                "detail": detail or "",
                "ref": ref or "",
            }
            self._events.append(ev)
            self._save()
            return ev

    def recent(self, limit: int = 50) -> List[Dict]:
        return list(reversed(self._events[-limit:]))

    def upstream(self, file: str) -> List[Dict]:
        """某文件的上游：所有产出过该文件的事件（来源链）"""
        return [e for e in reversed(self._events) if e["dst"] == file]

    def downstream(self, file: str) -> List[Dict]:
        """某文件的下游：所有以该文件为输入的事件（被哪些处理/报告引用）"""
        return [e for e in reversed(self._events) if e["src"] == file]

    def search(self, keyword: str, limit: int = 100) -> List[Dict]:
        kw = keyword.strip().lower()
        if not kw:
            return []
        out = []
        for e in reversed(self._events):
            if kw in e["src"].lower() or kw in e["dst"].lower() or kw in e["detail"].lower() or kw in e["ref"].lower():
                out.append(e)
                if len(out) >= limit:
                    break
        return out

    def graph(self) -> Dict:
        """全图：节点 = 出现过 src/dst 的文件；边 = src -> dst 及其次数"""
        nodes, edges = {}, {}
        for e in self._events:
            src, dst = e.get("src", ""), e.get("dst", "")
            for n in (src, dst):
                if n:
                    nodes.setdefault(n, {"name": n, "count": 0})
                    nodes[n]["count"] += 1
            if src and dst and src != dst:
                key = (src, dst)
                edges[key] = edges.get(key, 0) + 1
        return {
            "nodes": [{"name": k, "count": v["count"]} for k, v in nodes.items()],
            "edges": [{"src": s, "dst": d, "count": c} for (s, d), c in edges.items()],
        }

    def stats(self) -> Dict:
        from collections import Counter
        op_cnt = Counter(e.get("op", "") for e in self._events)
        files = set()
        for e in self._events:
            if e.get("src"):
                files.add(e["src"])
            if e.get("dst"):
                files.add(e["dst"])
        reports = [e for e in self._events if e.get("op") == "report"]
        return {
            "total_events": len(self._events),
            "files": len(files),
            "reports": len(reports),
            "op_dist": dict(op_cnt),
            "op_labels": LINEAGE_OPS,
        }


lineage_mgr = LineageManager()
