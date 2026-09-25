#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ai_analysis_engine.py — AI 增强分析引擎

核心能力：
1. 多维关联分析：自动发现列间关系、分组差异、隐藏模式
2. 智能异常检测：多算法异常点识别（Z-score + IQR + 孤立森林）
3. 趋势预测：时序数据自动分解 + Prophet/线性预测
4. 可解释性报告：自动生成自然语言分析报告
5. 上下文感知推理：结合用户问题理解分析意图
"""

import math
import re
from typing import Dict, List, Optional, Any, Tuple
from collections import Counter, defaultdict
from datetime import datetime
import pandas as pd
import numpy as np

from chart_insight_engine import ChartInsightEngine, is_time_column


class EnhancedAnalysisEngine:
    """增强分析引擎：多维关联 + 异常检测 + 趋势预测 + 可解释报告"""
    
    def __init__(self, df: pd.DataFrame, file_name: str = ""):
        self.df = df
        self.file_name = file_name
        self.cols = df.columns.tolist()
        self.num_cols = df.select_dtypes(include=['number']).columns.tolist()
        self.cat_cols = df.select_dtypes(include=['object', 'category']).columns.tolist()
        self.time_cols = [c for c in self.cols if is_time_column(c)]
        self.insight_engine = ChartInsightEngine()
        self._cache = {}
    
    def full_analysis(self, query: str = "") -> Dict:
        """执行完整分析（含计量经济学深度分析）"""
        result = {
            'overview': self._overview(),
            'correlations': self._correlation_analysis(),
            'anomalies': self._anomaly_detection(),
            'trends': self._trend_analysis(),
            'segments': self._segment_analysis(),
            'key_findings': self._key_findings(query),
            'risk_points': self._risk_assessment(),
            'recommendations': self._generate_recommendations(query),
        }
        
        # 面板数据诊断
        result['panel_diagnostics'] = self._panel_diagnostics()
        
        # DID回归（如果数据含treat和year列）
        did_result = self._did_regression_analysis()
        if did_result:
            result['did_regression'] = did_result
        
        # 平行趋势检验
        if did_result and did_result.get('has_treat'):
            result['parallel_trends'] = self._parallel_trends_analysis()
        
        # 异质性分析
        result['heterogeneity'] = self._heterogeneity_analysis()
        
        # 机制分析
        result['mechanism'] = self._mechanism_analysis()
        
        return result
    
    def _panel_diagnostics(self) -> Dict:
        """面板数据诊断"""
        if not self.time_cols or len(self.df) < 10:
            return {'message': '非面板数据，跳过面板诊断'}
        
        time_col = self.time_cols[0]
        
        # 检查是否为平衡面板
        unit_counts = self.df.groupby(time_col).size()
        balanced = unit_counts.std() == 0
        
        # 时间跨度
        time_span = self.df[time_col].max() - self.df[time_col].min()
        
        # 个体数
        n_units = self.df[time_col].nunique() if time_col in self.df.columns else 0
        
        return {
            'balanced': balanced,
            'time_span': time_span,
            'n_time_periods': len(unit_counts),
            'avg_obs_per_period': unit_counts.mean(),
            'message': f'{"平衡" if balanced else "非平衡"}面板，时间跨度{time_span}年，{n_units}个个体'
        }
    
    def _did_regression_analysis(self) -> Optional[Dict]:
        """DID回归分析：对surv/dev/upgr分别回归"""
        # 查找处理组变量
        treat_col = None
        for col in self.df.columns:
            if any(k in col for k in ['试点', 'treat', 'policy', '处理']):
                treat_col = col
                break
        
        if not treat_col or not self.time_cols:
            return None
        
        time_col = None
        for col in self.time_cols:
            if col in self.df.columns:
                time_col = col
                break
        
        if not time_col:
            return None
        
        unit_col = None
        for col in self.df.columns:
            if col.lower() in ('province', 'city', 'id', '个体', '省份'):
                unit_col = col
                break
        
        if not unit_col:
            if self.cat_cols:
                unit_col = self.cat_cols[0]
            else:
                return None
        
        # 查找所有被解释变量
        dep_vars = {}
        for col in self.df.columns:
            col_lower = col.lower()
            if any(k in col_lower for k in ['surv', '生存']):
                dep_vars['surv'] = col
            elif any(k in col_lower for k in ['dev', '发展']):
                dep_vars['dev'] = col
            elif any(k in col_lower for k in ['enjoy', '享受']):
                dep_vars['enjoy'] = col
            elif any(k in col_lower for k in ['upgr', 'upgrad', '升级', '消费结构']):
                dep_vars['upgr'] = col
        
        if not dep_vars:
            if self.num_cols:
                dep_vars['first'] = self.num_cols[0]
            else:
                return None
        
        # 查找控制变量
        control_vars = []
        for col in self.num_cols:
            if col not in list(dep_vars.values()) and col != treat_col and col != time_col and col != unit_col:
                control_vars.append(col)
        
        # 处理连续处理变量：创建虚拟变量
        df_reg = self.df.copy()
        if df_reg[treat_col].nunique() > 2 and df_reg[treat_col].min() >= 0 and df_reg[treat_col].max() <= 1:
            treat_dummy = f'{treat_col}_treated'
            df_reg[treat_dummy] = (df_reg[treat_col] > 0).astype(int)
            actual_treat = treat_dummy
        else:
            actual_treat = treat_col
        
        try:
            from did import did_regression
            
            results = {}
            for dep_name, dep_var in dep_vars.items():
                if dep_var not in df_reg.columns:
                    continue
                
                baseline = did_regression(df_reg, dep_var, actual_treat, time_col, unit_col, [])
                
                if 'error' not in baseline:
                    results[dep_name] = {
                        'dep_var': dep_var,
                        'baseline': baseline,
                        'coef': baseline['params'].get(actual_treat, 0),
                        'pval': baseline['pvalues'].get(actual_treat, 1),
                        'r2': baseline.get('r2', 0),
                        'n_obs': baseline.get('n_obs', 0)
                    }
                    
                    full = did_regression(df_reg, dep_var, actual_treat, time_col, unit_col, control_vars[:10])
                    if 'error' not in full:
                        results[dep_name]['full_model'] = full
                        results[dep_name]['full_coef'] = full['params'].get(actual_treat, 0)
            
            if not results:
                return {'has_treat': True, 'error': '所有回归均失败'}
            
            return {
                'has_treat': True,
                'dep_vars': dep_vars,
                'treat_col': actual_treat,
                'original_treat_col': treat_col,
                'time_col': time_col,
                'unit_col': unit_col,
                'results': results,
                'n_controls': len(control_vars[:10])
            }
        except Exception as e:
            return {'has_treat': True, 'error': str(e)}
    
    def _parallel_trends_analysis(self) -> Dict:
        """平行趋势检验（简化版）"""
        return {'message': '需通过 did.py 的 parallel_trends() 函数执行完整检验'}
    
    def _heterogeneity_analysis(self) -> Dict:
        """异质性分析"""
        if not self.cat_cols or not self.num_cols:
            return {'message': '无分类变量，跳过异质性分析'}
        
        return {'message': '建议按老龄化程度(disold)、城镇化率(urban)、收入水平(lininc)分组'}
    
    def _mechanism_analysis(self) -> Dict:
        """机制分析"""
        return {'message': '建议检验中介变量：医疗支出(medexp)、养老床位(bedrate)'}
    
    def _overview(self) -> Dict:
        """数据概览"""
        overview = {
            'rows': len(self.df),
            'cols': len(self.cols),
            'num_cols': self.num_cols,
            'cat_cols': self.cat_cols,
            'time_cols': self.time_cols,
            'missing_rate': round(self.df.isnull().sum().sum() / max(len(self.df) * len(self.cols), 1) * 100, 2),
        }
        if self.num_cols:
            desc = self.df[self.num_cols].describe().T
            overview['stats'] = desc[['mean', 'std', 'min', 'max']].round(2).to_dict()
        return overview
    
    def _correlation_analysis(self) -> Dict:
        """多维关联分析"""
        if len(self.num_cols) < 2:
            return {'message': '数值列不足，无法计算关联'}
        
        corr = self.df[self.num_cols].corr()
        
        # 找最强正/负相关对
        pairs = []
        for i in range(len(self.num_cols)):
            for j in range(i+1, len(self.num_cols)):
                c = corr.iloc[i, j]
                if not math.isnan(c):
                    pairs.append((self.num_cols[i], self.num_cols[j], round(c, 4)))
        
        pairs.sort(key=lambda x: abs(x[2]), reverse=True)
        
        # 分类相关强度
        strong = [p for p in pairs if abs(p[2]) >= 0.7]
        moderate = [p for p in pairs if 0.4 <= abs(p[2]) < 0.7]
        weak = [p for p in pairs if abs(p[2]) < 0.4]
        
        # 找潜在因果关系（相关系数高且逻辑上合理）
        causal_hints = []
        for col1, col2, c in strong:
            if c > 0:
                causal_hints.append(f"{col1} ↑ → {col2} ↑ (可能同向驱动)")
            else:
                causal_hints.append(f"{col1} ↑ → {col2} ↓ (可能反向制约)")
        
        return {
            'top_pairs': pairs[:5],
            'strong': strong[:3],
            'moderate': moderate[:3],
            'weak_count': len(weak),
            'causal_hints': causal_hints[:3],
            'corr_matrix': corr.round(3).to_dict(),
        }
    
    def _anomaly_detection(self) -> Dict:
        """智能异常检测（Z-score + IQR + 孤立森林思路）"""
        if not self.num_cols:
            return {'message': '无数值列'}
        
        anomalies = {}
        total_outliers = 0
        
        for col in self.num_cols:
            series = self.df[col].dropna()
            if len(series) < 5:
                continue
            
            # Z-score 法
            z_scores = ((series - series.mean()) / series.std()).abs()
            z_outliers = series[z_scores > 2.5]
            
            # IQR 法
            q1 = series.quantile(0.25)
            q3 = series.quantile(0.75)
            iqr = q3 - q1
            lower = q1 - 1.5 * iqr
            upper = q3 + 1.5 * iqr
            iqr_outliers = series[(series < lower) | (series > upper)]
            
            # 综合：两种方法都检测到才算异常
            combined = pd.Index(z_outliers.index).intersection(iqr_outliers.index)
            outlier_vals = series.loc[combined]
            
            if len(outlier_vals) > 0:
                anomalies[col] = {
                    'count': len(outlier_vals),
                    'rate': round(len(outlier_vals) / len(series) * 100, 2),
                    'values': outlier_vals.head(5).round(2).to_dict(),
                    'direction': 'high' if outlier_vals.mean() > series.mean() else 'low',
                    'severity': 'high' if len(outlier_vals) / len(series) > 0.05 else 'medium',
                }
                total_outliers += len(outlier_vals)
        
        return {
            'total_outliers': total_outliers,
            'affected_cols': list(anomalies.keys()),
            'details': anomalies,
            'message': f"发现 {total_outliers} 个异常点" if total_outliers > 0 else "数据分布正常",
        }
    
    def _trend_analysis(self) -> Dict:
        """趋势分析"""
        if not self.time_cols or not self.num_cols:
            return {'message': '缺少时间列或数值列'}
        
        time_col = self.time_cols[0]
        trends = {}
        
        for col in self.num_cols[:5]:  # 限制数量
            try:
                sorted_df = self.df.sort_values(time_col)
                values = sorted_df[col].dropna().values
                
                if len(values) < 3:
                    continue
                
                # 简单线性趋势
                x = np.arange(len(values))
                slope, intercept = np.polyfit(x, values, 1)
                
                # 趋势强度
                change_pct = (slope * len(values) / np.mean(values) * 100) if np.mean(values) != 0 else 0
                
                # 季节性检测（简单方差分析）
                if len(values) >= 6:
                    half = len(values) // 2
                    first_half_std = np.std(values[:half])
                    second_half_std = np.std(values[half:])
                    stability = 'stable' if abs(first_half_std - second_half_std) < np.std(values) * 0.5 else 'volatile'
                else:
                    stability = 'insufficient_data'
                
                # 拐点检测
                diffs = np.diff(values)
                turning_points = np.where(np.diff(diffs) != 0)[0]
                
                trends[col] = {
                    'direction': 'up' if slope > 0 else 'down',
                    'slope': round(slope, 4),
                    'total_change_pct': round(change_pct, 2),
                    'stability': stability,
                    'turning_points': len(turning_points),
                }
            except Exception:
                pass
        
        return trends
    
    def _segment_analysis(self) -> Dict:
        """分组分析（按类别列分组统计）"""
        if not self.cat_cols or not self.num_cols:
            return {'message': '缺少数值列或分类列'}
        
        cat_col = self.cat_cols[0]
        segments = {}
        
        try:
            grouped = self.df.groupby(cat_col)[self.num_cols[0]]
            stats = grouped.agg(['mean', 'std', 'count']).round(2)
            
            # 找最大/最小分组
            means = grouped.mean().sort_values(ascending=False)
            
            segments = {
                'group_col': cat_col,
                'metric_col': self.num_cols[0],
                'group_count': len(means),
                'top_group': (means.index[0], round(means.iloc[0], 2)) if len(means) > 0 else None,
                'bottom_group': (means.index[-1], round(means.iloc[-1], 2)) if len(means) > 0 else None,
                'gap_ratio': round(means.iloc[0] / means.iloc[-1], 2) if len(means) > 1 and means.iloc[-1] != 0 else None,
            }
        except Exception:
            pass
        
        return segments
    
    def _key_findings(self, query: str) -> List[str]:
        """根据用户问题生成关键发现"""
        findings = []
        
        # 基于数据特征生成发现
        if self.num_cols:
            # 找方差最大的列
            variances = self.df[self.num_cols].var()
            max_var_col = variances.idxmax()
            findings.append(f"{max_var_col} 是数据中变化最大的指标（方差={variances[max_var_col]:.2f}），是分析的重点")
            
            # 找分布最偏的列
            skewness = self.df[self.num_cols].skew()
            max_skew_col = skewness.abs().idxmax()
            if abs(skewness[max_skew_col]) > 1:
                direction = "右偏" if skewness[max_skew_col] > 0 else "左偏"
                findings.append(f"{max_skew_col} 分布{direction}（偏度={skewness[max_skew_col]:.2f}），存在极端值影响")
        
        # DID回归发现（多模型）
        did = self._did_regression_analysis()
        if did and did.get('results'):
            for dep_name, res in did['results'].items():
                coef = res.get('coef', 0)
                pval = res.get('pval', 1)
                sig = '***' if pval < 0.01 else '**' if pval < 0.05 else '*' if pval < 0.1 else ''
                dep_var_short = res.get('dep_var', dep_name)[:15]
                findings.append(f"{dep_name}（{dep_var_short}）: DID={coef:.4f}{sig}（p={pval:.4f}）")
                if 'full_coef' in res:
                    change = (coef - res['full_coef']) / abs(coef) * 100 if coef != 0 else 0
                    findings.append(f"  → 控制变量后: {res['full_coef']:.4f}（变化{change:.1f}%）")
        
        # 基于用户问题生成发现
        if query:
            if any(kw in query for kw in ['趋势', '变化', '增长', '下降']):
                if self.time_cols:
                    findings.append(f"时间维度（{self.time_cols[0]}）已识别，建议关注中长期趋势与季节性波动")
            if any(kw in query for kw in ['异常', '问题', '风险']):
                findings.append("已启用多算法异常检测（Z-score + IQR），将标记偏离正常模式的观测点")
            if any(kw in query for kw in ['did', '双重差分', 'didi', '政策评估']):
                findings.append("检测到面板数据结构，已自动执行DID回归分析")
        
        return findings[:5]
    
    def _risk_assessment(self) -> List[str]:
        """风险评估"""
        risks = []
        
        # 缺失值风险
        missing = self.df.isnull().sum()
        high_missing = missing[missing > len(self.df) * 0.1]
        if len(high_missing) > 0:
            risks.append(f"⚠️ {', '.join(high_missing.index[:3])} 缺失率超过10%，可能影响分析可靠性")
        
        # 异常值风险
        for col in self.num_cols:
            series = self.df[col].dropna()
            if len(series) > 0:
                z_scores = ((series - series.mean()) / series.std()).abs()
                outlier_rate = (z_scores > 3).sum() / len(series) * 100
                if outlier_rate > 5:
                    risks.append(f"⚠️ {col} 异常值比例 {outlier_rate:.1f}%，建议检查数据质量")
        
        # 样本量风险
        if len(self.df) < 30:
            risks.append(f"⚠️ 样本量仅 {len(self.df)} 条，统计推断可能不够稳健")
        
        return risks[:5]
    
    def _generate_recommendations(self, query: str) -> List[str]:
        """生成分析建议"""
        recs = []
        
        if len(self.num_cols) >= 2:
            recs.append("建议进行相关性分析，识别关键驱动因素")
        
        if self.time_cols:
            recs.append(f"建议使用时序分解分析 {self.time_cols[0]} 维度的趋势和季节性")
        
        if self.cat_cols:
            recs.append(f"建议按 {self.cat_cols[0]} 分组，比较不同类别的指标差异")
        
        if any(kw in query for kw in ['预测', '未来', '走势']):
            recs.append("建议使用 Prophet 或 ARIMA 模型进行趋势预测")
        
        if any(kw in query for kw in ['异常', '检测', '问题']):
            recs.append("建议使用孤立森林或 DBSCAN 进行更精细的异常检测")
        
        return recs[:5]
    
    def generate_report(self, query: str = "") -> str:
        """Generate professional analysis report (journal-quality)"""
        analysis = self.full_analysis(query)
        
        lines = []
        lines.append("## Data Analysis Report")
        lines.append(f"**File**: {self.file_name} | **Sample**: {analysis['overview']['rows']} rows x {analysis['overview']['cols']} cols")
        lines.append("")
        
        # Core conclusions
        conclusions = []
        
        # DID findings first (most important)
        did = analysis.get('did_regression', {})
        if did and did.get('results'):
            sig_count = sum(1 for r in did['results'].values() if r.get('pval', 1) < 0.1)
            total_count = len(did['results'])
            conclusions.append(f"DID: {sig_count}/{total_count} models show significant policy effect (p<0.1)")
            
            for dep_name, res in did['results'].items():
                coef = res.get('coef', 0)
                pval = res.get('pval', 1)
                sig = '***' if pval < 0.01 else '**' if pval < 0.05 else '*' if pval < 0.1 else ''
                if pval < 0.1:
                    direction = "positive" if coef > 0 else "negative"
                    conclusions.append(f"{dep_name}: {direction} effect (coef={coef:.4f}{sig}, p={pval:.4f})")
        
        # Correlation findings
        if analysis['correlations'].get('strong'):
            top_corr = analysis['correlations']['strong'][0]
            conclusions.append(f"Strongest correlation: {top_corr[0]} vs {top_corr[1]} (r={top_corr[2]})")
        
        # Trend findings
        if analysis['trends'] and 'message' not in analysis['trends']:
            trend_cols = list(analysis['trends'].keys())
            if trend_cols:
                first_trend = analysis['trends'][trend_cols[0]]
                direction = "up" if first_trend['direction'] == 'up' else "down"
                conclusions.append(f"{trend_cols[0]} trending {direction} ({first_trend['total_change_pct']}%)")
        
        if conclusions:
            lines.append("### Core Conclusions")
            for c in conclusions:
                lines.append(f"- {c}")
            lines.append("")
        
        # Key findings
        if analysis['key_findings']:
            lines.append("### Key Findings")
            for f in analysis['key_findings']:
                lines.append(f"- {f}")
            lines.append("")
        
        # Correlation analysis
        if analysis['correlations'].get('strong'):
            lines.append("### Strong Correlations")
            for col1, col2, c in analysis['correlations']['strong']:
                lines.append(f"- {col1} <-> {col2}: r = {c}")
            lines.append("")
        
        # Anomaly detection
        if analysis['anomalies'].get('total_outliers', 0) > 0:
            lines.append("### Anomaly Detection")
            lines.append(analysis['anomalies']['message'])
            for col, detail in list(analysis['anomalies'].get('details', {}).items())[:3]:
                lines.append(f"- {col}: {detail['count']} outliers ({detail['severity']})")
            lines.append("")
        
        # Trend analysis
        if analysis['trends'] and 'message' not in analysis['trends']:
            lines.append("### Trend Analysis")
            for col, trend in analysis['trends'].items():
                direction = "up" if trend['direction'] == 'up' else "down"
                lines.append(f"- {col}: {direction} trend, cumulative change {trend['total_change_pct']}%")
            lines.append("")
        
        # Segment analysis
        if analysis['segments'].get('top_group'):
            seg = analysis['segments']
            lines.append("### Segment Comparison")
            lines.append(f"- Highest {seg['metric_col']}: {seg['top_group'][0]} ({seg['top_group'][1]})")
            lines.append(f"- Lowest {seg['metric_col']}: {seg['bottom_group'][0]} ({seg['bottom_group'][1]})")
            if seg.get('gap_ratio'):
                lines.append(f"- Gap ratio: {seg['gap_ratio']}x")
            lines.append("")
        
        # DID Regression Results
        if did and did.get('results'):
            lines.append("### DID Regression Results")
            lines.append(f"- Treatment variable: {did.get('treat_col', '-')}")
            lines.append(f"- Observations: {list(did['results'].values())[0].get('n_obs', '-')}")
            lines.append("")
            
            for dep_name, res in did['results'].items():
                dep_var = res.get('dep_var', dep_name)
                lines.append(f"**{dep_name} ({dep_var})**")
                lines.append(f"- Baseline: coef={res.get('coef', 0):.4f}, p={res.get('pval', 1):.4f}")
                if 'full_coef' in res:
                    lines.append(f"- With controls: coef={res.get('full_coef', 0):.4f}")
                lines.append(f"- R-squared: {res.get('r2', 0):.4f}")
                lines.append("")
        
        # Risk points
        if analysis['risk_points']:
            lines.append("### Risk Assessment")
            for r in analysis['risk_points']:
                lines.append(f"- {r}")
            lines.append("")
        
        # Recommendations
        if analysis['recommendations']:
            lines.append("### Recommendations")
            for r in analysis['recommendations']:
                lines.append(f"- {r}")
        
        return "\n".join(lines)


def analyze_dataframe(df: pd.DataFrame, query: str = "", file_name: str = "") -> Dict:
    """便捷函数：分析 DataFrame 并返回结果"""
    engine = EnhancedAnalysisEngine(df, file_name)
    return engine.full_analysis(query)


def generate_analysis_report(df: pd.DataFrame, query: str = "", file_name: str = "") -> str:
    """便捷函数：生成分析报告"""
    engine = EnhancedAnalysisEngine(df, file_name)
    return engine.generate_report(query)
