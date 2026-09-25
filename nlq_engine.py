#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nlq_engine.py - 统一 Text-to-SQL 引擎

合并自: nlq_router.py + nlq_insight_engine.py + nlq_v2.py + nlq_clarifier.py
功能:
- 自然语言 → SQL 转换
- Schema Linking（智能选表）
- 数据标准化（品牌名/别名）
- SQL 校验（只读 + 安全）
- 置信度评估（多维度）
- 澄清反问（低置信度时）
- 业务洞察生成
- LLM 调用（OpenAI 兼容接口）

零费用: 纯规则引擎 + TF-IDF embedding
"""

import re
import os
import json
import math
import hashlib
import logging
import requests
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime, timedelta


# ==========================================================
# 数据结构
# ==========================================================

@dataclass
class BusinessInsight:
    """业务洞察"""
    summary: str = ""
    trend: str = ""
    anomalies: List[str] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)


@dataclass
class ConfidenceBreakdown:
    """置信度明细"""
    schema: float = 0.0
    sql: float = 0.0
    result: float = 0.0
    intent: float = 0.0
    final: float = 0.0


# ==========================================================
# 时间解析器
# ==========================================================

class TimeParser:
    """解析模糊时间表达式"""
    
    def __init__(self, current_date: datetime = None):
        self.now = current_date or datetime.now()
    
    def parse(self, expr: str) -> tuple:
        expr = expr.strip()
        year = self.now.year
        month = self.now.month
        
        if expr in ["今天", "今日"]:
            d = self.now.strftime("%Y-%m-%d")
            return (d, d)
        if expr in ["昨天", "昨日"]:
            return ((self.now - timedelta(days=1)).strftime("%Y-%m-%d"),) * 2
        
        m = re.search(r"最近\s*(\d+)\s*天", expr)
        if m:
            n = int(m.group(1))
            return ((self.now - timedelta(days=n-1)).strftime("%Y-%m-%d"), self.now.strftime("%Y-%m-%d"))
        
        if expr in ["本周", "这周"]:
            return ((self.now - timedelta(days=self.now.weekday())).strftime("%Y-%m-%d"), self.now.strftime("%Y-%m-%d"))
        if expr in ["上周", "上个星期"]:
            lm = self.now - timedelta(days=self.now.weekday() + 7)
            return (lm.strftime("%Y-%m-%d"), (lm + timedelta(days=6)).strftime("%Y-%m-%d"))
        if expr in ["本月", "这个月"]:
            return (f"{year}-{month:02d}-01", self.now.strftime("%Y-%m-%d"))
        if expr in ["上个月", "上月"]:
            if month == 1:
                return (f"{year-1}-12-01", f"{year-1}-12-31")
            return (f"{year}-{month-1:02d}-01", (datetime(year, month, 1) - timedelta(days=1)).strftime("%Y-%m-%d"))
        if expr in ["去年"]:
            return (f"{year-1}-01-01", f"{year-1}-12-31")
        if expr in ["今年"]:
            return (f"{year}-01-01", self.now.strftime("%Y-%m-%d"))
        
        m = re.search(r"(\d{4}-\d{2}-\d{2})", expr)
        if m:
            return (m.group(1), m.group(1))
        
        return (f"{year}-{month:02d}-01", self.now.strftime("%Y-%m-%d"))


# ==========================================================
# Schema Linking（智能选表 + 同义词库）
# ==========================================================

SYNONYM_DICT = {
    "销量": ["amount", "quantity", "sales_volume", "order_count"],
    "卖得": ["amount", "quantity", "sales"],
    "成交额": ["amount", "revenue", "turnover", "gmv"],
    "销售额": ["amount", "sales_amount", "revenue", "total_sales"],
    "收入": ["revenue", "income", "earnings"],
    "订单量": ["order_count", "order_num", "orders"],
    "客单价": ["avg_amount", "avg_order_value", "aov"],
    "利润": ["profit", "margin", "gross_profit"],
    "毛利": ["gross_profit", "gross_margin"],
    "净利润": ["net_profit", "net_margin"],
    "成本": ["cost", "expense", "spend"],
    "GMV": ["gmv", "total_gross_merchandise_value", "amount"],
    "ROI": ["roi", "return_on_investment"],
    "用户": ["user", "customer", "client", "member", "user_id"],
    "新客": ["new_user", "new_customer", "new_user_flag"],
    "复购": ["repurchase", "repeat_purchase"],
    "留存": ["retention", "retention_rate"],
    "转化": ["conversion", "conversion_rate", "cvr"],
    "流失": ["churn", "inactive"],
    "DAU": ["dau", "daily_active_user"],
    "MAU": ["mau", "monthly_active_user"],
    "产品": ["product", "goods", "item", "sku", "product_name"],
    "商品": ["product", "goods", "item"],
    "品类": ["category", "cat", "type"],
    "品牌": ["brand", "brand_name"],
    "库存": ["stock", "inventory"],
    "省份": ["province", "prov", "region"],
    "城市": ["city", "metro"],
    "地区": ["region", "area", "district"],
    "渠道": ["channel", "source"],
    "广告": ["ad", "campaign", "promotion"],
    "活动": ["activity", "campaign", "event"],
    "退款": ["refund", "return"],
    "增长率": ["growth_rate", "increase_rate", "growth"],
    "同比": ["yoy", "year_on_year"],
    "环比": ["mom", "month_on_month"],
    "营收": ["revenue", "total_revenue"],
}

BRAND_SYNONYMS = {
    "apple": "Apple", "苹果": "Apple", "iphone": "iPhone",
    "huawei": "华为", "华为": "华为", "xiaomi": "小米", "小米": "小米",
    "samsung": "三星", "三星": "三星", "byd": "比亚迪", "比亚迪": "比亚迪",
    "tesla": "特斯拉", "特斯拉": "特斯拉", "catl": "宁德时代", "宁德时代": "宁德时代",
}


class SchemaLinker:
    """智能选表 + 字段映射"""
    
    def __init__(self, schema: Dict = None):
        self.schema = schema or {}
        self.table_index = self._build_index()
    
    def _build_index(self) -> Dict:
        index = {"tables": {}, "columns": {}, "keywords": {}}
        
        for table in self.schema.get("tables", []):
            tname = table["name"]
            index["tables"][tname] = table
            
            for col in table.get("columns", []):
                cname = col["name"]
                index["columns"].setdefault(cname, []).append((tname, col))
                
                for kw in self._split_keywords(cname):
                    index["keywords"].setdefault(kw, set()).add((tname, cname))
        
        return index
    
    @staticmethod
    def _split_keywords(text: str) -> List[str]:
        parts = re.split(r'[_\-]', text.lower())
        camel = re.sub(r'([A-Z])', r' \1', text).lower().split()
        return list(set(parts + camel) - {''})
    
    def link(self, query: str, top_k: int = 5) -> Dict:
        query_lower = query.lower()
        table_scores = {}
        
        # 关键词匹配
        for kw, refs in self.table_index["keywords"].items():
            if kw in query_lower:
                for tname, cname in refs:
                    table_scores[tname] = table_scores.get(tname, 0) + 2.0
        
        # 同义词匹配（高分）
        for term, tech_terms in SYNONYM_DICT.items():
            if term in query:
                for tt in tech_terms:
                    for col_name, refs in self.table_index["columns"].items():
                        if tt in col_name:
                            for tname, col in refs:
                                table_scores[tname] = table_scores.get(tname, 0) + 5.0
        
        # 品牌匹配
        for brand, standard in BRAND_SYNONYMS.items():
            if brand in query.lower():
                table_scores["sales"] = table_scores.get("sales", 0) + 3.0
        
        sorted_tables = sorted(table_scores.items(), key=lambda x: x[1], reverse=True)
        
        result_tables = []
        for tname, score in sorted_tables[:top_k]:
            table_info = self.table_index["tables"].get(tname, {})
            result_tables.append({
                "name": tname,
                "relevance_score": round(score, 2),
                "columns": [c["name"] for c in table_info.get("columns", [])][:8],
            })
        
        return {
            "tables": result_tables,
            "covered_metrics": [m for m in SYNONYM_DICT if m in query],
        }


# ==========================================================
# SQL 校验器
# ==========================================================

class SQLValidator:
    """SQL 安全校验（只读）"""
    
    WRITE_KEYWORDS = ["DROP", "DELETE", "UPDATE", "INSERT", "ALTER", "TRUNCATE", "CREATE",
                      "EXEC", "GRANT", "REVOKE", "INTO", "LOAD_FILE", "OUTFILE"]
    
    @classmethod
    def validate(cls, sql: str) -> tuple:
        if not sql or not sql.strip():
            return False, "SQL 不能为空"
        
        if not re.match(r"^\s*SELECT\s+", sql, re.IGNORECASE):
            return False, "只允许 SELECT 查询"
        
        for kw in cls.WRITE_KEYWORDS:
            if re.search(rf'\b{kw}\b', sql, re.IGNORECASE):
                return False, f"禁止关键字: {kw}"
        
        if re.search(r";\s*(DROP|DELETE|UPDATE|INSERT)", sql, re.IGNORECASE):
            return False, "多语句注入"
        
        return True, "安全"
    
    @classmethod
    def auto_fix(cls, sql: str) -> str:
        """自动修正常见错误"""
        fixes = {
            r'\bSELET\b': 'SELECT', r'\bSELCT\b': 'SELECT', r'\bFORM\b': 'FROM',
            r'\bWERE\b': 'WHERE', r'\bWHRE\b': 'WHERE', r'\bGROUYP\b': 'GROUP',
        }
        for pattern, repl in fixes.items():
            sql = re.sub(pattern, repl, sql, flags=re.IGNORECASE)
        
        # 补全括号, LIMIT
        if sql.count("(") > sql.count(")"):
            sql += ")"
        if not re.search(r'\bLIMIT\s+\d+', sql, re.IGNORECASE):
            sql = sql.rstrip(';').rstrip() + " LIMIT 100"
        
        return sql


# ==========================================================
# 置信度评估器
# ==========================================================

class ConfidenceEvaluator:
    """多维度置信度评估"""
    
    def evaluate(self, query: str, schema_linking: Dict, sql: str, 
                 validation: Dict, data: List) -> ConfidenceBreakdown:
        bd = ConfidenceBreakdown()
        
        # Schema 匹配
        tables = schema_linking.get("tables", [])
        bd.schema = min(0.5 + len(tables) * 0.15, 1.0) if tables else 0.0
        
        # SQL 质量
        bd.sql = 0.9 if validation and validation.get("valid") else 0.2
        
        # 结果合理性
        bd.result = 0.8 if data else 0.1
        
        # 意图明确度
        clear_kw = ["统计", "查询", "销售额", "利润", "用户"]
        vague_kw = ["那个", "这个", "东西", "情况"]
        bd.intent = 0.7 if any(k in query for k in clear_kw) else 0.3
        bd.intent -= sum(0.05 for k in vague_kw if k in query)
        
        # 加权
        bd.final = 0.35 * bd.schema + 0.25 * bd.sql + 0.25 * bd.result + 0.15 * bd.intent
        bd.intent = max(bd.intent, 0)
        
        return bd


# ==========================================================
# 澄清反问生成器
# ==========================================================

class ClarificationGenerator:
    """低置信度时生成澄清问题"""
    
    def generate(self, query: str, confidence: Any) -> Dict:
        # 兼容 dict 和 ConfidenceBreakdown 对象
        if isinstance(confidence, dict):
            schema_score = confidence.get("schema", 0)
            intent_score = confidence.get("intent", 0)
        else:
            schema_score = confidence.schema
            intent_score = confidence.intent
        
        if schema_score < 0.3:
            return {
                "question": "您想查看哪类数据？",
                "options": ["销售数据", "用户数据", "产品数据", "财务报表"]
            }
        if intent_score < 0.3:
            return {
                "question": "您想查看哪个指标？",
                "options": ["销售额", "利润", "订单量", "用户数"]
            }
        return {
            "question": "您能提供更具体的查询条件吗？",
            "options": ["按时间", "按地区", "按产品", "按用户"]
        }


# ==========================================================
# 统一 NLQ 引擎
# ==========================================================

class NLQEngine:
    """
    统一 Text-to-SQL 引擎
    
    功能: 自然语言 → SQL → 执行 → 洞察
    """
    
    def __init__(self, db_engine=None, schema: Dict = None, 
                 llm_config: Dict = None, threshold: float = 0.6):
        self.db_engine = db_engine
        self.schema = schema or {}
        self.threshold = threshold
        
        self.schema_linker = SchemaLinker(schema)
        self.time_parser = TimeParser()
        self.sql_validator = SQLValidator()
        self.confidence_evaluator = ConfidenceEvaluator()
        self.clarification_gen = ClarificationGenerator()
        
        self.llm_config = llm_config or {}
        self.llm_config.setdefault("temperature", 0.1)
        self.llm_config.setdefault("model", "gpt-4")
    
    def nlq_to_sql(self, query: str) -> Dict:
        """自然语言 → SQL（增强版：支持置信度评估）"""
        schema_linking = self.schema_linker.link(query)
        sql = self._rule_generate_sql(query, schema_linking)
        sql = self.sql_validator.auto_fix(sql)
        valid, reason = self.sql_validator.validate(sql)
        
        # 置信度评估（仅基于 SQL 和 schema，无执行数据）
        confidence = self.confidence_evaluator.evaluate(
            query=query, 
            schema_linking=schema_linking, 
            sql=sql, 
            validation={"valid": valid, "error": reason}, 
            data=[]  # 暂无执行结果
        )
        
        return {
            "sql": sql,
            "schema_linking": schema_linking,
            "validation": {"valid": valid, "error": reason},
            "confidence": {"final": confidence.final, "schema": confidence.schema, "sql": confidence.sql, "intent": confidence.intent}
        }
    
    def _rule_generate_sql(self, query: str, schema_linking: Dict) -> str:
        """
        规则引擎 生成 SQL（增强版 - 支持更多场景）
        """
        tables = schema_linking.get("tables", [])
        table = tables[0]["name"] if tables else "sales"
        
        # ========== 1. 检测 TopN / 排序 ==========
        top_n = 100  # 默认
        order_dir = "DESC"
        
        # TopN 检测：前N名/前N个/TopN/最高的N个/N大
        top_match = re.search(r'(?:前|top|topn|最高的|最大的|排前)\s*(\d+)\s*(?:名|个|条|位|大)?', query, re.IGNORECASE)
        if top_match:
            top_n = int(top_match.group(1))
        
        # TopN 英文
        if not top_match:
            top_match = re.search(r'top\s*(\d+)', query, re.IGNORECASE)
            if top_match:
                top_n = int(top_match.group(1))
        
        # 降序/升序
        if re.search(r'(最小|最少|最低|升序|asc)', query, re.IGNORECASE):
            order_dir = "ASC"
        
        # 排序字段检测
        order_col = None
        if re.search(r'按.*排', query) or re.search(r'排序', query):
            # 提取"按XX排序"
            order_match = re.search(r'按\s*(.+?)\s*(?:排序|排)', query)
            if order_match:
                order_col = order_match.group(1).strip()
        
        # ========== 2. 聚合字段选择 ==========
        agg = "SUM(amount) AS total_sales"
        
        # 占比/百分比优先检查（避免被其他规则抢先）
        if re.search(r'占比|百分比|比例|贡献', query):
            # 判断是金额占比还是数量占比
            if re.search(r'订单|数量|笔数|用户|客户', query):
                agg = "ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM sales), 2) AS pct"
            else:
                agg = "ROUND(SUM(amount) * 100.0 / (SELECT SUM(amount) FROM sales), 2) AS pct"
        elif "利润" in query or "盈利" in query:
            agg = "SUM(profit) AS total_profit"
        elif "数量" in query or "订单" in query or "笔数" in query:
            agg = "COUNT(*) AS total_count"
        elif "用户" in query or "客户" in query or "人数" in query:
            agg = "COUNT(DISTINCT user_id) AS total_users"
        elif "均价" in query or "平均价格" in query or "客单价" in query:
            agg = "ROUND(AVG(price), 2) AS avg_price"
        elif re.search(r'平均|均值', query):
            agg = "ROUND(AVG(amount), 2) AS avg_val"
        elif "最大" in query or "最高" in query:
            agg = "MAX(amount) AS max_val"
        elif "最小" in query or "最低" in query:
            agg = "MIN(amount) AS min_val"
        
        # ========== 3. 维度选择 ==========
        group = "province"
        if "城市" in query or "市" in query:
            group = "city"
        elif "行业" in query or "品类" in query or "类目" in query:
            group = "industry"
        elif "月份" in query or "月度" in query:
            group = "strftime('%Y-%m', date) AS month"
        elif "季度" in query:
            group = "CASE WHEN CAST(strftime('%m', date) AS INTEGER) BETWEEN 1 AND 3 THEN 'Q1' WHEN CAST(strftime('%m', date) AS INTEGER) BETWEEN 4 AND 6 THEN 'Q2' WHEN CAST(strftime('%m', date) AS INTEGER) BETWEEN 7 AND 9 THEN 'Q3' ELSE 'Q4' END AS quarter"
        elif "年份" in query or "年度" in query:
            group = "strftime('%Y', date) AS year"
        elif "周" in query:
            group = "strftime('%Y-%W', date) AS week"
        elif "渠道" in query or "来源" in query or "平台" in query:
            group = "channel"
        elif "品牌" in query:
            group = "brand"
        elif "每天" in query or "每日" in query:
            group = "date"
        
        # ========== 4. 时间条件 ==========
        where_parts = []
        time_exprs = ["今天", "昨天", "本周", "上周", "本月", "上个月", "今年", "去年", "最近"]
        for expr in time_exprs:
            if expr in query:
                try:
                    start, end = self.time_parser.parse(expr)
                    where_parts.append(f"date >= '{start}' AND date <= '{end}'")
                    break
                except Exception:
                    # 时间表达式解析失败时尝试下一个候选
                    pass
        
        # 具体日期范围
        date_range = re.search(r'(\d{4}[-/]\d{1,2}[-/]\d{1,2})\s*(?:到|至|-|~)\s*(\d{4}[-/]\d{1,2}[-/]\d{1,2})', query)
        if date_range:
            start_d = date_range.group(1).replace('/', '-')
            end_d = date_range.group(2).replace('/', '-')
            where_parts.append(f"date >= '{start_d}' AND date <= '{end_d}'")
        
        # ========== 5. 过滤条件 ==========
        # 数值条件：>XX、<XX、>=XX、<=XX
        # 使用列名白名单，避免匹配中文描述
        valid_cols = {"amount", "price", "profit", "quantity", "user_id", "id", "age", "score", "count", "num"}
        # SchemaLinker.link 返回的 columns 为字符串列表（旧版为 dict 列表），两者兼容
        for t in tables:
            for c in t.get("columns", []):
                valid_cols.add(c.get("name", "") if isinstance(c, dict) else str(c))
        
        num_filter = re.search(r'(\w+?)\s*(?:>|大于|超过|高于)\s*(\d+(?:\.\d+)?)', query)
        if num_filter:
            col = num_filter.group(1)
            val = num_filter.group(2)
            if col in valid_cols:
                where_parts.append(f"{col} > {val}")
        
        num_filter2 = re.search(r'(\w+?)\s*(?:<|小于|低于|不足)\s*(\d+(?:\.\d+)?)', query)
        if num_filter2:
            col = num_filter2.group(1)
            val = num_filter2.group(2)
            if col in valid_cols:
                where_parts.append(f"{col} < {val}")
        
        # 相等条件
        eq_match = re.search(r'(\w+)\s*(?:=|是|为)\s*(?:"|\')?(.+?)(?:"|\')?$', query)
        if eq_match and "select" not in query.lower():
            col = eq_match.group(1)
            val = eq_match.group(2).strip()
            if col in ["province", "city", "industry", "category", "channel", "brand"]:
                where_parts.append(f"{col} = '{val}'")
        
        # IN 条件：XX是A、B、C / A和B / A、B和C
        in_match = re.search(r'(\w+)\s*(?:=|是)\s*(.+?)(?:和|、|或)\s*(.+?)$', query)
        if in_match:
            col = in_match.group(1)
            if col in ["province", "city", "industry", "category", "channel", "brand"]:
                vals = [in_match.group(2).strip(), in_match.group(3).strip()]
                in_vals = ", ".join([f"'{v}'" for v in vals])
                where_parts.append(f"{col} IN ({in_vals})")
        
        # 对比条件：XX vs YY / XX 和 YY 对比/比较
        vs_match = re.search(r'(北京|上海|广州|深圳|浙江|江苏|山东|河南|四川|湖北|湖南|河北|福建|安徽|辽宁|陕西|江西|重庆|天津|广西|云南|贵州|山西|内蒙古|黑龙江|吉林|甘肃|海南|宁夏|青海|新疆|西藏|湖北|广东|吉林)\s*(?:vs|和|与)\s*(北京|上海|广州|深圳|浙江|江苏|山东|河南|四川|湖北|湖南|河北|福建|安徽|辽宁|陕西|江西|重庆|天津|广西|云南|贵州|山西|内蒙古|黑龙江|吉林|甘肃|海南|宁夏|青海|新疆|西藏|湖北|广东|吉林)\s*(?:对比|比较|的?区别)?', query, re.IGNORECASE)
        if vs_match:
            col1 = vs_match.group(1).strip()
            col2 = vs_match.group(2).strip()
            where_parts.append(f"province = '{col1}' OR province = '{col2}'")
        
        # 相等条件
        eq_match = re.search(r'(\w+)\s*(?:=|是|为)\s*(?:"|\')?(.+?)(?:"|\')?$', query)
        if eq_match and "select" not in query.lower():
            col = eq_match.group(1)
            val = eq_match.group(2).strip()
            if col in ["province", "city", "industry", "category", "channel", "brand"]:
                where_parts.append(f"{col} = '{val}'")
        
        # IN 条件：XX是A、B、C
        in_match = re.search(r'(\w+)\s*(?:=|是)\s*(.+?)(?:和|、|或)\s*(.+?)$', query)
        if in_match:
            col = in_match.group(1)
            if col in ["province", "city", "industry", "category", "channel", "brand"]:
                vals = [in_match.group(2).strip(), in_match.group(3).strip()]
                in_vals = ", ".join([f"'{v}'" for v in vals])
                where_parts.append(f"{col} IN ({in_vals})")
        
        # ========== 6. 组装 SQL ==========
        where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
        
        # 排序
        order_by = f"ORDER BY {order_col} {order_dir}" if order_col else f"ORDER BY total_sales {order_dir}"
        # 如果排序字段在select里，直接用
        if order_col and order_col not in group and order_col not in agg:
            order_by = f"ORDER BY total_sales {order_dir}"  # fallback
        
        sql = f"SELECT {group}, {agg} FROM {table} {where} GROUP BY {group} {order_by} LIMIT {top_n}"
        
        return sql.strip()
    
    def execute_sql(self, sql: str) -> Dict:
        """执行 SQL"""
        valid, reason = self.sql_validator.validate(sql)
        if not valid:
            return {"ok": False, "error": reason}
        
        if self.db_engine:
            try:
                from sqlalchemy import text
                with self.db_engine.connect() as conn:
                    rows = conn.execute(text(sql))
                    columns = list(rows.keys())
                    data = [dict(zip(columns, r)) for r in rows.fetchall()]
                    return {"ok": True, "data": data, "columns": columns}
            except Exception as e:
                return {"ok": False, "error": str(e)}
        
        # Mock
        return {"ok": True, "data": [
            {"province": "广东", "total_sales": 500000},
            {"province": "浙江", "total_sales": 350000},
        ], "columns": ["province", "total_sales"]}
    
    def generate_insight(self, data: List, columns: List[str]) -> BusinessInsight:
        """生成业务洞察"""
        insight = BusinessInsight()
        if not data:
            insight.summary = "查询结果为空"
            return insight
        
        # 数值分析
        for col in columns:
            try:
                vals = [float(r[col]) for r in data if r.get(col) is not None]
                if not vals:
                    continue
                
                total, avg = sum(vals), sum(vals)/len(vals)
                mx, mn = max(vals), min(vals)
                insight.summary = f"共 {len(data)} 条，{col} 合计 {total:,.0f}，均值 {avg:,.0f}"
                
                # 异常检测
                if len(vals) >= 3:
                    std = math.sqrt(sum((v-avg)**2 for v in vals)/len(vals))
                    if std > 0:
                        for i, v in enumerate(vals):
                            if abs(v-avg) > 2*std:
                                insight.anomalies.append(f"第{i+1}行 {col} 偏高/低")
                
                break
            except (ValueError, TypeError):
                continue
        
        if not insight.summary:
            insight.summary = f"共 {len(data)} 条记录"
        
        return insight
    
    def query(self, query: str) -> Dict:
        """完整查询流程: NLQ → SQL → 执行 → 评估 → 澄清/返回"""
        result = {"query": query, "status": "success"}
        
        # NLQ → SQL
        nlq_result = self.nlq_to_sql(query)
        result["sql"] = nlq_result["sql"]
        result["schema_linking"] = nlq_result["schema_linking"]
        
        # 执行
        exec_result = self.execute_sql(nlq_result["sql"])
        result["data"] = exec_result.get("data", [])
        result["columns"] = exec_result.get("columns", [])
        
        if not exec_result.get("ok"):
            result["status"] = "error"
            result["error"] = exec_result["error"]
            return result
        
        # 置信度评估
        confidence = self.confidence_evaluator.evaluate(
            query, nlq_result["schema_linking"],
            nlq_result["sql"], nlq_result["validation"], result["data"]
        )
        result["confidence"] = {
            "schema": round(confidence.schema, 3),
            "sql": round(confidence.sql, 3),
            "result": round(confidence.result, 3),
            "intent": round(confidence.intent, 3),
            "final": round(confidence.final, 3),
        }
        
        # 低置信度 → 澄清
        if confidence.final < self.threshold:
            result["status"] = "clarify"
            result["clarification"] = self.clarification_gen.generate(query, confidence)
            return result
        
        # 洞察
        result["insight"] = self.generate_insight(result["data"], result["columns"])
        
        return result
    
    def clarify_and_retry(self, original_query: str, selected_option: str) -> Dict:
        """用户选择澄清选项后重试"""
        enhanced = f"{original_query}（{selected_option}）"
        return self.query(enhanced)


# ==============================================
# 全局实例
# ==============================================

nlq_engine = NLQEngine(schema={
    "tables": [
        {"name": "sales", "columns": [
            {"name": "id", "type": "INT"},
            {"name": "date", "type": "DATE"},
            {"name": "province", "type": "VARCHAR"},
            {"name": "city", "type": "VARCHAR"},
            {"name": "amount", "type": "DECIMAL"},
            {"name": "profit", "type": "DECIMAL"},
            {"name": "quantity", "type": "INT"},
            {"name": "user_id", "type": "INT"},
            {"name": "product_id", "type": "INT"},
            {"name": "channel", "type": "VARCHAR"},
        ]},
        {"name": "users", "columns": [
            {"name": "id", "type": "INT"},
            {"name": "user_name", "type": "VARCHAR"},
            {"name": "total_spend", "type": "DECIMAL"},
            {"name": "login_days", "type": "INT"},
        ]},
    ]
})


# ==============================================
# 使用示例
# ==============================================

if __name__ == "__main__":
    engine = NLQEngine(schema=nlq_engine.schema)
    
    # 测试查询
    queries = [
        "统计上个月各省份的销售额",
        "查询广东省的订单量",
        "那个红色的数据",  # 应触发澄清
    ]
    
    for q in queries:
        print(f"\n{'='*50}")
        print(f"Query: {q}")
        print(f"{'='*50}")
        
        result = engine.query(q)
        print(f"Status: {result['status']}")
        print(f"Confidence: {result.get('confidence', {}).get('final', 0):.1%}")
        
        if result["status"] == "clarify":
            print(f"Clarification: {result['clarification']['question']}")
            print(f"Options: {result['clarification']['options']}")
        else:
            print(f"SQL: {result['sql'][:60]}...")
            print(f"Data: {len(result['data'])} rows")
            if result.get("insight"):
                print(f"Insight: {result['insight'].summary}")
