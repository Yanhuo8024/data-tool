#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
feedback_loop.py - Bad Case 自动归集 + A/B 测试 Prompt 版本 + 增强 NLQ 引擎

核心能力：
1. Bad Case 自动归集：用户反馈 → 自动记录 Prompt、SQL、错误信息
2. A/B 测试：灰度发布新 Prompt，对比 SQL 执行成功率
3. Prompt 版本管理：版本切换、回滚、效果对比
4. 增强 NLQ 引擎：集成 NLQEngine（Schema Linking + 置信度 + 澄清反问）
"""

import json
import time
import hashlib
import random
import sqlite3
import os
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from collections import Counter

# 导入 NLQEngine
try:
    from nlq_engine import NLQEngine, TimeParser, SchemaLinker, SQLValidator, ConfidenceEvaluator, ClarificationGenerator
    HAS_NLQ_ENGINE = True
except ImportError:
    HAS_NLQ_ENGINE = False


# ==================== Bad Case 自动归集 ====================

@dataclass
class BadCase:
    """Bad Case 记录"""
    id: str
    timestamp: float
    session_id: str
    user_query: str
    prompt_used: str
    sql_generated: str
    error_type: str          # syntax_error / schema_error / empty_result / user_feedback
    error_message: str
    feedback_type: str       # regenerate / inaccurate / other
    prompt_version: str      # 使用的 Prompt 版本
    metadata: Dict = field(default_factory=dict)


class BadCaseCollector:
    """
    Bad Case 自动归集器
    
    触发条件：
    - 用户点击"重新生成"
    - 用户点击"结果不准确"
    - SQL 执行报错
    - 返回空结果
    """
    
    def __init__(self, log_dir: str = "output/bad_cases"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.cases: List[BadCase] = []
        self._load()
    
    def _load(self):
        """加载历史 Bad Case"""
        log_file = self.log_dir / "bad_cases.jsonl"
        if log_file.exists():
            with open(log_file, 'r', encoding='utf-8') as f:
                for line in f:
                    try:
                        data = json.loads(line)
                        self.cases.append(BadCase(**data))
                    except Exception:
                        pass
    
    def _save(self, case: BadCase):
        """持久化 Bad Case"""
        log_file = self.log_dir / "bad_cases.jsonl"
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(case.__dict__, ensure_ascii=False) + '\n')
    
    def record(self, user_query: str, prompt: str, sql: str, 
               error_type: str, error_message: str = "",
               feedback_type: str = "auto", session_id: str = "default",
               prompt_version: str = "v1", metadata: Dict = None):
        """
        记录 Bad Case
        
        Args:
            user_query: 用户原始查询
            prompt: 使用的 Prompt
            sql: 生成的 SQL
            error_type: 错误类型
            error_message: 错误详情
            feedback_type: 触发类型（regenerate/inaccurate/auto）
            session_id: 会话 ID
            prompt_version: Prompt 版本
        """
        case = BadCase(
            id=hashlib.md5(f"{user_query}{time.time()}".encode()).hexdigest()[:12],
            timestamp=time.time(),
            session_id=session_id,
            user_query=user_query,
            prompt_used=prompt[:500],  # 截断过长 Prompt
            sql_generated=sql,
            error_type=error_type,
            error_message=error_message[:500],
            feedback_type=feedback_type,
            prompt_version=prompt_version,
            metadata=metadata or {}
        )
        
        self.cases.append(case)
        self._save(case)
        
        # 同时写入按版本分组的统计
        self._update_version_stats(prompt_version, error_type)
    
    def _update_version_stats(self, version: str, error_type: str):
        """更新版本统计"""
        stats_file = self.log_dir / "version_stats.json"
        
        if stats_file.exists():
            stats = json.load(open(stats_file, 'r', encoding='utf-8'))
        else:
            stats = {}
        
        # 兼容历史/异构格式：JSON 加载后 error_types 是普通 dict（非 Counter），
        # 且旧文件可能缺少 total/errors/success 键，直接 += 会抛 KeyError 导致查询整体失败
        slot = stats.setdefault(version, {})
        if not isinstance(slot, dict):
            slot = {}
            stats[version] = slot
        slot.setdefault("total", 0)
        slot.setdefault("errors", 0)
        slot.setdefault("success", 0)
        if not isinstance(slot.get("error_types"), dict):
            slot["error_types"] = {}

        slot["total"] += 1
        if error_type in ("syntax_error", "schema_error", "empty_result", "user_feedback"):
            slot["errors"] += 1
            slot["error_types"][error_type] = slot["error_types"].get(error_type, 0) + 1
        else:
            slot["success"] += 1

        json.dump(stats, open(stats_file, 'w', encoding='utf-8'), ensure_ascii=False, indent=2, default=dict)
    
    def get_recent_cases(self, n: int = 20, error_type: str = None) -> List[BadCase]:
        """获取最近的 Bad Case"""
        cases = self.cases
        if error_type:
            cases = [c for c in cases if c.error_type == error_type]
        return cases[-n:]
    
    def get_stats(self) -> Dict:
        """获取统计信息"""
        total = len(self.cases)
        error_types = Counter(c.error_type for c in self.cases)
        feedback_types = Counter(c.feedback_type for c in self.cases)
        versions = Counter(c.prompt_version for c in self.cases)
        
        return {
            "total_bad_cases": total,
            "error_types": dict(error_types),
            "feedback_types": dict(feedback_types),
            "by_version": dict(versions),
            "recent_24h": len([c for c in self.cases if time.time() - c.timestamp < 86400])
        }
    
    def get_version_stats(self) -> Dict:
        """获取各版本统计"""
        stats_file = self.log_dir / "version_stats.json"
        if stats_file.exists():
            return json.load(open(stats_file, 'r', encoding='utf-8'))
        return {}


# ==================== A/B 测试 Prompt 版本 ====================

@dataclass
class PromptVersion:
    """Prompt 版本"""
    version_id: str
    name: str
    prompt_template: str
    traffic_percent: float  # 流量占比 0-100
    is_active: bool = True
    created_at: float = 0.0
    total_queries: int = 0
    success_count: int = 0
    error_count: int = 0
    
    @property
    def success_rate(self) -> float:
        if self.total_queries == 0:
            return 0.0
        return self.success_count / self.total_queries


class PromptABTestManager:
    """
    Prompt A/B 测试管理器
    
    功能：
    - 多版本 Prompt 灰度发布
    - 流量分配（按百分比）
    - 成功率自动统计
    - 版本对比报告
    """
    
    def __init__(self, versions_file: str = "output/prompt_versions.json"):
        self.versions_file = Path(versions_file)
        self.versions_file.parent.mkdir(exist_ok=True)
        self.versions: Dict[str, PromptVersion] = {}
        self._load()
    
    def _load(self):
        """加载版本配置"""
        if self.versions_file.exists():
            data = json.load(open(self.versions_file, 'r', encoding='utf-8'))
            for vid, vinfo in data.items():
                self.versions[vid] = PromptVersion(**vinfo)
    
    def _save(self):
        """保存版本配置"""
        data = {vid: v.__dict__ for vid, v in self.versions.items()}
        json.dump(data, open(self.versions_file, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    
    def register_version(self, version_id: str, name: str, prompt_template: str, 
                         traffic_percent: float = 0) -> PromptVersion:
        """注册新版本"""
        v = PromptVersion(
            version_id=version_id,
            name=name,
            prompt_template=prompt_template,
            traffic_percent=traffic_percent,
            created_at=time.time()
        )
        self.versions[version_id] = v
        self._save()
        return v
    
    def set_traffic(self, version_id: str, percent: float):
        """设置流量占比"""
        if version_id in self.versions:
            self.versions[version_id].traffic_percent = min(max(percent, 0), 100)
            self._normalize_traffic()
            self._save()
    
    def _normalize_traffic(self):
        """归一化流量（总和=100）"""
        active = [v for v in self.versions.values() if v.is_active]
        if not active:
            return
        
        total = sum(v.traffic_percent for v in active)
        if total > 0:
            for v in active:
                v.traffic_percent = (v.traffic_percent / total) * 100
    
    def select_version(self, session_id: str = None) -> PromptVersion:
        """
        根据流量分配选择版本
        
        策略：
        - 如果提供了 session_id，使用哈希确保同一用户始终走同一版本
        - 否则按流量比例随机分配
        """
        active = [v for v in self.versions.values() if v.is_active]
        if not active:
            return None
        
        if session_id:
            # 一致性哈希：同一 session 始终走同一版本
            hash_val = int(hashlib.md5(session_id.encode()).hexdigest(), 16) % 100
            cumulative = 0
            for v in active:
                cumulative += v.traffic_percent
                if hash_val < cumulative:
                    return v
        
        # 随机分配
        # 注意：流量配置可能全为 0（新注册版本未设流量 / 全部暂停），
        # 此时 random.choices 会抛 "Total of weights must be greater than zero"
        # 导致整个 NLQ 查询失败。权重无效时退化为等概率选择。
        weights = [v.traffic_percent for v in active]
        if not weights or sum(weights) <= 0:
            weights = None
        return random.choices(active, weights=weights, k=1)[0]
    
    def record_result(self, version_id: str, success: bool):
        """记录执行结果"""
        if version_id in self.versions:
            v = self.versions[version_id]
            v.total_queries += 1
            if success:
                v.success_count += 1
            else:
                v.error_count += 1
            self._save()
    
    def get_comparison_report(self) -> Dict:
        """生成版本对比报告"""
        report = {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "versions": []
        }
        
        for v in sorted(self.versions.values(), key=lambda x: x.success_rate, reverse=True):
            report["versions"].append({
                "version_id": v.version_id,
                "name": v.name,
                "traffic_percent": round(v.traffic_percent, 1),
                "total_queries": v.total_queries,
                "success_count": v.success_count,
                "error_count": v.error_count,
                "success_rate": round(v.success_rate * 100, 2),
                "is_active": v.is_active,
                "created_at": time.strftime("%Y-%m-%d", time.localtime(v.created_at))
            })
        
        # 找出最佳版本
        active = [v for v in self.versions.values() if v.is_active and v.total_queries > 0]
        if active:
            best = max(active, key=lambda x: x.success_rate)
            report["best_version"] = best.version_id
            report["recommendation"] = f"建议将流量全部分配给 {best.name} ({best.success_rate*100:.1f}% 成功率)"
        
        return report
    
    def promote_version(self, version_id: str):
        """提升某版本为 100% 流量"""
        for v in self.versions.values():
            v.traffic_percent = 100 if v.version_id == version_id else 0
        self._save()
    
    def rollback(self, version_id: str):
        """回滚到指定版本"""
        if version_id in self.versions:
            self.promote_version(version_id)


# ==================== 整合引擎 ====================

class FeedbackDrivenNLQ:
    """
    反馈驱动的 NLQ 引擎（增强版）
    
    整合：
    - Bad Case 自动归集
    - A/B 测试 Prompt 版本
    - NLQEngine（Schema Linking + 置信度评估 + 澄清反问）
    - SQLite 数据库直连查询
    """
    
    def __init__(self, db_path: str = None, schema: Dict = None):
        self.db_path = db_path or "output/data.db"
        self.schema = schema or {}
        self.bad_case_collector = BadCaseCollector()
        self.ab_test_manager = PromptABTestManager()
        
        # 初始化 NLQEngine
        if HAS_NLQ_ENGINE:
            self.nlq_engine = NLQEngine(schema=self.schema)
        else:
            self.nlq_engine = None
        
        # 注册默认 Prompt 版本
        self._register_default_prompts()
    
    def _register_default_prompts(self):
        """注册默认 Prompt 版本"""
        if "v1" not in self.ab_test_manager.versions:
            self.ab_test_manager.register_version(
                "v1", "基础规则版", 
                "根据以下 Schema 生成 SQL:\n{schema}\n\n用户问题: {query}\n\nSQL:",
                traffic_percent=70
            )
        
        if "v2" not in self.ab_test_manager.versions:
            self.ab_test_manager.register_version(
                "v2", "增强版（含 Few-Shot）",
                "你是 SQL 专家。参考示例:\n{examples}\n\nSchema:\n{schema}\n\n用户问题: {query}\n\n<thinking>\n</thinking>\n<sql>\n</sql>",
                traffic_percent=30
            )
    
    def query(self, user_query: str, session_id: str = "default") -> Dict:
        """
        执行查询（含反馈闭环 + NLQEngine）
        """
        result = {
            "query": user_query,
            "session_id": session_id,
            "sql": "",
            "data": [],
            "columns": [],
            "prompt_version": None,
            "confidence": None,
            "clarification": None,
            "errors": []
        }
        
        # 选择 Prompt 版本
        version = self.ab_test_manager.select_version(session_id)
        if version:
            result["prompt_version"] = version.version_id
        
        # 使用 NLQEngine 生成 SQL
        if self.nlq_engine:
            try:
                nlq_result = self.nlq_engine.nlq_to_sql(user_query)
                sql = nlq_result.get("sql", "")
                validation = nlq_result.get("validation", {})
                
                if not validation.get("valid", False):
                    result["errors"].append(f"SQL 校验失败: {validation.get('error', '')}")
                
                result["sql"] = sql
                conf = nlq_result.get("confidence")
                result["confidence"] = conf if conf else {}
                
                # 低置信度时生成澄清问题
                if conf and conf.get("final", 1.0) < 0.6:
                    clarification = self.nlq_engine.clarification_gen.generate(
                        user_query, nlq_result.get("schema_linking", {})
                    )
                    result["clarification"] = clarification
                
            except Exception as e:
                result["errors"].append(f"NLQEngine 错误: {str(e)}")
                import traceback
                result["errors"].append(traceback.format_exc())
                # 降级到简化版
                result["sql"] = self._generate_sql_fallback(user_query)
        else:
            result["sql"] = self._generate_sql_fallback(user_query)
        
        # 执行 SQL
        success = self._execute_sql(result, user_query, session_id, version)
        
        # 记录 A/B 测试结果
        if version:
            self.ab_test_manager.record_result(version.version_id, success)
        
        return result
    
    def _execute_sql(self, result: Dict, user_query: str, session_id: str, version) -> bool:
        """执行 SQL 并处理结果"""
        sql = result["sql"]
        if not sql:
            result["errors"].append("未能生成 SQL")
            return False
        
        # 尝试连接 SQLite 执行
        db_path = Path(self.db_path)
        if db_path.exists():
            try:
                import sqlite3
                conn = sqlite3.connect(str(db_path))
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(sql)
                rows = cursor.fetchall()
                result["columns"] = [desc[0] for desc in cursor.description] if cursor.description else []
                result["data"] = [dict(zip(result["columns"], row)) for row in rows]
                conn.close()
            except Exception as e:
                result["errors"].append(str(e))
                
                # 自动记录 Bad Case
                self.bad_case_collector.record(
                    user_query=user_query,
                    prompt=version.prompt_template if version else "",
                    sql=sql,
                    error_type="syntax_error",
                    error_message=str(e),
                    feedback_type="auto",
                    session_id=session_id,
                    prompt_version=version.version_id if version else "unknown"
                )
                return False
        else:
            # 绝不返回模拟数据：数据库缺失是错误，不是可降级场景
            result["errors"].append(f"数据库文件不存在: {db_path}")
            return False
        
        # 空结果 → 记录 Bad Case
        if not result["data"] and not result["errors"]:
            self.bad_case_collector.record(
                user_query=user_query,
                prompt=version.prompt_template if version else "",
                sql=sql,
                error_type="empty_result",
                error_message="返回 0 条记录",
                feedback_type="auto",
                session_id=session_id,
                prompt_version=version.version_id if version else "unknown"
            )
        
        return True
    
    def _generate_sql_fallback(self, query: str) -> str:
        """降级 SQL 生成（简化版）"""
        time_cond = ""
        for expr in ["上个月", "本月", "上周", "去年", "今年"]:
            if expr in query:
                now = datetime.now()
                if expr == "上个月":
                    if now.month == 1:
                        time_cond = f"date >= '{now.year-1}-12-01' AND date <= '{now.year-1}-12-31'"
                    else:
                        time_cond = f"date >= '{now.year}-{now.month-1:02d}-01' AND date <= '{now.year}-{now.month:02d}-01'"
                break
        
        agg = "SUM(amount) AS total_sales"
        if "利润" in query:
            agg = "SUM(profit) AS total_profit"
        
        group_col = "province"
        if "城市" in query:
            group_col = "city"
        
        sql = f"SELECT {group_col}, {agg} FROM sales"
        if time_cond:
            sql += f" WHERE {time_cond}"
        sql += f" GROUP BY {group_col} ORDER BY total_sales DESC LIMIT 100"
        
        return sql
    
    def update_schema(self, schema: Dict):
        """更新 Schema（用于动态加载用户上传文件的表结构）"""
        self.schema = schema
        if self.nlq_engine:
            self.nlq_engine = NLQEngine(schema=schema)
    
    def set_db_path(self, db_path: str):
        """设置数据库路径"""
        self.db_path = db_path
    
    def report_feedback(self, session_id: str, feedback_type: str, query: str = "", sql: str = ""):
        """
        用户主动反馈
        
        Args:
            session_id: 会话 ID
            feedback_type: regenerate / inaccurate
            query: 原始查询
            sql: 生成的 SQL
        """
        self.bad_case_collector.record(
            user_query=query,
            prompt="",
            sql=sql,
            error_type="user_feedback",
            error_message=f"用户反馈: {feedback_type}",
            feedback_type=feedback_type,
            session_id=session_id
        )
    
    def get_bad_case_stats(self) -> Dict:
        """获取 Bad Case 统计"""
        return self.bad_case_collector.get_stats()
    
    def get_ab_test_report(self) -> Dict:
        """获取 A/B 测试报告"""
        return self.ab_test_manager.get_comparison_report()


# ==================== 使用示例 ====================

def demo():
    """反馈驱动 NLQ Demo"""
    engine = FeedbackDrivenNLQ()
    
    print("=" * 60)
    print(" 反馈驱动 NLQ 引擎 Demo")
    print("=" * 60)
    
    # 正常查询
    print("\n--- 正常查询 ---")
    r1 = engine.query("统计上个月各省份的销售额", session_id="user_001")
    print(f"Version: {r1['prompt_version']}, SQL: {r1['sql'][:60]}...")
    
    # 模拟用户反馈
    print("\n--- 用户反馈: 重新生成 ---")
    engine.report_feedback("user_001", "regenerate", 
                          "统计上个月各省份的销售额", r1['sql'])
    
    # 模拟执行错误
    print("\n--- 模拟执行错误 ---")
    r2 = engine.query("SELCT * FROM non_exist_table", session_id="user_002")
    print(f"Errors: {r2['errors']}")
    
    # 统计
    print("\n--- Bad Case 统计 ---")
    stats = engine.get_bad_case_stats()
    print(json.dumps(stats, indent=2, ensure_ascii=False))
    
    # A/B 测试报告
    print("\n--- A/B 测试报告 ---")
    report = engine.get_ab_test_report()
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    demo()
