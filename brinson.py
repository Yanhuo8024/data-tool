#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
brinson.py - Brinson 归因分析模型

将超额收益拆解为三部分：
1. 配置效应（Allocation Effect）：行业/板块权重偏离带来的收益
2. 选择效应（Selection Effect）：行业内个股精选带来的收益
3. 交互效应（Interaction Effect）：配置与选择的交叉影响

单周期 Brinson 模型：
    R_p = 组合收益, R_b = 基准收益
    w_pi = 组合中第i个行业的权重
    w_bi = 基准中第i个行业的权重
    R_pi = 组合中第i个行业的收益
    R_bi = 基准中第i个行业的收益
    
    配置效应 = Σ (w_pi - w_bi) × (R_bi - R_b)
    选择效应 = Σ w_bi × (R_pi - R_bi)
    交互效应 = Σ (w_pi - w_bi) × (R_pi - R_bi)
    
    总超额收益 = R_p - R_b = 配置 + 选择 + 交互
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class AttributionResult:
    """Brinson 归因结果"""
    total_return: float = 0.0           # 组合总收益
    benchmark_return: float = 0.0       # 基准总收益
    excess_return: float = 0.0          # 超额收益
    allocation_effect: float = 0.0      # 配置效应
    selection_effect: float = 0.0       # 选择效应
    interaction_effect: float = 0.0     # 交互效应
    
    # 分行业明细
    industry_details: List[Dict] = field(default_factory=list)
    
    def to_dict(self) -> Dict:
        return {
            "total_return_pct": round(self.total_return * 100, 2),
            "benchmark_return_pct": round(self.benchmark_return * 100, 2),
            "excess_return_pct": round(self.excess_return * 100, 2),
            "allocation_effect_pct": round(self.allocation_effect * 100, 2),
            "selection_effect_pct": round(self.selection_effect * 100, 2),
            "interaction_effect_pct": round(self.interaction_effect * 100, 2),
            "industry_details": self.industry_details
        }


def calculate_brinson_attribution(
    portfolio_weights: Dict[str, float],
    benchmark_weights: Dict[str, float],
    portfolio_returns: Dict[str, float],
    benchmark_returns: Dict[str, float],
    segments: List[str] = None
) -> AttributionResult:
    """
    计算单周期 Brinson 归因

    Args:
        portfolio_weights: 组合权重 {segment: weight}
        benchmark_weights: 基准权重 {segment: weight}
        portfolio_returns: 组合收益率 {segment: return}
        benchmark_returns: 基准收益率 {segment: return}
        segments: 行业/板块列表（可选，自动计算并集）

    Returns:
        AttributionResult 归因结果
    """
    result = AttributionResult()
    
    # 获取所有行业/板块
    if segments is None:
        segments = list(set(
            list(portfolio_weights.keys()) + 
            list(benchmark_weights.keys())
        ))
    
    # 计算总收益
    result.total_return = sum(
        portfolio_weights.get(s, 0) * portfolio_returns.get(s, 0) 
        for s in segments
    )
    result.benchmark_return = sum(
        benchmark_weights.get(s, 0) * benchmark_returns.get(s, 0) 
        for s in segments
    )
    result.excess_return = result.total_return - result.benchmark_return
    
    # 逐行业计算归因
    allocation_total = 0.0
    selection_total = 0.0
    interaction_total = 0.0
    
    for seg in segments:
        w_p = portfolio_weights.get(seg, 0.0)
        w_b = benchmark_weights.get(seg, 0.0)
        r_p = portfolio_returns.get(seg, 0.0)
        r_b = benchmark_returns.get(seg, 0.0)
        
        # 配置效应: (w_p - w_b) * (r_b - R_b)
        alloc = (w_p - w_b) * (r_b - result.benchmark_return)
        # 选择效应: w_b * (r_p - r_b)
        select = w_b * (r_p - r_b)
        # 交互效应: (w_p - w_b) * (r_p - r_b)
        interact = (w_p - w_b) * (r_p - r_b)
        
        allocation_total += alloc
        selection_total += select
        interaction_total += interact
        
        # 只记录有意义的行业
        if w_p != 0 or w_b != 0:
            result.industry_details.append({
                "segment": seg,
                "portfolio_weight": round(w_p * 100, 2),
                "benchmark_weight": round(w_b * 100, 2),
                "weight_diff": round((w_p - w_b) * 100, 2),
                "portfolio_return": round(r_p * 100, 2),
                "benchmark_return": round(r_b * 100, 2),
                "allocation_effect": round(alloc * 100, 2),
                "selection_effect": round(select * 100, 2),
                "interaction_effect": round(interact * 100, 2),
            })
    
    result.allocation_effect = allocation_total
    result.selection_effect = selection_total
    result.interaction_effect = interaction_total
    
    # 按配置效应绝对值排序
    result.industry_details.sort(key=lambda x: abs(x["allocation_effect"]) + abs(x["selection_effect"]), reverse=True)
    
    return result


def backtest_to_brinson(
    trades: List[Dict],
    price_data: Dict[str, pd.DataFrame],
    benchmark_data: pd.DataFrame = None,
    industry_map: Dict[str, str] = None,
    date_col: str = 'date',
    close_col: str = 'close',
    freq: str = 'M'
) -> AttributionResult:
    """
    从回测交易记录自动生成 Brinson 归因
    
    Args:
        trades: 交易记录列表 [{date, symbol, action, price, shares, value}]
        price_data: 各股票价格数据 {symbol: DataFrame}
        benchmark_data: 基准指数价格数据
        industry_map: 股票行业映射 {symbol: industry}
        date_col: 日期列名
        close_col: 收盘价列名
        freq: 归因频率 D/W/M

    Returns:
        AttributionResult
    """
    if not trades:
        return AttributionResult()
    
    # 构建组合权重时间序列
    portfolio_df = _build_portfolio_weights(trades, price_data, date_col, close_col, freq)
    
    # 默认行业映射（按股票代码前缀分类）
    if industry_map is None:
        industry_map = _infer_industries(list(price_data.keys()))
    
    # 合并行业信息
    portfolio_df['industry'] = portfolio_df['symbol'].map(industry_map).fillna('其他')
    
    # 按行业聚合权重
    industry_weights = portfolio_df.groupby('industry')['weight'].mean().to_dict()
    
    # 计算各行业收益
    industry_returns = _calculate_industry_returns(price_data, industry_map, freq, date_col, close_col)
    
    # 基准权重和收益
    benchmark_weights = _calculate_benchmark_weights(benchmark_data, industry_map, freq) if benchmark_data is not None else {}
    benchmark_returns = _calculate_benchmark_industry_returns(benchmark_data, industry_map, freq) if benchmark_data is not None else industry_returns
    
    # 归一化权重
    total_w = sum(industry_weights.values())
    if total_w > 0:
        industry_weights = {k: v / total_w for k, v in industry_weights.items()}
    
    # 计算 Brinson 归因
    all_industries = list(set(list(industry_weights.keys()) + list(benchmark_weights.keys())))
    
    result = calculate_brinson_attribution(
        portfolio_weights=industry_weights,
        benchmark_weights=benchmark_weights if benchmark_weights else industry_weights,
        portfolio_returns=industry_returns,
        benchmark_returns=benchmark_returns if benchmark_returns else industry_returns,
        segments=all_industries
    )
    
    return result


def _build_portfolio_weights(trades, price_data, date_col, close_col, freq):
    """构建组合权重时间序列"""
    records = []
    for t in trades:
        symbol = t.get('symbol', t.get('stock', ''))
        date = pd.to_datetime(t.get('date', t.get('日期', '')))
        value = float(t.get('value', t.get('金额', 0)))
        
        records.append({
            'date': date,
            'symbol': symbol,
            'value': value
        })
    
    df = pd.DataFrame(records)
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values('date')
    
    # 计算每个时点的总市值
    df['total_value'] = df.groupby('date')['value'].transform('sum')
    df['weight'] = df['value'] / df['total_value']
    
    return df


def _infer_industries(symbols):
    """根据股票代码前缀推断行业"""
    industries = {}
    for sym in symbols:
        prefix = sym[:3]
        if prefix in ['600', '601', '603', '605']:
            industries[sym] = '沪市主板'
        elif prefix in ['000', '001', '002']:
            industries[sym] = '深市主板'
        elif prefix in ['300', '301']:
            industries[sym] = '创业板'
        elif prefix in ['688', '689']:
            industries[sym] = '科创板'
        else:
            industries[sym] = '其他'
    return industries


def _calculate_industry_returns(price_data, industry_map, freq, date_col, close_col):
    """计算各行业平均收益"""
    industry_returns = {}
    
    for symbol, df in price_data.items():
        if symbol not in industry_map:
            continue
        industry = industry_map[symbol]
        
        if industry not in industry_returns:
            industry_returns[industry] = []
        
        # 计算周期收益
        df = df.sort_values(date_col)
        returns = df[close_col].pct_change().dropna()
        if len(returns) > 0:
            industry_returns[industry].append(returns.mean())
    
    # 取平均
    return {k: np.mean(v) if v else 0.0 for k, v in industry_returns.items()}


def _calculate_benchmark_weights(benchmark_data, industry_map, freq):
    """计算基准权重"""
    # 简化：假设基准等权
    industries = set(industry_map.values())
    n = len(industries)
    return {ind: 1.0 / n for ind in industries} if n > 0 else {}


def _calculate_benchmark_industry_returns(benchmark_data, industry_map, freq):
    """计算基准行业收益"""
    # 简化：返回等权收益
    industries = set(industry_map.values())
    return {ind: 0.0 for ind in industries}


def simple_backtest_attribution(
    strategy_returns: pd.Series,
    benchmark_returns: pd.Series,
    num_segments: int = 5,
    date_labels: List[str] = None
) -> AttributionResult:
    """
    简化版回测归因：将收益按时间段分段归因

    Args:
        strategy_returns: 策略收益率序列
        benchmark_returns: 基准收益率序列
        num_segments: 分段数量
        date_labels: 各段标签

    Returns:
        AttributionResult
    """
    if len(strategy_returns) == 0:
        return AttributionResult()
    
    total_return = strategy_returns.sum()
    bench_return = benchmark_returns.sum()
    excess = total_return - bench_return
    
    # 分段
    n = len(strategy_returns)
    seg_size = max(n // num_segments, 1)
    
    if date_labels is None:
        date_labels = [f"第{i+1}段" for i in range(num_segments)]
    
    segments = []
    portfolio_w = {}
    portfolio_r = {}
    benchmark_w = {}
    benchmark_r = {}
    
    for i in range(min(num_segments, n)):
        start_idx = i * seg_size
        end_idx = min((i + 1) * seg_size, n)
        
        seg_label = date_labels[i] if i < len(date_labels) else f"第{i+1}段"
        seg_strat = strategy_returns.iloc[start_idx:end_idx].sum()
        seg_bench = benchmark_returns.iloc[start_idx:end_idx].sum()
        
        # 等权配置
        portfolio_w[seg_label] = 1.0 / num_segments
        benchmark_w[seg_label] = 1.0 / num_segments
        portfolio_r[seg_label] = seg_strat
        benchmark_r[seg_label] = seg_bench
        
        segments.append(seg_label)
    
    return calculate_brinson_attribution(
        portfolio_weights=portfolio_w,
        benchmark_weights=benchmark_w,
        portfolio_returns=portfolio_r,
        benchmark_returns=benchmark_r,
        segments=segments
    )


def generate_attribution_report(result: AttributionResult) -> str:
    """生成归因报告（文本格式）"""
    d = result.to_dict()
    
    # 结论
    conclusion = abs(d['allocation_effect_pct']) > abs(d['selection_effect_pct'])
    conclusion_text = (
        "组合的超额收益主要来自行业配置能力" if conclusion 
        else "组合的超额收益主要来自个股精选能力"
    )
    
    report = f"""
{'='*60}
            Brinson 归因分析报告
{'='*60}

一、收益概览
    组合总收益:        {d['total_return_pct']:>8.2f}%
    基准总收益:        {d['benchmark_return_pct']:>8.2f}%
    超额收益:          {d['excess_return_pct']:>8.2f}%

二、归因拆解
    配置效应（行业选择）: {d['allocation_effect_pct']:>8.2f}%
    选择效应（个股精选）: {d['selection_effect_pct']:>8.2f}%
    交互效应:            {d['interaction_effect_pct']:>8.2f}%

三、结论
    {conclusion_text}

四、行业明细"""
    
    for ind in d['industry_details'][:10]:
        report += f"""
    {ind['segment']}:
        组合权重 {ind['portfolio_weight']:>6.2f}% | 基准权重 {ind['benchmark_weight']:>6.2f}%
        组合收益 {ind['portfolio_return']:>6.2f}% | 基准收益 {ind['benchmark_return']:>6.2f}%
        配置效应 {ind['allocation_effect']:>6.2f}% | 选择效应 {ind['selection_effect']:>6.2f}%"""
    
    report += f"\n{'='*60}\n"
    return report
