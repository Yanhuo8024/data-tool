#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
conversation_memory.py - 短期 + 长期对话记忆（7.1）

- short_term：当前会话最近 N 轮（意图 + 实体 + 内容），进程内保留
- long_term ：用户偏好（常用数据集、高频指标、高频意图），JSON 持久化到磁盘

设计：
- 轻量、纯同步、无网络；失败静默降级（所有方法 try/except）
- 与 ai_assistant.ChatManager（历史文本）互补：这里记录的是结构化路由元数据
"""

import os
import json
import time
import logging
from typing import Dict, Any, List, Optional
from collections import Counter
from pathlib import Path

logger = logging.getLogger(__name__)

_MEMORY_FILE = Path(__file__).parent / "data" / "conversation_memory.json"


class ConversationMemory:
    def __init__(self, max_short: int = 6):
        self.max_short = max_short
        # P1-2 多会话隔离：short_term 按 conversation_id 分桶。
        # 旧实现 short_term 是进程级单一列表 → 切换会话后工作记忆串台
        # （会话B 首句「然后呢」被续接成会话A 的意图且不澄清）。
        self._buckets: Dict[str, List[Dict[str, Any]]] = {}
        self.active_conversation_id: str = 'legacy'
        self.short_term: List[Dict[str, Any]] = self._buckets.setdefault('legacy', [])
        self.long_term: Dict[str, Any] = self._load_long_term()

    # ---------- P1-2 会话隔离 ----------
    def set_active_conversation(self, conversation_id: Optional[str]) -> str:
        """切换当前会话桶；后续 short_term 读写仅作用于该会话。返回规范化后的会话 ID。"""
        cid = conversation_id or 'legacy'
        self.active_conversation_id = cid
        self.short_term = self._buckets.setdefault(cid, [])
        return cid

    def forget_conversation(self, conversation_id: str) -> None:
        """清空并移除某会话的短期记忆桶（供会话删除时调用）。"""
        cid = conversation_id or 'legacy'
        self._buckets.pop(cid, None)
        if self.active_conversation_id == cid:
            self.short_term = self._buckets.setdefault(cid, [])

    # ---------- 持久化（长期偏好） ----------
    def _load_long_term(self) -> Dict[str, Any]:
        try:
            if _MEMORY_FILE.exists():
                data = json.loads(_MEMORY_FILE.read_text(encoding='utf-8'))
                if isinstance(data, dict):
                    return data
        except Exception as e:
            logger.debug("加载长期记忆失败: %s", e)
        return {"files": Counter(), "intents": Counter(), "indicators": Counter(), "targets": Counter()}

    def _save_long_term(self):
        try:
            _MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            payload = {k: dict(v) for k, v in self.long_term.items()}
            _MEMORY_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception as e:
            logger.debug("保存长期记忆失败: %s", e)

    # P2-2：写盘节流——每消息同步全量写 JSON 改为最短间隔合并写，
    # 进程退出/显式 flush 时落盘（单管理员场景可接受丢失窗口内增量）
    _SAVE_MIN_INTERVAL = 10.0  # 秒
    _COUNTER_MAX = 100         # Counter 无上限增长 → 截断 top 100
    _last_save_ts = 0.0

    @classmethod
    def _trim_counter(cls, c):
        try:
            from collections import Counter as _C
            if len(c) > cls._COUNTER_MAX:
                return _C(dict(c.most_common(cls._COUNTER_MAX)))
        except Exception:
            pass
        return c

    def _save_long_term_throttled(self):
        import time as _t
        now = _t.time()
        if now - getattr(self, '_last_save_ts', 0.0) < self._SAVE_MIN_INTERVAL:
            self._dirty = True
            return
        self._last_save_ts = now
        self._dirty = False
        self._save_long_term()

    def flush(self):
        """立即落盘长期记忆（供显式调用；节流窗口内的增量在此补写）"""
        self._dirty = False
        self._save_long_term()

    # ---------- 写入 ----------
    def add_turn(self, user_message: str, router: Dict[str, Any], file_name: str = None):
        """记录一轮对话的结构化元数据（意图/实体/文件）"""
        try:
            turn = {
                "ts": time.time(),
                "message": user_message[:200],
                "intent": router.get("intent", "chat"),
                "depth": router.get("depth", "balanced"),
                "entities": router.get("entities", {}),
                "file": file_name,
            }
            self.short_term.append(turn)
            # 原地截断（del 切片）而非重新赋值，保证 self.short_term 与所属会话桶是同一对象
            if len(self.short_term) > self.max_short:
                del self.short_term[:len(self.short_term) - self.max_short]
            self._buckets[self.active_conversation_id] = self.short_term

            lt = self.long_term
            lt["intents"][router.get("intent", "chat")] += 1
            ent = router.get("entities", {})
            if ent.get("indicator"):
                for i in ent["indicator"]:
                    lt["indicators"][i] += 1
            if ent.get("target"):
                for t in ent["target"]:
                    lt["targets"][t] += 1
            if file_name:
                lt["files"][file_name] += 1
            # P2-2：Counter 截断 + 节流写盘
            for k in ("intents", "indicators", "targets", "files"):
                lt[k] = self._trim_counter(lt[k])
            self._save_long_term_throttled()
        except Exception as e:
            logger.debug("记忆记录失败（忽略）: %s", e)

    # ---------- 读取 ----------
    def top_files(self, n: int = 3) -> List[str]:
        return [f for f, _ in self.long_term.get("files", {}).most_common(n)]

    def top_indicators(self, n: int = 5) -> List[str]:
        return [i for i, _ in self.long_term.get("indicators", {}).most_common(n)]

    def context_hint(self, max_files: int = 3, max_inds: int = 5) -> str:
        """生成用户偏好提示（注入 agent/LLM，辅助定位常用数据与指标）"""
        parts = []
        files = self.top_files(max_files)
        if files:
            parts.append("用户常用数据集: " + ", ".join(files))
        inds = self.top_indicators(max_inds)
        if inds:
            parts.append("用户常关注的指标/维度: " + ", ".join(inds))
        return "\n".join(parts)

    def recent_intents(self, n: int = 3) -> List[str]:
        return [t.get("intent", "") for t in self.short_term[-n:]]

    def reset(self):
        """清空全部会话的短期记忆与长期偏好（测试/显式重置用）。"""
        self._buckets.clear()
        self.active_conversation_id = 'legacy'
        self.short_term = self._buckets.setdefault('legacy', [])
        self.long_term = {"files": Counter(), "intents": Counter(), "indicators": Counter(), "targets": Counter()}
        self._save_long_term()

    # ---------- ⑥ 会话摘要压缩（SummaryBufferMemory）------------
    _SUMMARY_KEY = "conversation_summary"

    def get_summary(self) -> str:
        """返回当前会话摘要（压缩后），失败返回空"""
        return self.long_term.get(self._SUMMARY_KEY, "")

    def update_summary(self, summary: str):
        """更新会话摘要（每轮对话后由外部调用，存到 long_term）"""
        try:
            self.long_term[self._SUMMARY_KEY] = (summary or "")[:2000]
            self._save_long_term()
        except Exception as e:
            logger.debug("更新摘要失败: %s", e)

    def get_full_context(self) -> str:
        """
        返回完整上下文：最近 N 轮 + 摘要压缩 + 长期偏好。
        供外部注入 LLM Prompt 使用。
        """
        parts = []
        # 长会话摘要
        summ = self.get_summary()
        if not summ:
            summ = self.summarize_rule_based()
        if summ:
            parts.append(f"【会话摘要】{summ}")
        # 历史偏好
        hint = self.context_hint()
        if hint:
            parts.append(hint)
        return "\n".join(parts)

    def maybe_update_summary_llm(self, user_message: str, assistant_reply: str,
                                  llm_config: dict = None, every_n_turns: int = 4) -> bool:
        """语义记忆摘要（数据闭环）：每 N 轮用 LLM 提炼关键事实摘要（异步线程，零感知）。

        - 摘要内容：已分析的标的、关键数据、未决问题——比规则式摘要质量更高；
        - 无 llm_config（免费额度敏感/测试环境）时静默跳过，规则式摘要仍可用；
        - 异步线程执行，绝不阻塞回复，失败静默。返回是否触发了摘要任务。"""
        try:
            if not llm_config:
                return False
            if len(self.short_term) == 0 or len(self.short_term) % every_n_turns != 0:
                return False

            mem = self

            def _work():
                try:
                    from llm_client import chat
                    turns = "\n".join(
                        f"- 用户: {t.get('message', '')[:80]}" for t in mem.short_term[-every_n_turns:])
                    resp = chat(
                        [{'role': 'system',
                          'content': ('你是会话摘要器。把对话提炼为不超过 120 字的关键事实摘要：'
                                      '已分析的标的、关键数据结论、未决问题。只输出摘要正文，不要评论。')},
                         {'role': 'user',
                          'content': f"最近对话：\n{turns}\n\n最后一条回复：{assistant_reply[:300]}"}],
                        llm_config, max_tokens=200, temperature=0)
                    content = (resp.get('content') or '').strip() if isinstance(resp, dict) else ''
                    if content:
                        mem.update_summary(content)
                except Exception as e:
                    logger.debug("LLM 摘要失败（保留规则式摘要）: %s", e)

            import threading
            threading.Thread(target=_work, daemon=True).start()
            return True
        except Exception:
            return False

    def summarize_rule_based(self) -> str:
        """
        零 LLM 的轻量摘要压缩：把最近会话的关键要素（意图/实体/文件）折叠成一句。
        仅在会话发生多轮时才有意义；用于超长历史的紧凑复原，不消耗免费额度。
        """
        if len(self.short_term) < 2:
            return ""
        intents = {}
        entities_all = []
        files = []
        for t in self.short_term:
            i = t.get("intent", "")
            intents[i] = intents.get(i, 0) + 1
            ent = t.get("entities", {})
            if "indicator" in ent:
                entities_all += ent["indicator"]
            if "target" in ent:
                entities_all += ent["target"]
            if t.get("file"):
                files.append(t["file"])
        uniq_int = "、".join(f"{k}({v}次)" for k, v in intents.items())
        uniq_ent = "、".join(dict.fromkeys(entities_all))[:60]
        f = "、".join(dict.fromkeys(files))
        parts = [f"已完成 {len(self.short_term)} 轮，包含意图: {uniq_int}"]
        if uniq_ent:
            parts.append(f"涉及: {uniq_ent}")
        if f:
            parts.append(f"数据集: {f}")
        return "；".join(parts)


# 全局实例（供 ai_assistant 复用）
_memory = ConversationMemory()


def get_memory() -> ConversationMemory:
    return _memory


# P2-2：节流写盘的兜底——进程退出时把窗口内未落盘的增量补写
import atexit as _atexit


def _flush_memory_at_exit():
    try:
        _memory.flush()
    except Exception:
        pass


_atexit.register(_flush_memory_at_exit)