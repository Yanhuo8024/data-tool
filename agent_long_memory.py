#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent_long_memory.py - Agent 长期记忆引擎
==========================================

设计目标
--------
- 持久化用户偏好、角色设定、关注列表、历史结论摘要
- 跨会话可用（按 user_id 键），区别于 agent_memory.SessionMemoryManager 的会话级状态
- JSON 原子写（临时文件 + rename），无外部依赖

字段约定
--------
- role:        用户角色（如"基金经理"、"研究员"、"学生"）
- preferences: 偏好 dict（chart_type、color、language、report_style 等）
- watchlist:   关注的股票/指数代码列表
- history_summary: 近期关键结论 [{topic, key_finding, ts}, ...]
- updated_at:  最近更新时间
"""

import json
import time
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any

from term_extract import overlap  # 轻量相关度（零依赖）


logger = logging.getLogger(__name__)


# 长期记忆落盘目录：<项目根>/data/memory/long_term/
_MEMORY_ROOT = Path(__file__).parent / "data" / "memory" / "long_term"


class LongTermMemory:
    """长期记忆（按 user_id 跨会话持久化）"""

    SCHEMA_VERSION = 1
    MAX_HISTORY_KEEP = 20      # 历史结论摘要保留条数上限
    MAX_WATCHLIST = 50         # 关注列表上限

    def __init__(self, user_id: str = "default"):
        self.user_id = user_id or "default"
        self.path = _MEMORY_ROOT / f"{self._safe_id(self.user_id)}.json"
        self._data: Dict[str, Any] = self._load()

    # ---------------------- 文件名安全 ----------------------
    @staticmethod
    def _safe_id(user_id: str) -> str:
        """清洗 user_id 防路径越界，仅保留字母数字与 -_.，最大 64 字符"""
        s = str(user_id or "default")
        return "".join(c if c.isalnum() or c in "-_." else "_" for c in s)[:64] or "default"

    # ---------------------- 加载/保存 ----------------------
    def _load(self) -> Dict[str, Any]:
        defaults = {
            "schema_version": self.SCHEMA_VERSION,
            "user_id": self.user_id,
            "role": "",
            "preferences": {},
            "watchlist": [],
            "history_summary": [],
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        if not self.path.exists():
            return defaults
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 向前兼容：缺失字段补齐
            for k, v in defaults.items():
                data.setdefault(k, v)
            # 类型兜底（防止手工损坏）
            if not isinstance(data.get("preferences"), dict):
                data["preferences"] = {}
            if not isinstance(data.get("watchlist"), list):
                data["watchlist"] = []
            if not isinstance(data.get("history_summary"), list):
                data["history_summary"] = []
            return data
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"长期记忆读取失败，回退默认: {e}")
            return defaults

    def _save(self) -> None:
        """原子写：临时文件 + replace；落盘失败不阻塞主流程"""
        self._data["updated_at"] = time.time()
        try:
            _MEMORY_ROOT.mkdir(parents=True, exist_ok=True)
            tmp_path = self.path.with_suffix(".json.tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            tmp_path.replace(self.path)
        except (OSError, SystemExit) as e:
            # SystemExit 也须捕获：运行环境的写入/删除守卫会以 SystemExit 中断磁盘操作，
            # 长期记忆属旁路增强，落盘失败绝不阻塞主流程。
            logger.warning(f"长期记忆落盘失败（{self.user_id}）: {e}")

    # ---------------------- 写入接口 ----------------------
    def update_role(self, role: str) -> None:
        """更新用户角色（仅在变化时落盘）"""
        role = str(role or "").strip()
        if role and role != self._data.get("role"):
            self._data["role"] = role
            self._save()

    def update_preferences(self, **kwargs) -> None:
        """更新偏好；传入 None 的字段跳过"""
        changed = False
        for k, v in kwargs.items():
            if v is None:
                continue
            if self._data["preferences"].get(k) != v:
                self._data["preferences"][k] = v
                changed = True
        if changed:
            self._save()

    def add_to_watchlist(self, code: str) -> None:
        """添加到关注列表（去重保序，最多 50）"""
        code = str(code or "").strip()
        if not code:
            return
        wl = self._data.setdefault("watchlist", [])
        if code not in wl:
            wl.append(code)
            if len(wl) > self.MAX_WATCHLIST:
                wl[:] = wl[-self.MAX_WATCHLIST:]
            self._save()

    def record_interaction(self, topic: str, key_finding: str) -> None:
        """记录一次关键交互（用于未来衔接）"""
        topic = str(topic or "").strip()
        if not topic:
            return
        entry = {
            "topic": topic[:200],
            "key_finding": str(key_finding or "")[:400],
            "ts": time.time(),
        }
        history = self._data.setdefault("history_summary", [])
        history.append(entry)
        if len(history) > self.MAX_HISTORY_KEEP:
            history[:] = history[-self.MAX_HISTORY_KEEP:]
        self._save()

    # ---------------------- 读取接口 ----------------------
    def get(self) -> Dict[str, Any]:
        """获取完整快照（拷贝，避免外部修改污染内部状态）"""
        return dict(self._data)

    def _pick_history(self, history: List[Dict], query: str = "", keep: int = 3) -> List[Dict]:
        """按相关度挑选历史结论。

        原实现固定取最近 keep 条：用户一旦换话题，注入的仍是无关主题的旧结论——
        既占 Prompt 预算，又容易把模型往旧主题上带。改为「相关度 + 时间衰减」排序：
        相关度权重 0.75、时效权重 0.25；全部不相关时退化为「最近 keep 条」。
        不传 query 时行为与旧版完全一致（向后兼容）。
        """
        if not query:
            return list(history[-keep:])
        now = time.time()
        scored = []
        for idx, h in enumerate(history):
            text = f"{h.get('topic', '')} {h.get('key_finding', '')}"
            rel = overlap(query, text)
            age_days = max((now - float(h.get('ts', 0) or 0)) / 86400.0, 0.0)
            decay = 1.0 / (1.0 + age_days / 30.0)
            scored.append((rel * 0.75 + decay * 0.25, idx, h))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [h for _score, _idx, h in scored[:keep]]

    def to_natural_language(self, query: str = "") -> str:
        """转化为自然语言片段，供 Planner / Agent Prompt 注入。

        query 非空时历史结论按相关度挑选（见 _pick_history），否则取最近 3 条。
        """
        parts: List[str] = []
        if self._data.get("role"):
            parts.append(f"用户角色: {self._data['role']}")
        prefs = self._data.get("preferences") or {}
        if prefs:
            items = ", ".join(f"{k}={v}" for k, v in prefs.items())
            parts.append(f"用户偏好: {items}")
        wl = self._data.get("watchlist") or []
        if wl:
            tail = "等" if len(wl) > 10 else ""
            parts.append(f"关注标的: {', '.join(wl[:10])}{tail}")
        history = self._data.get("history_summary") or []
        if history:
            lines: List[str] = []
            for h in self._pick_history(history, query):
                age_days = max(int((time.time() - h.get("ts", 0)) / 86400), 0)
                tag = f"{age_days}天前" if age_days > 0 else "今日"
                lines.append(f"- [{tag}] 主题「{h['topic']}」: {h.get('key_finding', '')[:120]}")
            parts.append("近期重要结论（避免重复已说过的话）：\n" + "\n".join(lines))
        return " | ".join(parts) if parts else ""


# ---------------------- 模块级缓存 ----------------------
_LONG_TERM_CACHE: Dict[str, LongTermMemory] = {}


def get_long_term(user_id: str = "default") -> LongTermMemory:
    """按 user_id 获取（缓存）长期记忆实例"""
    key = user_id or "default"
    if key not in _LONG_TERM_CACHE:
        _LONG_TERM_CACHE[key] = LongTermMemory(key)
    return _LONG_TERM_CACHE[key]


def clear_cache() -> None:
    """清空模块缓存（测试 / 用户登出场景）"""
    _LONG_TERM_CACHE.clear()