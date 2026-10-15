#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
real_compute.py - 真实计算结果注入（防幻觉，C）

对"金融计算/数据分析"类请求，在让 LLM 解读之前，先用后端真实函数算出数值，
把真实结果作为上下文注入给 LLM，LLM 只做解读，禁止模型自己数值推算。

设计：
- 纯本地、无网络、无 HTTP：直接调用既有 backend 计算函数
- 落地优先：只实现可稳定复用的计算（基金业绩指标 / 描述统计 / 相关性 / 假设检验）
- 任何失败静默降级返回 None，绝不阻断主流程
"""

import json
import re
import logging
from pathlib import Path
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

try:
    from core_config import OUT_DIR, load_dataframe, pd, np
    _HAS_CORE = True
except Exception as e:
    _HAS_CORE = False
    logger.debug("real_compute 依赖加载失败: %s", e)


# ---------------- 意图识别：计算类 -> 处理函数 ----------------

_INTENT_PATTERNS = {
    "fund_metrics": ["夏普", "sharpe", "回撤", "drawdown", "卡玛", "calmar", "索提诺", "sortino", "年化收益", "净值", "基金", "业绩指标", "波动率"],
    "describe": ["描述统计", "描述性", "统计特征", "均值", "方差", "分布", "describe", "概览", "概况", "基础统计"],
    "correlation": ["相关", "corr", "pearson", "spearman", "相关系系", "关联性"],
    "hypothesis": ["t检验", "t 检验", "显著性", "ttest", "假设检验", "p值", "卡方", "anova", "方差分析"],
    "stability": ["稳定性", "psi", "漂移", "ks", "woe", "iv", "评分卡"],
    # 纯数字列表运算：平均值/求和/中位数等（无需数据文件）
    "numbers": ["平均值", "平均数", "平均一下", "求和", "相加", "总和", "加一下", "中位数",
                "标准差", "均值", "总和是", "算一下", "帮我算", "加总", "乘一下", "算这些"],
}


def infer_compute_intent(message: str) -> Optional[str]:
    """识别消息对应的真实计算类型；不匹配返回 None"""
    low = message.lower()
    # 纯数字列表运算优先：需命中运算词且消息内含 ≥2 个数字
    # （避免"平均值"含"均值"被 describe 抢先；也避免年份/代码被误当数字列表）
    nums_in_msg = len(re.findall(r'(?<![\d.])(\d+(?:\.\d+)?)(?![\d.])', message))
    if nums_in_msg >= 2 and any(k in low for k in _INTENT_PATTERNS['numbers']):
        return 'numbers'
    for intent, kws in _INTENT_PATTERNS.items():
        if intent == 'numbers':
            continue
        if any(k in low for k in kws):
            return intent
    return None


def _require_file(current_file: str) -> Optional[Path]:
    if not current_file:
        return None
    # P1 修复：路径穿越守卫（与 load_dataframe 同一白名单语义，防 ../ 读任意文件）
    try:
        from core_config import safe_data_path
        safe = safe_data_path(OUT_DIR / current_file)
        return safe if (safe and safe.exists()) else None
    except Exception:
        return None


# ---------------- 基金业绩指标（真实计算） ----------------

def _compute_fund_metrics(file: str, rf: float = 0.02) -> Optional[Dict[str, Any]]:
    path = _require_file(file)
    if not path:
        return {"error": "未提供有效数据文件"}
    try:
        from finance_modeler import FinanceModeler
        modeler = FinanceModeler()
        if path.suffix.lower() == '.csv':
            df = pd.read_csv(path)
        else:
            df = pd.read_excel(path)
        modeler.load_data(df)
        metrics = modeler.fund_performance_metrics(nav_col=None, rf=rf)
        if not metrics.get("ok", True):
            return {"error": metrics.get("error", "指标计算失败")}
        # 剔除 base64 图，只取数值
        return {k: (round(float(v), 4) if isinstance(v, (int, float)) else v)
                for k, v in metrics.items() if k not in ('chart', 'figure', 'plot', 'ok', 'image', 'img')}
    except Exception as e:
        logger.debug("fund_metrics 计算失败: %s", e)
        return {"error": f"指标计算失败: {e}"}


# ---------------- 纯数字列表运算（无需文件） ----------------

_NUM_OPS = {'平均': 'mean', '均值': 'mean', '总和': 'sum', '求和': 'sum', '相加': 'sum',
            '加总': 'sum', '中位数': 'median', '标准差': 'std', '最大': 'max', '最小': 'min'}


def _compute_numbers(message: str) -> Optional[Dict[str, Any]]:
    """从消息中提取数字列表并求值（mean/sum/median/std 等）。"""
    try:
        import numpy as _np
        # 提取数字：支持 1,2,3 / 1 2 3 / 1.5、2.5
        nums = [float(x) for x in re.findall(r'(?<![\d.])(\d+(?:\.\d+)?)(?![\d.])', message)]
        # 排除疑似 6 位股票/年份代码等过大数据（统计场景多为小数/小整数）
        nums = [n for n in nums if n < 100000]
        if len(nums) < 2:
            return {"error": "未在消息中找到至少 2 个数字"}
        op = 'mean'
        for kw, o in _NUM_OPS.items():
            if kw in message:
                op = o
                break
        a = _np.array(nums)
        if op == 'sum':
            result = float(a.sum())
        elif op == 'median':
            result = float(_np.median(a))
        elif op == 'std':
            result = float(a.std(ddof=1 if len(nums) > 1 else 0))
        elif op == 'max':
            result = float(a.max())
        elif op == 'min':
            result = float(a.min())
        else:
            result = float(a.mean())
        return {
            "operation": op,
            "numbers": nums,
            "count": len(nums),
            "result": round(result, 4),
        }
    except Exception as e:
        logger.debug("numbers 计算失败: %s", e)
        return {"error": f"数字计算失败: {e}"}


# ---------------- 描述统计（真实计算） ----------------

def _compute_describe(file: str) -> Optional[Dict[str, Any]]:
    path = _require_file(file)
    if not path:
        return {"error": "未提供有效数据文件"}
    try:
        df = load_dataframe(str(path))
        if df is None or df.empty:
            return {"error": "数据为空"}
        num = df.select_dtypes(include=[np.number])
        out = {"rows": int(len(df)), "cols": int(df.shape[1])}
        if not num.empty:
            d = num.describe()
            out["numeric_summary"] = {}
            for col in d.columns:
                out["numeric_summary"][str(col)] = {
                    "count": int(d.loc["count", col]),
                    "mean": round(float(d.loc["mean", col]), 4),
                    "std": round(float(d.loc["std", col]), 4),
                    "min": round(float(d.loc["min", col]), 4),
                    "25%": round(float(d.loc["25%", col]), 4),
                    "50%": round(float(d.loc["50%", col]), 4),
                    "75%": round(float(d.loc["75%", col]), 4),
                    "max": round(float(d.loc["max", col]), 4),
                }
        out["missing"] = {str(c): int(df[c].isna().sum()) for c in df.columns}
        return out
    except Exception as e:
        logger.debug("describe 计算失败: %s", e)
        return {"error": f"描述统计失败: {e}"}


# ---------------- 相关性（真实计算） ----------------

def _compute_correlation(file: str) -> Optional[Dict[str, Any]]:
    path = _require_file(file)
    if not path:
        return {"error": "未提供有效数据文件"}
    try:
        df = load_dataframe(str(path))
        if df is None or df.empty:
            return {"error": "数据为空"}
        num = df.select_dtypes(include=[np.number])
        if num.shape[1] < 2:
            return {"error": "数值列不足 2 列，无法计算相关性"}
        corr = num.corr()
        # 只保留对角线以下的上三角之外的"成对"绝对值，挑出最大几对
        pairs = []
        cols = list(corr.columns)
        for i in range(len(cols)):
            for j in range(i + 1, len(cols)):
                v = corr.iloc[i, j]
                if not np.isnan(v):
                    pairs.append((cols[i], cols[j], round(float(v), 4)))
        pairs.sort(key=lambda x: abs(x[2]), reverse=True)
        return {"top_correlations": pairs[:8]}
    except Exception as e:
        logger.debug("correlation 计算失败: %s", e)
        return {"error": f"相关性计算失败: {e}"}


# ---------------- 顶层：计算并渲染注入块 ----------------

_RESOLVERS = {
    "fund_metrics": _compute_fund_metrics,
    "describe": _compute_describe,
    "correlation": _compute_correlation,
}

_OP_LABEL = {'mean': '平均值', 'sum': '总和', 'median': '中位数', 'std': '标准差', 'max': '最大值', 'min': '最小值'}


def compute_and_render(message: str, current_file: str = None) -> Dict[str, Any]:
    """
    计算类请求的顶层入口：
      返回 {"kind": <intent>|None, "block": str, "computed": dict|None, "text": str|None}
      - 未识别计算意图 → {"kind": None, "block": ""}
      - 识别但无文件 → block 提示需要文件（仍可用，但标注无法算真值）
      - 计算失败 → block 说明失败
    供 ai_assistant 注入 LLM 上下文（LLM 只解读真实数字）。
    """
    intent = infer_compute_intent(message)
    if not intent:
        return {"kind": None, "block": "", "computed": None, "text": None}
    if not _HAS_CORE:
        return {"kind": intent, "block": "# 提示：后端计算能力不可用，请勿进行数值推算；仅可基于通用方法讲解。", "computed": None, "text": None}

    # 纯数字列表运算：不需要文件，直接计算并返回人话答案
    if intent == 'numbers':
        computed = _compute_numbers(message)
        if computed and not computed.get("error"):
            op_name = _OP_LABEL.get(computed.get('operation'), '计算结果')
            nums_s = ', '.join(str(x) for x in computed['numbers'])
            text = f"📊 {op_name}：**{computed['result']}**\n\n数据（{computed['count']} 个）：{nums_s}\n计算方式：对以上数字求{op_name}。"
            return {
                "kind": intent, "block": "", "computed": computed, "text": text,
            }
        return {
            "kind": intent,
            "block": f"# 提示：{computed.get('error')}。请只做方法讲解，切勿编造数字。",
            "computed": None, "text": None,
        }

    resolver = _RESOLVERS.get(intent)
    if not resolver:
        # 已识别计算意图但暂无自动计算器：必须显式告知 LLM 不可计算，
        # 而不是静默不注入（否则模型会在无真值的情况下自行推算/编造数字）
        return {
            "kind": intent,
            "block": ("# 提示：用户请求的计算（" + intent + "）后端暂无自动计算器，无法提供真实数值。\n"
                      "请只做方法与步骤讲解（需要哪些列、如何分组、什么口径），"
                      "明确告知用户\"暂无法直接给出实值，请在对应分析页操作或补充数据后再算\"；"
                      "严禁编造任何具体数值、p 值或结论。"),
            "computed": None, "text": None,
        }

    computed = resolver(current_file or "",)
    if not current_file:
        return {
            "kind": intent,
            "block": "# 提示：当前未选择数据文件，无法计算真实数值。请基于方法讲解，明确说明暂无实值，切勿编造数字。",
            "computed": None, "text": None,
        }

    if not computed or computed.get("error"):
        err = (computed or {}).get("error", "未知错误")
        return {
            "kind": intent,
            "block": f"# 提示：真实计算失败（{err}）。请只做方法讲解，明确说明无法提供本次实值，切勿编造数字。",
            "computed": None, "text": None,
        }

    block = "# 后端真实计算结果（必须基于以下数值解读，禁止模型自行推算）\n```json\n" + \
            json.dumps(computed, ensure_ascii=False, default=str)[:6000] + "\n```"
    return {"kind": intent, "block": block, "computed": computed, "text": None}


if __name__ == "__main__":
    import sys
    msg = sys.argv[1] if len(sys.argv) > 1 else "帮我算一下夏普比率和最大回撤"
    f = sys.argv[2] if len(sys.argv) > 2 else ""
    r = compute_and_render(msg, f)
    print("kind:", r["kind"])
    print(r["block"][:800])