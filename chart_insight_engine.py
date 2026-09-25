#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
chart_insight_engine.py - 智能图表推荐 + 自动洞察提炼引擎

核心能力：
1. 智能图表推荐：自动分析数据结构，匹配最佳图表类型
2. 自动洞察提炼：基于规则引擎生成一句话业务总结
3. 业务语义理解：识别关键指标、维度、异常、趋势
4. 自然语言生成：将数据转化为人类可读的业务结论
"""

import re
import math
from typing import Dict, List, Optional, Any
from collections import Counter
from datetime import datetime


# ==========================================================
# 图表类型定义
# ==========================================================

CHART_TYPES = {
    "line": {"name": "折线图", "min_series": 1, "max_series": 5, "needs_time": True, "id": "line"},
    "bar": {"name": "柱状图", "min_series": 1, "max_series": 10, "needs_time": False, "id": "bar"},
    "pie": {"name": "饼图", "min_series": 1, "max_series": 8, "needs_time": False, "id": "pie", "needs_ratio": True},
    "scatter": {"name": "散点图", "min_series": 2, "max_series": 2, "needs_time": False, "id": "scatter"},
    "area": {"name": "面积图", "min_series": 1, "max_series": 3, "needs_time": True, "id": "area"},
    "heatmap": {"name": "热力图", "min_series": 1, "max_series": 1, "needs_time": False, "id": "heatmap", "needs_2d": True},
    "table": {"name": "数据表", "min_series": 1, "max_series": 100, "needs_time": False, "id": "table"},
    "kline": {"name": "K线图", "min_series": 4, "max_series": 4, "needs_time": True, "id": "kline", "needs_ohlc": True},
}


# ==========================================================
# 时间维度识别
# ==========================================================

TIME_PATTERNS = [
    r'\d{4}-\d{2}-\d{2}',           # 2024-01-15
    r'\d{4}/\d{2}/\d{2}',           # 2024/01/15
    r'\d{4}年\d{1,2}月\d{1,2}日',   # 2024年1月15日
    r'\d{4}-\d{2}',                 # 2024-01
    r'\d{4}年\d{1,2}月',            # 2024年1月
    r'\d{4}年',                     # 2024年
    r'\d{1,2}月',                    # 1月
    r'Q[1-4]',                       # Q1-Q4
    r'\d{4}-Q[1-4]',                 # 2024-Q1
    r'周一|周二|周三|周四|周五|周六|周日',
]

TIME_KEYWORDS = ["date", "time", "day", "month", "year", "dt", "日期", "时间", "月份", "年份", "年月"]


def is_time_column(col_name: str) -> bool:
    """判断列是否为时间维度"""
    col_lower = col_name.lower()
    return any(kw in col_lower for kw in TIME_KEYWORDS)


def is_time_value(value: str) -> bool:
    """判断值是否为时间格式"""
    for pattern in TIME_PATTERNS:
        if re.search(pattern, str(value)):
            return True
    return False


# ==========================================================
# 指标类型识别
# ==========================================================

METRIC_KEYWORDS = {
    "amount": "金额", "revenue": "收入", "sales": "销售额", "gmv": "GMV",
    "profit": "利润", "margin": "毛利", "cost": "成本", "expense": "费用",
    "count": "数量", "quantity": "数量", "cnt": "数量", "num": "数量",
    "users": "用户数", "user_count": "用户数", "dau": "DAU", "mau": "MAU",
    "rate": "比率", "ratio": "比率", "percentage": "占比", "pct": "占比",
}

DIMENSION_KEYWORDS = {
    "province": "省份", "city": "城市", "region": "地区",
    "industry": "行业", "category": "品类", "brand": "品牌",
    "channel": "渠道", "product": "产品", "type": "类型",
    "gender": "性别", "age_group": "年龄段", "level": "等级",
}


def classify_column(col_name: str) -> str:
    """分类列：metric/dimension/time"""
    col_lower = col_name.lower()
    
    # 时间列
    if is_time_column(col_name):
        return "time"
    
    # 指标列
    for kw in METRIC_KEYWORDS:
        if kw in col_lower:
            return "metric"
    
    # 维度列
    for kw in DIMENSION_KEYWORDS:
        if kw in col_lower:
            return "dimension"
    
    # 数值列 → 指标
    # 字符串列 → 维度
    return "unknown"


# ==========================================================
# 智能图表推荐器
# ==========================================================

class ChartRecommender:
    """基于数据特征推荐最佳图表"""
    
    def recommend(self, data: List[Dict], columns: List[str]) -> Dict:
        """
        分析数据结构，推荐最佳图表
        
        Returns:
            {
                "chart_type": "line/bar/pie/...",
                "chart_name": "折线图",
                "x_axis": "date",
                "y_axis": ["sales"],
                "reason": "推荐理由",
                "options": {}  # ECharts 配置
            }
        """
        if not data or not columns:
            return self._default_chart()
        
        # 分析列类型
        col_types = {}
        for col in columns:
            col_types[col] = self._detect_column_type(col, data)
        
        # 分类列
        time_cols = [c for c, t in col_types.items() if t == "time"]
        metric_cols = [c for c, t in col_types.items() if t in ("metric", "number")]
        dimension_cols = [c for c, t in col_types.items() if t in ("dimension", "category")]
        
        # 数据行数
        n_rows = len(data)
        
        # 图表推荐逻辑
        chart_type, reason = self._select_chart(
            n_rows, time_cols, metric_cols, dimension_cols
        )
        
        # 确定 x/y 轴
        x_axis, y_axis = self._assign_axes(
            time_cols, metric_cols, dimension_cols, chart_type
        )
        
        # 生成配置
        options = self._generate_echarts_options(
            chart_type, x_axis, y_axis, data
        )
        
        return {
            "chart_type": chart_type,
            "chart_name": CHART_TYPES[chart_type]["name"],
            "x_axis": x_axis,
            "y_axis": y_axis,
            "reason": reason,
            "options": options,
        }
    
    def _detect_column_type(self, col_name: str, data: List[Dict]) -> str:
        """检测列类型"""
        # 时间列
        if is_time_column(col_name):
            return "time"
        
        # 采样值判断
        sample = [row.get(col_name) for row in data[:10] if row.get(col_name) is not None]
        
        if not sample:
            return "unknown"
        
        # 判断是否为数值列
        numeric_count = 0
        for val in sample:
            try:
                float(val)
                numeric_count += 1
            except (ValueError, TypeError):
                pass
        
        if numeric_count / len(sample) > 0.8:
            return "metric"
        
        # 判断是否为分类列（去重值较少或字符串）
        str_count = sum(1 for v in sample if isinstance(v, str))
        if str_count > 0:
            return "dimension"
        
        unique_ratio = len(set(str(v) for v in sample)) / len(sample)
        if unique_ratio < 0.5:
            return "dimension"
        
        return "unknown"
    
    def _select_chart(self, n_rows: int, time_cols: List[str],
                      metric_cols: List[str], dimension_cols: List[str]) -> tuple:
        """选择图表类型"""
        # K 线图检测
        if len(metric_cols) >= 4 and time_cols:
            metric_lower = [m.lower() for m in metric_cols]
            if any(k in metric_lower for k in ["open", "high", "low", "close"]):
                return "kline", "检测到 OHLC 数据，推荐 K 线图"
        
        # 有时间维度
        if time_cols:
            if len(metric_cols) == 1:
                if n_rows <= 30:
                    return "line", "单一时间序列，推荐折线图展示趋势"
                else:
                    return "area", "长时间序列，推荐面积图展示趋势"
            elif len(metric_cols) <= 3:
                return "line", "多指标时间序列，推荐折线图对比"
        
        # 占比/比例数据（优先于分类对比）
        if len(dimension_cols) == 1 and len(metric_cols) == 1 and not time_cols:
            return "pie", "维度占比数据，推荐饼图"
        
        # 分类对比
        if dimension_cols and metric_cols:
            return "bar", "分类对比数据，推荐柱状图"
        
        # 散布关系
        if len(metric_cols) == 2:
            return "scatter", "双变量关系，推荐散点图"
        
        # 大数据量 → 表格
        if n_rows > 50:
            return "table", "数据量较大，推荐表格展示"
        
        return "bar", "默认柱状图"
    
    def _assign_axes(self, time_cols, metric_cols, dimension_cols, chart_type):
        """分配坐标轴"""
        x_axis = None
        y_axis = []
        
        if chart_type == "pie":
            x_axis = dimension_cols[0] if dimension_cols else None
            y_axis = [metric_cols[0]] if metric_cols else []
            return x_axis, y_axis
        
        if chart_type == "scatter":
            x_axis = metric_cols[0] if metric_cols else None
            y_axis = [metric_cols[1]] if len(metric_cols) > 1 else []
            return x_axis, y_axis
        
        # 其他图表：X 轴为时间或维度，Y 轴为指标
        if time_cols:
            x_axis = time_cols[0]
        elif dimension_cols:
            x_axis = dimension_cols[0]
        
        y_axis = metric_cols if metric_cols else []
        
        return x_axis, y_axis
    
    def _generate_echarts_options(self, chart_type: str, x_axis: str,
                                   y_axis: List[str], data: List[Dict]) -> Dict:
        """生成 ECharts 配置"""
        if chart_type == "pie":
            return self._gen_pie_options(x_axis, y_axis, data)
        
        if chart_type == "scatter":
            return self._gen_scatter_options(x_axis, y_axis, data)
        
        if chart_type == "table":
            return self._gen_table_options(data)
        
        return self._gen_xy_options(chart_type, x_axis, y_axis, data)
    
    def _gen_pie_options(self, x_axis, y_axis, data):
        pie_data = [{"name": str(row.get(x_axis, "")), "value": row.get(y_axis[0], 0)} for row in data]
        return {
            "tooltip": {"trigger": "item", "formatter": "{b}: {c} ({d}%)"},
            "series": [{"type": "pie", "radius": ["40%", "70%"], "data": pie_data}]
        }
    
    def _gen_scatter_options(self, x_axis, y_axis, data):
        scatter_data = [[row.get(x_axis, 0), row.get(y_axis[0], 0)] for row in data]
        return {
            "xAxis": {"name": x_axis},
            "yAxis": {"name": y_axis[0]},
            "series": [{"type": "scatter", "data": scatter_data}]
        }
    
    def _gen_table_options(self, data):
        return {"table": True, "data": data[:100]}
    
    def _gen_xy_options(self, chart_type, x_axis, y_axis, data):
        x_data = [str(row.get(x_axis, "")) for row in data]
        
        if chart_type == "area":
            series = [{
                "name": col,
                "type": "line",
                "areaStyle": {"opacity": 0.3},
                "data": [row.get(col, 0) for row in data]
            } for col in y_axis]
        else:
            series = [{
                "name": col,
                "type": chart_type,
                "data": [row.get(col, 0) for row in data]
            } for col in y_axis]
        
        return {
            "tooltip": {"trigger": "axis"},
            "legend": {"data": y_axis},
            "xAxis": {"type": "category", "data": x_data},
            "yAxis": {"type": "value"},
            "series": series,
        }
    
    def _default_chart(self):
        return {
            "chart_type": "table",
            "chart_name": "数据表",
            "x_axis": None,
            "y_axis": [],
            "reason": "数据为空，使用默认表格显示",
            "options": {"table": True}
        }


# ==========================================================
# 自动洞察提炼引擎
# ==============================================

class InsightEngine:
    """基于规则自动提炼业务洞察"""
    
    def generate(self, data: List[Dict], columns: List[str],
                 chart_rec: Dict = None, query: str = "") -> Dict:
        """
        生成一句话业务洞察
        
        Returns:
            {
                "summary": "核心结论",
                "trend": "趋势判断",
                "anomaly": "异常发现",
                "action": "建议行动",
                "detail": { ... }
            }
        """
        if not data:
            return self._empty_result()
        
        result = {
            "summary": self._gen_summary(data, columns, query),
            "trend": self._gen_trend(data, columns),
            "anomaly": self._gen_anomaly(data, columns),
            "action": self._gen_action(data, columns, query),
            "metrics": self._calc_key_metrics(data, columns)
        }
        
        return result
    
    def _gen_summary(self, data, columns, query) -> str:
        """生成核心结论"""
        # 找到数值指标列（排除时间类列）
        time_cols = [c for c in columns if is_time_column(c)]
        metric_cols = [c for c in columns if self._is_numeric_col(c, data) and c not in time_cols]
        
        if not metric_cols:
            return f"共查询到 {len(data)} 条记录"
        
        # 选择方差最大的指标（最能体现数据差异）
        main_metric = metric_cols[0]
        if len(metric_cols) > 1:
            try:
                variances = {}
                for c in metric_cols:
                    vals = [float(row.get(c, 0) or 0) for row in data]
                    mean = sum(vals) / len(vals)
                    variances[c] = sum((v - mean) ** 2 for v in vals) / len(vals) if mean != 0 else 0
                main_metric = max(variances, key=variances.get)
            except Exception:
                pass
        
        values = [float(row.get(main_metric, 0) or 0) for row in data]
        
        if not values:
            return f"共 {len(data)} 条记录"
        
        total = sum(values)
        avg = total / len(values) if len(values) > 0 else 0
        
        # 维度列（非数值、非时间的字符串列，如省份）
        dim_cols = [c for c in columns if not self._is_numeric_col(c, data) and not is_time_column(c)]
        dim_name = dim_cols[0] if dim_cols else "总体"
        
        # 找最大/最小
        max_idx = values.index(max(values))
        min_idx = values.index(min(values))
        max_dim = str(data[max_idx].get(dim_name, f"第{max_idx+1}行"))
        min_dim = str(data[min_idx].get(dim_name, f"第{min_idx+1}行"))
        
        # 趋势判断（按时间排序后比较前后半段）
        trend_hint = ""
        if time_cols and len(values) >= 2:
            try:
                time_col = time_cols[0]
                sorted_data = sorted(data, key=lambda x: str(x.get(time_col, '')))
                sorted_vals = [float(row.get(main_metric, 0) or 0) for row in sorted_data]
                if len(sorted_vals) >= 2:
                    mid = len(sorted_vals) // 2
                    first_half = sum(sorted_vals[:mid]) / max(mid, 1)
                    second_half = sum(sorted_vals[mid:]) / max(len(sorted_vals) - mid, 1)
                    if first_half != 0:
                        change = (second_half - first_half) / abs(first_half)
                        if change > 0.05:
                            trend_hint = f"呈上升趋势（+{change:.1%}）"
                        elif change < -0.05:
                            trend_hint = f"呈下降趋势（{change:.1%}）"
                    elif second_half > first_half:
                        trend_hint = "呈上升趋势"
                    elif second_half < first_half:
                        trend_hint = "呈下降趋势"
            except Exception:
                pass
        
        summary_parts = [
            f"共 {len(data)} 条记录",
            f"{main_metric} 均值 {self._format_number(avg)}",
        ]
        
        if len(values) > 1:
            summary_parts.append(f"最高 {max_dim} ({self._format_number(max(values))})")
            summary_parts.append(f"最低 {min_dim} ({self._format_number(min(values))})")
        
        if trend_hint:
            summary_parts.append(trend_hint)
        
        return "，".join(summary_parts)
    
    def _gen_trend(self, data, columns) -> str:
        """判断趋势"""
        time_cols = [c for c in columns if is_time_column(c)]
        if not time_cols:
            return "无法识别时间维度"
        
        metric_cols = [c for c in columns if self._is_numeric_col(c, data) and c not in time_cols]
        if not metric_cols:
            return "无数值指标"
        
        # 选择方差最大的指标
        main_metric = metric_cols[0]
        if len(metric_cols) > 1:
            try:
                variances = {}
                for c in metric_cols:
                    vals = [float(row.get(c, 0) or 0) for row in data]
                    mean = sum(vals) / len(vals)
                    variances[c] = sum((v - mean) ** 2 for v in vals) / len(vals) if mean != 0 else 0
                main_metric = max(variances, key=variances.get)
            except Exception:
                pass
        
        try:
            time_col = time_cols[0]
            sorted_data = sorted(data, key=lambda x: str(x.get(time_col, '')))
            values = [float(row.get(main_metric, 0) or 0) for row in sorted_data]
        except Exception:
            values = [float(row.get(main_metric, 0) or 0) for row in data]
        
        if len(values) < 2:
            return "数据不足"
        
        # 计算环比增长
        growths = []
        for i in range(1, len(values)):
            if values[i-1] != 0:
                growth = (values[i] - values[i-1]) / abs(values[i-1])
                growths.append(growth)
        
        if not growths:
            return "无法计算增长"
        
        avg_growth = sum(growths) / len(growths)
        
        if avg_growth > 0.05:
            return f"整体上升，平均环比增长 {avg_growth:.1%}"
        elif avg_growth < -0.05:
            return f"整体下降，平均环比降低 {abs(avg_growth):.1%}"
        
        return "整体波动平稳"
    
    def _gen_anomaly(self, data, columns) -> str:
        """异常检测"""
        time_cols = [c for c in columns if is_time_column(c)]
        metric_cols = [c for c in columns if self._is_numeric_col(c, data) and c not in time_cols]
        anomalies = []
        
        # 维度列用于显示有意义的标签
        dim_cols = [c for c in columns if not self._is_numeric_col(c, data) and not is_time_column(c)]
        dim_name = dim_cols[0] if dim_cols else None
        
        for col in metric_cols:
            values = [float(row.get(col, 0) or 0) for row in data]
            if len(values) < 3:
                continue
            
            avg = sum(values) / len(values)
            std = math.sqrt(sum((v - avg)**2 for v in values) / len(values))
            
            if std == 0:
                continue
            
            for i, v in enumerate(values):
                z_score = abs(v - avg) / std
                if z_score > 2.0:
                    label = data[i].get(dim_name, f"第{i+1}行") if dim_name else f"第{i+1}行"
                    anomalies.append(f"{label} {col}={self._format_number(v)} 异常（偏离 {z_score:.1f}σ）")
        
        if anomalies:
            return anomalies[0]
        
        return "数据正常，无明显异常"
    
    def _gen_action(self, data, columns, query) -> str:
        """建议行动"""
        anomaly = self._gen_anomaly(data, columns)
        trend = self._gen_trend(data, columns)
        
        if "异常" in anomaly and "无异常" not in anomaly:
            return "建议深入排查异常数据点，确认数据准确性"
        
        if "下降" in trend:
            return "趋势呈下降状态，建议深入分析各细分维度贡献度"
        
        if "上升" in trend:
            return "表现良好，建议持续监控关键指标变化"
        
        return "建议持续观察，关注趋势变化"
    
    def _calc_key_metrics(self, data, columns) -> Dict:
        """计算关键指标"""
        metrics = {}
        
        for col in columns:
            if not self._is_numeric_col(col, data):
                continue
            
            values = [float(row.get(col, 0) or 0) for row in data]
            metrics[col] = {
                "total": round(sum(values), 2),
                "avg": round(sum(values) / len(values), 2),
                "max": round(max(values), 2),
                "min": round(min(values), 2),
                "growth": round((values[-1] - values[0]) / values[0] * 100, 2) if values[0] != 0 else 0
            }
        
        return metrics
    
    @staticmethod
    def _is_numeric_col(col: str, data: List[Dict]) -> bool:
        """判断列是否为数值列"""
        sample = [row.get(col) for row in data[:5] if row.get(col) is not None]
        if not sample:
            return False
        
        numeric = 0
        for val in sample:
            try:
                float(val)
                numeric += 1
            except (ValueError, TypeError):
                pass
        
        return numeric / len(sample) > 0.8
    
    @staticmethod
    def _format_number(num: float) -> str:
        """格式化数字"""
        if abs(num) >= 1e8:
            return f"{num/1e8:.1f}亿"
        elif abs(num) >= 1e4:
            return f"{num/1e4:.1f}万"
        elif abs(num) >= 1:
            return f"{num:,.0f}"
        else:
            return f"{num:.2f}"
    
    @staticmethod
    def _empty_result() -> Dict:
        return {
            "summary": "查询结果为空",
            "trend": "无数据",
            "anomaly": "无数据",
            "action": "请检查查询条件",
            "metrics": {}
        }


# ==========================================================
# 统一入口
# ==========================================================

class ChartInsightEngine:
    """图表推荐 + 洞察提炼 统一引擎"""
    
    def __init__(self):
        self.chart_recommender = ChartRecommender()
        self.insight_engine = InsightEngine()
    
    def analyze(self, data: List[Dict], columns: List[str],
                query: str = "") -> Dict:
        """
        完整分析：图表推荐 + 洞察提炼
        
        Returns:
            {
                "chart": { ... },
                "insight": { ... }
            }
        """
        # 图表推荐
        chart_rec = self.chart_recommender.recommend(data, columns)
        
        # 洞察提炼
        insight = self.insight_engine.generate(data, columns, chart_rec, query)
        
        return {
            "chart": chart_rec,
            "insight": insight,
        }


# ==============================================
# 全局实例
# ==============================================

chart_insight_engine = ChartInsightEngine()


# ==============================================
# 使用示例
# ==============================================

if __name__ == "__main__":
    # 测试数据
    test_data = [
        {"month": "2024-01", "sales": 1200000, "profit": 120000},
        {"month": "2024-02", "sales": 1350000, "profit": 135000},
        {"month": "2024-03", "sales": 1100000, "profit": 110000},
        {"month": "2024-04", "sales": 1500000, "profit": 150000},
        {"month": "2024-05", "sales": 1400000, "profit": 140000},
    ]
    columns = ["month", "sales", "profit"]
    
    engine = ChartInsightEngine()
    result = engine.analyze(test_data, columns, "月度销售趋势")
    
    print("=" * 60)
    print(" 智能图表推荐 + 自动洞察提炼")
    print("=" * 60)
    
    print(f"\n📊 图表推荐:")
    print(f"  类型: {result['chart']['chart_name']}")
    print(f"  X轴: {result['chart']['x_axis']}")
    print(f"  Y轴: {result['chart']['y_axis']}")
    print(f"  理由: {result['chart']['reason']}")
    
    print(f"\n💡 业务洞察:")
    print(f"  结论: {result['insight']['summary']}")
    print(f"  趋势: {result['insight']['trend']}")
    print(f"  异常: {result['insight']['anomaly']}")
    print(f"  建议: {result['insight']['action']}")
    
    print(f"\n📈 关键指标:")
    for metric, vals in result['insight']['metrics'].items():
        print(f"  {metric}: 合计 {vals['total']:,.0f}, 均值 {vals['avg']:,.0f}, 增长 {vals['growth']}%")
