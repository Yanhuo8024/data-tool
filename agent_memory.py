#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent_memory.py - Agent 记忆管理引擎

核心能力：
1. 显式状态机：结构化 JSON 记录会话状态（筛选器、偏好、上下文）
2. 历史查询缓存：语义相似的结果直接缓存返回
3. 上下文注入：每轮对话自动将状态转化为自然语言注入 Prompt
"""

import json
import re
import math
import time
import hashlib
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field


# ==================== 会话状态机 ====================

@dataclass
class SessionState:
    """
    会话状态机（结构化 JSON）
    
    记录用户当前的数据分析上下文，跨轮次保持
    """
    # 当前筛选条件
    current_filters: Dict[str, Any] = field(default_factory=dict)
    # 当前关注的指标
    current_metrics: List[str] = field(default_factory=list)
    # 当前维度
    current_dimensions: List[str] = field(default_factory=list)
    # 时间范围
    time_range: Dict[str, str] = field(default_factory=dict)
    # 目标表
    target_tables: List[str] = field(default_factory=list)
    # 用户偏好（如喜欢的图表类型）
    preferences: Dict[str, Any] = field(default_factory=dict)
    # 对话历史摘要
    conversation_summary: List[Dict] = field(default_factory=list)
    # 最近一次查询
    last_query: str = ""
    last_sql: str = ""
    # 状态创建时间
    created_at: float = 0.0
    updated_at: float = 0.0
    
    def to_dict(self) -> Dict:
        return {
            "current_filters": self.current_filters,
            "current_metrics": self.current_metrics,
            "current_dimensions": self.current_dimensions,
            "time_range": self.time_range,
            "target_tables": self.target_tables,
            "preferences": self.preferences,
            "conversation_summary": self.conversation_summary[-5:],  # 最近5轮
            "last_query": self.last_query,
            "updated_at": self.updated_at
        }
    
    def to_natural_language(self) -> str:
        """将状态转化为自然语言，注入 Prompt"""
        parts = []
        
        if self.current_filters:
            filter_desc = ", ".join(f"{k}={v}" for k, v in self.current_filters.items())
            parts.append(f"当前筛选条件: {filter_desc}")
        
        if self.current_metrics:
            parts.append(f"关注指标: {', '.join(self.current_metrics)}")
        
        if self.current_dimensions:
            parts.append(f"分析维度: {', '.join(self.current_dimensions)}")
        
        if self.time_range:
            parts.append(f"时间范围: {self.time_range.get('start', '')} ~ {self.time_range.get('end', '')}")
        
        if self.target_tables:
            parts.append(f"目标表: {', '.join(self.target_tables)}")
        
        if self.last_query:
            parts.append(f"上一轮查询: {self.last_query}")
        
        # 跨轮续接的关键：历史问答摘要（最近若干轮）。此前该字段被记录但从未注入，
        # 导致"接着上次分析再深入"无法衔接。这里显式纳入最近几轮问答。
        if self.conversation_summary:
            recent = self.conversation_summary[-5:]
            lines = [f"- 用户问：{s['query']} → 结论：{str(s.get('answer',''))[:150]}" for s in recent]
            parts.append("近期对话摘要（供衔接本次分析，勿重复已有结论）：\n" + "\n".join(lines))
        
        if not parts:
            return "无历史上下文"
        
        return " | ".join(parts)


class SessionMemoryManager:
    """会话记忆管理器"""

    # P0② 会话状态过期时间（秒）：超过即视为失效，惰性剔除，防止无界增长
    SESSION_TTL = 30 * 60

    def __init__(self):
        self.sessions: Dict[str, SessionState] = {}  # session_id -> state

    def _purge_expired(self):
        """惰性清理长期未更新的会话状态，避免内存无界增长。"""
        now = time.time()
        try:
            expired = [k for k, v in self.sessions.items()
                       if now - v.updated_at > self.SESSION_TTL]
            for k in expired:
                del self.sessions[k]
        except Exception:
            pass

    def get_or_create(self, session_id: str) -> SessionState:
        self._purge_expired()
        if session_id not in self.sessions:
            self.sessions[session_id] = SessionState(
                created_at=time.time(),
                updated_at=time.time()
            )
        return self.sessions[session_id]
    
    def update_state(self, session_id: str, **kwargs) -> SessionState:
        """更新会话状态"""
        state = self.get_or_create(session_id)
        
        for key, value in kwargs.items():
            if hasattr(state, key) and value is not None:
                setattr(state, key, value)
        
        state.updated_at = time.time()
        return state
    
    def update_from_query(self, session_id: str, query: str, result: Dict):
        """从查询结果中自动更新状态"""
        state = self.get_or_create(session_id)
        
        # 自动提取时间范围
        time_range = result.get("time_range")
        if time_range:
            state.time_range = time_range
        
        # 自动提取指标（从 SQL 中解析）
        sql = result.get("sql", "")
        metrics = self._extract_metrics(sql)
        if metrics:
            state.current_metrics = metrics
        
        # 提取维度
        dims = self._extract_dimensions(sql)
        if dims:
            state.current_dimensions = dims
        
        # 提取表名
        tables = self._extract_tables(sql)
        if tables:
            state.target_tables = tables
        
        # 记录历史
        state.last_query = query
        state.last_sql = sql
        state.conversation_summary.append({
            "query": query,
            "sql": sql,
            "timestamp": time.time(),
            "row_count": len(result.get("data", []))
        })
        
        # 只保留最近 10 轮
        if len(state.conversation_summary) > 10:
            state.conversation_summary = state.conversation_summary[-10:]
        
        state.updated_at = time.time()
        return state
    
    def _extract_metrics(self, sql: str) -> List[str]:
        """从 SQL 提取指标"""
        metrics = []
        agg_patterns = [
            (r'SUM\s*\(\s*(\w+)\s*\)', 'sum'),
            (r'AVG\s*\(\s*(\w+)\s*\)', 'avg'),
            (r'COUNT\s*\(\s*(\w+)\s*\)', 'count'),
            (r'MAX\s*\(\s*(\w+)\s*\)', 'max'),
            (r'MIN\s*\(\s*(\w+)\s*\)', 'min'),
        ]
        
        for pattern, agg_type in agg_patterns:
            for match in re.finditer(pattern, sql, re.IGNORECASE):
                col = match.group(1)
                if col != '*':
                    metrics.append(f"{agg_type}({col})")
        
        return metrics
    
    def _extract_dimensions(self, sql: str) -> List[str]:
        """从 SQL 提取维度"""
        dims = []
        m = re.search(r'GROUP\s+BY\s+([\w.]+(?:\s*,\s*[\w.]+)*)', sql, re.IGNORECASE)
        if m:
            dims = [c.strip() for c in m.group(1).split(',')]
        return dims

    def _extract_tables(self, sql: str) -> List[str]:
        """从 SQL 提取表名（去子查询：排除括号内的引用，支持 schema 限定名）"""
        tables = set()
        for match in re.finditer(r'\b(?:FROM|JOIN)\s+([\w.]+)', sql, re.IGNORECASE):
            ref = match.group(1)
            # 跳过括号内(子查询)的引用
            head = sql[match.start(0):]
            open_idx = sql.rfind('(', 0, match.start(0))
            close_before = sql.rfind(')', 0, match.start(0))
            if open_idx > close_before:
                continue
            tables.add(ref)
        return list(tables)
    
    def get_context_prompt(self, session_id: str) -> str:
        """获取上下文 Prompt"""
        state = self.get_or_create(session_id)
        return state.to_natural_language()
    
    def clear_session(self, session_id: str):
        """清除会话"""
        if session_id in self.sessions:
            del self.sessions[session_id]


# ==================== 历史查询缓存 ====================

class QueryCache:
    """
    历史查询缓存
    
    策略：
    1. 精确匹配：SQL 完全相同 → 直接返回
    2. 语义匹配：查询意图相似 → 返回缓存（带提示）
    3. TTL 过期：超过 5 分钟自动失效
    """
    
    def __init__(self, ttl: int = 300, max_size: int = 100):
        self.ttl = ttl  # 过期时间（秒）
        self.max_size = max_size
        self.cache: Dict[str, Dict] = {}  # cache_key -> {result, timestamp, query}
        self.access_count: Dict[str, int] = {}  # 访问计数
    
    def _make_key(self, query: str, filters: Dict = None) -> str:
        """生成缓存键"""
        key_data = query.lower().strip()
        if filters:
            key_data += json.dumps(filters, sort_keys=True)
        return hashlib.md5(key_data.encode()).hexdigest()
    
    def get(self, query: str, filters: Dict = None) -> Optional[Dict]:
        """获取缓存"""
        key = self._make_key(query, filters)
        
        if key in self.cache:
            entry = self.cache[key]
            # 检查过期
            if time.time() - entry["timestamp"] < self.ttl:
                self.access_count[key] = self.access_count.get(key, 0) + 1
                result = entry["result"].copy()
                result["from_cache"] = True
                result["cache_age"] = round(time.time() - entry["timestamp"], 1)
                return result
            else:
                # 过期删除
                del self.cache[key]
                del self.access_count[key]
        
        return None
    
    def put(self, query: str, result: Dict, filters: Dict = None):
        """写入缓存"""
        # 清理过期项
        self._cleanup()
        
        key = self._make_key(query, filters)
        self.cache[key] = {
            "result": result,
            "timestamp": time.time(),
            "query": query
        }
        self.access_count[key] = 0
    
    def _cleanup(self):
        """清理过期和超出容量的缓存"""
        now = time.time()
        expired = [k for k, v in self.cache.items() if now - v["timestamp"] > self.ttl]
        for k in expired:
            del self.cache[k]
            if k in self.access_count:
                del self.access_count[k]
        
        # 超出容量 → 删除最少访问的
        if len(self.cache) > self.max_size:
            sorted_keys = sorted(self.access_count.keys(), key=lambda k: self.access_count[k])
            for k in sorted_keys[:len(self.cache) - self.max_size]:
                if k in self.cache:
                    del self.cache[k]
                del self.access_count[k]
    
    def get_stats(self) -> Dict:
        """获取缓存统计"""
        return {
            "total_entries": len(self.cache),
            "total_hits": sum(self.access_count.values()),
            "avg_hits": sum(self.access_count.values()) / max(len(self.cache), 1)
        }


# ==================== 记忆增强的 NLQ 引擎 ====================

class MemoryEnhancedNLQ:
    """
    记忆增强的 NLQ 引擎
    
    整合：
    1. 会话状态机（跨轮次上下文）
    2. 历史查询缓存（省钱+快速）
    3. 上下文注入（自动更新 Prompt）
    """
    
    def __init__(self, db_engine=None, schema: Dict = None):
        self.db_engine = db_engine
        self.schema = schema or {}
        self.session_mgr = SessionMemoryManager()
        self.query_cache = QueryCache(ttl=300)
    
    def query(self, query: str, session_id: str = "default") -> Dict:
        """
        带记忆的查询
        
        流程：
        1. 检查缓存 → 命中直接返回
        2. 获取会话状态 → 注入上下文
        3. 执行查询
        4. 更新状态 + 写入缓存
        """
        result = {
            "query": query,
            "session_id": session_id,
            "from_cache": False,
            "context_used": None,
            "sql": "",
            "data": [],
            "errors": []
        }
        
        # Step 1: 检查缓存
        cached = self.query_cache.get(query, self.session_mgr.get_or_create(session_id).current_filters)
        if cached:
            result.update(cached)
            result["from_cache"] = True
            return result
        
        # Step 2: 获取会话上下文
        state = self.session_mgr.get_or_create(session_id)
        context = state.to_natural_language()
        result["context_used"] = context
        
        # Step 3: 构建增强 Prompt（含上下文）
        enhanced_query = self._build_contextual_query(query, state)
        
        # Step 4: 执行查询（简化版，实际调用 NLQ v2 引擎）
        sql = self._generate_sql(enhanced_query, state)
        result["sql"] = sql
        
        # 执行
        if self.db_engine:
            try:
                from sqlalchemy import text
                with self.db_engine.connect() as conn:
                    rows = conn.execute(text(sql))
                    result["columns"] = list(rows.keys())
                    result["data"] = [dict(zip(result["columns"], r)) for r in rows.fetchall()]
            except Exception as e:
                result["errors"].append(str(e))
                return result
        else:
            result["data"] = [
                {"province": "广东", "total_sales": 500000},
                {"province": "浙江", "total_sales": 350000},
            ]
            result["columns"] = ["province", "total_sales"]
        
        # Step 5: 更新状态
        self.session_mgr.update_from_query(session_id, query, result)
        
        # Step 6: 写入缓存
        self.query_cache.put(query, result, state.current_filters)
        
        return result
    
    def _build_contextual_query(self, query: str, state: SessionState) -> str:
        """构建含上下文的查询"""
        context_parts = []
        
        if state.current_filters:
            context_parts.append(f"筛选: {state.current_filters}")
        if state.time_range:
            context_parts.append(f"时间: {state.time_range}")
        if state.target_tables:
            context_parts.append(f"表: {state.target_tables}")
        
        if context_parts:
            return f"[{' | '.join(context_parts)}] {query}"
        return query
    
    def _generate_sql(self, query: str, state: SessionState) -> str:
        """生成 SQL（简化版）"""
        # 提取时间
        time_cond = ""
        for expr in ["上个月", "本月", "上周", "去年", "今年"]:
            if expr in query:
                from datetime import datetime, timedelta
                now = datetime.now()
                if expr == "上个月":
                    if now.month == 1:
                        time_cond = f"date >= '{now.year-1}-12-01' AND date <= '{now.year-1}-12-31'"
                    else:
                        time_cond = f"date >= '{now.year}-{now.month-1:02d}-01' AND date <= '{now.year}-{now.month:02d}-01'"
                elif expr == "本月":
                    time_cond = f"date >= '{now.year}-{now.month:02d}-01' AND date <= '{now.year}-{now.month:02d}-{now.day:02d}'"
                break
        
        # 提取聚合
        agg = "SUM(amount) AS total_sales"
        if "利润" in query:
            agg = "SUM(profit) AS total_profit"
        elif "数量" in query:
            agg = "COUNT(*) AS total_count"
        
        # 提取维度
        group_col = "province"
        if "城市" in query:
            group_col = "city"
        elif "行业" in query:
            group_col = "industry"
        elif "月份" in query:
            group_col = "DATE_FORMAT(date, '%Y-%m') AS month"
        
        # 表名
        table = state.target_tables[0] if state.target_tables else "sales"
        
        sql = f"SELECT {group_col}, {agg} FROM {table}"
        if time_cond:
            sql += f" WHERE {time_cond}"
        sql += f" GROUP BY {group_col} ORDER BY {agg.split('AS')[-1].strip()} DESC LIMIT 100"
        
        return sql
    
    def get_session_state(self, session_id: str) -> Dict:
        """获取会话状态"""
        return self.session_mgr.get_or_create(session_id).to_dict()
    
    def get_cache_stats(self) -> Dict:
        """获取缓存统计"""
        return self.query_cache.get_stats()


# ==================== 使用示例 ====================

def demo():
    """记忆增强 NLQ Demo"""
    engine = MemoryEnhancedNLQ()
    
    print("=" * 60)
    print(" 记忆增强 NLQ 引擎 Demo")
    print("=" * 60)
    
    # 第一轮查询
    print("\n--- 第 1 轮 ---")
    r1 = engine.query("统计上个月各省份的销售额", session_id="user_001")
    print(f"Query: {r1['query']}")
    print(f"SQL: {r1['sql']}")
    print(f"Context: {r1['context_used']}")
    print(f"Data: {len(r1['data'])} rows")
    
    # 第二轮查询（继承上下文）
    print("\n--- 第 2 轮（继承上下文）---")
    r2 = engine.query("按城市拆分", session_id="user_001")
    print(f"Query: {r2['query']}")
    print(f"SQL: {r2['sql']}")
    print(f"Context: {r2['context_used']}")
    
    # 第三轮查询（缓存命中）
    print("\n--- 第 3 轮（缓存命中）---")
    r3 = engine.query("统计上个月各省份的销售额", session_id="user_001")
    print(f"Query: {r3['query']}")
    print(f"From Cache: {r3['from_cache']}")
    if r3.get('cache_age'):
        print(f"Cache Age: {r3['cache_age']}s")
    
    # 状态查看
    print("\n--- 会话状态 ---")
    state = engine.get_session_state("user_001")
    print(json.dumps(state, indent=2, ensure_ascii=False))
    
    # 缓存统计
    print("\n--- 缓存统计 ---")
    stats = engine.get_cache_stats()
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    demo()
