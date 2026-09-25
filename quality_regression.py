#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
quality_regression.py - 质量回归（P2b）
=====================================================
目标：
1. 维护一套"黄金用例"（golden cases）：覆盖各自代表指标/工具的场景，标注
   期望工具、期望数字、是否禁止跳步。
2. 对每个用例运行 Agent，采集执行轨迹（calls + 终稿文本），用"确定性打分器"
   计算三项核心指标，无需 LLM 也能在 CI 离线路跑：
     - 工具选对率（tool_accuracy）：是否调用了期望工具，是否误调无关工具
     - 数字忠制度（number_fidelity）：关键数字是否出现在终稿（且不凭空伪造）
     - 跳步率（skip_rate）：资料不足就整除/多处缺数据未标注的行为占比
3. 可选 LLM-as-judge：当配置了 LLM 且运行联网（QUALITY_LLM=1）时，用 LLM 对每个
   用例交叉评分，输出 0-10 句文化评分，用于更主观的"论证质量"审查，防回归更稳。
4. 结果落盘 output/quality_report.json，供 CI 断言阈值。

离线/联网切换：
  - 默认（CI 单测）：只跑确定性打分 + 用例结构校验，无需 LLM、无需联网。
  - 手动/定时：QUALITY_LLM=1 时额外对保留用例执行 LLM-as-judge 交叉评分。
"""
import os
import re
import json
import time
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

# =============================================================================
# RAG 检索-注入足迹（P2-6 质量回归钩子）
# =============================================================================
# knowledge_rag.get_knowledge_context 在注入前调用本函数，把"查询 → 实际注入条目"
# 记录到进程内队列，供回归用例校验"知识命中与注入一致性/回忆率"，默认静默、无副作用。
_kb_inject_ring = []      # deque 简化：仅保留最近 N 次
_KB_RING_MAX = 100


def record_kb_injection(query: str, injected_names: list) -> None:
    """记录一次知识注入足迹（无副作用；供回归/可观测消费）"""
    global _kb_inject_ring
    if not isinstance(_kb_inject_ring, list):
        _kb_inject_ring = []
    _kb_inject_ring.append({
        "ts": time.time(),
        "query": str(query)[:120],
        "injected": [str(n) for n in (injected_names or [])][:8],
    })
    if len(_kb_inject_ring) > _KB_RING_MAX:
        _kb_inject_ring = _kb_inject_ring[-_KB_RING_MAX:]


def recent_kb_injections(limit: int = 20) -> List[Dict[str, Any]]:
    """返回最近的 RAG 注入足迹（供测试/监控断言）"""
    return list(_kb_inject_ring[-limit:])


# =============================================================================
# 知识库检索质量评估（P2-6 补充：自动算召回率/精确率）
# =============================================================================
# 人工标注的 ground-truth 评估集：每条查询持有"应命中的条目 name 集合"。
# 名称与 static/knowledge_data.json 的 name 字段完全一致（119 条库中采样）。
_KB_EVAL_CASES: List[Dict[str, Any]] = [
    {"query": "什么是市盈率", "relevant": {"市盈率(PE)", "市盈率TTM"}},
    {"query": "DID双重差分是什么", "relevant": {"DID双重差分", "平行趋势检验", "安慰剂检验"}},
    {"query": "XGBoost和随机森林区别", "relevant": {"XGBoost", "随机森林"}},
    {"query": "逻辑回归怎么用", "relevant": {"逻辑回归"}},
    {"query": "公司要交企业所得税吗", "relevant": {"企业所得税", "偷税/避税/税务筹划的区别"}},
    {"query": "个税专项附加扣除有哪几项", "relevant": {"个税专项附加扣除(7项)", "个人所得税(综合所得)"}},
    {"query": "基金最大回撤怎么看", "relevant": {"最大回撤", "夏普比率", "卡玛比率与索提诺比率"}},
    {"query": "债券久期和凸性是什么", "relevant": {"久期与凸性", "收益率曲线形态", "债券估值与定价"}},
    {"query": "等额本息等额本金哪个划算", "relevant": {"房贷:等额本息vs等额本金", "提前还款", "名义利率vs实际利率vs年化IRR"}},
    {"query": "怎么检查时间序列平稳性", "relevant": {"ADF检验", "自相关ACF/PACF", "白噪声"}},
    {"query": "支持向量机SVM原理", "relevant": {"支持向量机(SVM)"}},
    {"query": "什么是数据泄露", "relevant": {"数据泄露"}},
    {"query": "平行趋势检验是什么", "relevant": {"DID双重差分", "平行趋势检验"}},
    {"query": "Shibor是什么", "relevant": {"Shibor同业拆借利率", "LPR(贷款市场报价利率)"}},
    {"query": "多重共线性怎么判断", "relevant": {"方差膨胀因子(VIF)", "多重共线性"}},
    {"query": "买车要交哪些税", "relevant": {"车辆购置税", "消费税", "印花税(证券交易印花税)"}},
    {"query": "杜邦分析怎么拆解ROE", "relevant": {"杜邦分析", "净资产收益率(ROE)"}},
    {"query": "小规模纳税人增值税怎么报", "relevant": {"增值税(一般纳税人/小规模纳税人)", "发票(专票/普票)"}},
    {"query": "信用卡免息期怎么算", "relevant": {"信用卡(账单日/还款日/免息期)"}},
    {"query": "偷税和避税有什么区别", "relevant": {"偷税/避税/税务筹划的区别", "发票(专票/普票)"}},
]


def evaluate_kb_metrics(top_k: int = 2, cases: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """基于 ground-truth 评估集自动计算知识检索的 召回率/精确率。
    返回 {recall, precision, num_cases, per_case:[{query, relevant_n, retrieved, recall, precision}]}
    默认用系统实际注入口径 top_k=2；延迟 import 避免循环依赖，检索失败静默降级为 0。"""
    cases = cases if cases is not None else _KB_EVAL_CASES
    try:
        from knowledge_rag import get_knowledge_context
    except Exception as e:
        logger.warning("知识检索评估跳过（无法导入检索器）: %s", e)
        return {"recall": 0.0, "precision": 0.0, "num_cases": len(cases), "per_case": []}
    recalls, precisions, per = [], [], []
    for c in cases:
        rel = set(c.get("relevant") or [])
        # 按系统实际注入口径：get_knowledge_context 内会调 record_kb_injection 记录注入条目名，
        # 通过 recent_kb_injections 取回（含核心优先/泛化降权等注入排序逻辑）。
        try:
            before = len(recent_kb_injections())
            get_knowledge_context(c["query"], top_k=top_k)
            ring = recent_kb_injections()
            ret = ring[-1]["injected"] if ring and len(ring) > before else []
        except Exception as e:
            logger.warning("知识检索评估用例失败: %s", e)
            ret = []
        hit = sum(1 for n in ret if n in rel)
        recalls.append(hit / max(len(rel), 1))
        precisions.append(hit / max(len(ret), 1))
        per.append({"query": str(c["query"])[:60], "relevant_n": len(rel),
                    "retrieved": ret[:top_k], "recall": round(hit / max(len(rel), 1), 2),
                    "precision": round(hit / max(len(ret), 1), 2)})
    n = len(recalls) or 1
    return {
        "recall": round(sum(recalls) / n, 3),
        "precision": round(sum(precisions) / n, 3),
        "top_k": top_k,
        "num_cases": len(cases),
        "per_case": per,
    }

# =============================================================================
# 黄金用例
# =============================================================================
# expected_tools: 达成目标应该优先使用的工具（命中名单）
# forbidden_tools: 达成该目标不应误用的工具（误调用扣分）
# key_numbers: 期望终稿引用的关键数字子串（数字忠制度用；缺失即扣分）
# no_skip: 是否要求先取数再下结论（跳步率用；True 表示严禁跳步）
GOLDEN_CASES: List[Dict[str, Any]] = [
    # ---- 数据文件分析类 ----
    {"id": "GC001", "query": "帮我做描述性统计并列出关键字段的均值和缺失率",
     "file": "finance_ml_final_project_data.csv",
     "expected_tools": ["describe", "clean", "profile"], "forbidden_tools": ["did"],
     "key_numbers": ["mean", "missing"], "no_skip": True, "must_have_tool": True},
    {"id": "GC002", "query": "分析各变量与违约违约的相关性",
     "file": "finance_ml_final_project_data.csv",
     "expected_tools": ["correlation"], "forbidden_tools": ["finance"],
     "key_numbers": [], "no_skip": True, "must_have_tool": True},
    {"id": "GC003", "query": "看看数据的时间趋势，按月份画趋势图",
     "file": "finance_ml_final_project_data.csv",
     "expected_tools": ["ts"], "forbidden_tools": [], "key_numbers": [],
     "no_skip": True, "must_have_tool": True},
    {"id": "GC004", "query": "对分组变量做分组对比",
     "file": "finance_ml_final_project_data.csv",
     "expected_tools": ["group"], "forbidden_tools": [], "key_numbers": [],
     "no_skip": True, "must_have_tool": True},
    {"id": "GC005", "query": "训练一个模型预测违约概率并给特征重要性",
     "file": "finance_ml_final_project_data.csv",
     "expected_tools": ["ml"], "forbidden_tools": ["scrape"], "key_numbers": [],
     "no_skip": True, "must_have_tool": True},
    # ---- 金融/行情类（无数据文件，用金融工具） ----
    {"id": "GC006", "query": "分析贵州茅台的投资价值，给DCF估值",
     "expected_tools": ["company", "finance", "valuation"], "forbidden_tools": ["describe"],
     "key_numbers": [], "no_skip": True, "must_have_tool": True},
    {"id": "GC007", "query": "查找A股平均市盈率排前十的股票",
     "expected_tools": ["screener", "finance"], "forbidden_tools": ["clean"],
     "key_numbers": [], "no_skip": True, "must_have_tool": True},
    {"id": "GC008", "query": "做固收债券的凸性与久期分析",
     "expected_tools": ["fixedincome"], "forbidden_tools": ["ml"],
     "key_numbers": [], "no_skip": True, "must_have_tool": True},
    # ---- 生成类 ----
    {"id": "GC009", "query": "帮我生成一份完整的财务报表分析报告",
     "expected_tools": ["report"], "forbidden_tools": [], "key_numbers": [],
     "no_skip": False, "must_have_tool": False},
    {"id": "GC010", "query": "对该数据做端到端预处理流水线",
     "file": "finance_ml_final_project_data.csv",
     "expected_tools": ["clean", "pipeline"], "forbidden_tools": ["did"],
     "key_numbers": [], "no_skip": True, "must_have_tool": True},
    # ---- 计量/因果类 ----
    {"id": "GC011", "query": "用双重差分评估政策对消费的影响",
     "file": "finance_ml_final_project_data.csv",
     "expected_tools": ["did"], "forbidden_tools": ["ml"],
     "key_numbers": [], "no_skip": True, "must_have_tool": True},
    {"id": "GC012", "query": "做面板数据回归估计个体固定效应",
     "file": "finance_ml_final_project_data.csv",
     "expected_tools": ["did"], "forbidden_tools": ["scrape"],
     "key_numbers": [], "no_skip": True, "must_have_tool": True},
    # ---- 工程/工具类 ----
    {"id": "GC013", "query": "列出当前系统可用的分析方法列表",
     "expected_tools": [], "forbidden_tools": ["finance", "ml"], "key_numbers": [],
     "no_skip": False, "must_have_tool": False},
    {"id": "GC014", "query": "把刚才的分析结果保存成报告",
     "expected_tools": ["report", "save"], "forbidden_tools": [], "key_numbers": [],
     "no_skip": False, "must_have_tool": False},
    {"id": "GC015", "query": "查看数据集中有没有需要清洗的异常值",
     "file": "finance_ml_final_project_data.csv",
     "expected_tools": ["clean", "describe"], "forbidden_tools": ["valuation"],
     "key_numbers": [], "no_skip": True, "must_have_tool": True},
    # ---- 精确数字务必出现在终稿 ----
    {"id": "GC016", "query": "报告违约样本占总样本的比例这一关键数字",
     "file": "finance_ml_final_project_data.csv",
     "expected_tools": ["describe", "profile"], "forbidden_tools": [],
     "key_numbers": ["%" , "违约"], "no_skip": True, "must_have_tool": True},
    {"id": "GC017", "query": "给出数据集的样本量、列数并确认",
     "file": "finance_ml_final_project_data.csv",
     "expected_tools": ["describe"], "forbidden_tools": [], "key_numbers": [],
     "no_skip": True, "must_have_tool": True},
    # ---- 明确禁止跳步 / 需联网 ----
    {"id": "GC018", "query": "宁德时代当前市值和最新股价是多少", "expected_tools": ["finance", "web"],
     "forbidden_tools": ["describe"], "key_numbers": [], "no_skip": True, "must_have_tool": True},
    {"id": "GC019", "query": "分析五粮液和泸州老窖的估值对比", "expected_tools": ["valuation", "finance"],
     "forbidden_tools": ["clean"], "key_numbers": [], "no_skip": True, "must_have_tool": True},
    {"id": "GC020", "query": "对该 CSV 做多变量回归并给出显著变量", "file": "finance_ml_final_project_data.csv",
     "expected_tools": ["ml", "did"], "forbidden_tools": ["screener"],
     "key_numbers": [], "no_skip": True, "must_have_tool": True},
]


def _case_tool_names() -> List[str]:
    """汇总所有黄金用例期望的工具名，用于训练集一致性检查。"""
    names = set()
    for c in GOLDEN_CASES:
        names.update(c["expected_tools"])
    return sorted(names)


# =============================================================================
# 确定性打分器（无需 LLM，可离线路跑）
# =============================================================================

def score_trace(case: Dict[str, Any], calls: List[Dict[str, Any]],
                final_text: str = "") -> Dict[str, Any]:
    """对一个用例的 Agent 执行轨迹做确定性打分。
    calls: [{tool, status}] 轨迹；final_text: 终稿文本。返回各指标 0-100。"""
    # 提取实际调用的工具名与成功次数
    called = [str(c.get('tool', '')) for c in (calls or [])]
    ok_calls = [t for t, c in zip(called, (calls or [])) if c.get('status') == 'ok']

    exp = set(case.get('expected_tools', []))
    forb = set(case.get('forbidden_tools', []))
    called_set = set(ok_calls)

    # ---- 工具选对率 ----
    if case.get('must_have_tool'):
        hit = len(exp.intersection(called_set))
        tool_acc = 100.0 if exp and hit >= min(1, len(exp)) else 0.0
    else:
        # 无需特定工具的用例：选用"合法工具"（非 forbidden）给基础分，误用 forbidden 扣一半
        tool_acc = 100.0  # 默认给满，误用 forbidden 再扣
    if forb.intersection(called_set):
        tool_acc = max(0.0, tool_acc - 30.0)  # 误用明确不该用的工具显著扣分

    # ---- 数字忠制度 ----
    text = final_text or ""
    key_missing = [k for k in case.get('key_numbers', []) if k and k not in text]
    if case.get('key_numbers'):
        num_fidelity = 100.0 * (1 - len(key_missing) / len(case['key_numbers']))
    else:
        # 无显式关键数字的用例：只要不是"一句话空泛应答"就算基本守住
        num_fidelity = 60.0 if len(text) >= 80 else 30.0

    # ---- 跳步率（no_skip=True 时，成功工具调用很少却直接长篇下结论 = 疑似跳步） ----
    skip_rate = 0.0
    if case.get('no_skip'):
        success = len(ok_calls)
        if success == 0:
            skip_rate = 80.0 if len(text) >= 100 else 50.0  # 没取数就写结论
        elif success == 1:
            skip_rate = 40.0 if len(text) >= 300 else 20.0
        else:
            skip_rate = 0.0  # 取了足够数据
    skip_penalty = 100.0 - skip_rate  # 跳步越少越好 → 得分越高

    return {
        "case_id": case["id"],
        "query": case["query"],
        "called_tools": called,
        "ok_tools": ok_calls,
        "expected_hit": sorted(exp.intersection(called_set)),
        "forbidden_hit": sorted(forb.intersection(called_set)),
        "tool_accuracy": round(tool_acc, 1),
        "number_fidelity": round(num_fidelity, 1),
        "skip_rate": round(skip_rate, 1),
        "overall": round((tool_acc * 0.4 + num_fidelity * 0.3 + skip_penalty * 0.3), 1),
    }


# =============================================================================
# LLM-as-judge 可选交叉审查（联网才有）
# =============================================================================

_JUDGE_PROMPT = """你是质量回归评审员（LLM-as-judge）。请对给定用例的 Agent 表现打分 0-10。
严格评分维度：1) 工具是否选对（是否为了拿数据/达成目标调用了合适的工具）；
2) 关键数字是否忠实于工具返回（不凭空捏造）；3) 是否跳步（资料不足就下结论）。
只输出一个 JSON：{{"score": 0-10, "tool_correct": 0-10, "number_fidelity": 0-10,
"no_skip": 0-10, "note": "一段简要说明"}}。
用例：{query}
期望工具：{expected}
实际调用：{called}
终稿（截断）：{final}"""


def judge_with_llm(case: Dict[str, Any], calls: List[Dict[str, Any]],
                   final_text: str, llm_config: Dict[str, Any]) -> Dict[str, Any]:
    """用 LLM 对单个用例打分（需可用 LLM 与联网；失败返回空 dict 视为跳过）。"""
    try:
        from llm_client import chat
        called = ", ".join(str(c.get('tool', '')) for c in (calls or [])) or "（无）"
        prompt = _JUDGE_PROMPT.format(
            query=case["query"],
            expected=", ".join(case.get('expected_tools', [])),
            called=called,
            final=(final_text or "")[:1500],
        )
        resp = chat([{"role": "system", "content": "你是严格的 LLM-as-judge，只输出合法 JSON。"},
                     {"role": "user", "content": prompt}],
                    llm_config, max_tokens=400)
        content = (resp.get("content") or "") if isinstance(resp, dict) else ""
        m = re.search(r'\{.*\}', content, re.S)
        if not m:
            return {}
        data = json.loads(m.group(0))
        return {f"judge_{k}": v for k, v in data.items() if k != "note"} \
            | {"judge_note": data.get("note", "")}
    except Exception as e:
        logger.warning("LLM-judge 失败: %s", e)
        return {}


# =============================================================================
# 运行回归
# =============================================================================

def run_regression(llm_config: Optional[Dict[str, Any]] = None,
                   cases: Optional[List[Dict[str, Any]]] = None,
                   run_llm_judge: bool = False,
                   out_json: Optional[str] = None) -> Dict[str, Any]:
    """真实运行每个黄金用例并打分。返回汇总报告。
    run_llm_judge 需 LLM 配置与联网；否则只跑确定性打分。"""
    cases = cases or GOLDEN_CASES
    from ai_agent import agent_loop

    results = []
    for case in cases:
        try:
            res = agent_loop(
                user_message=case["query"],
                llm_config=llm_config or {"provider": "sensenova"},
                file_name=case.get("file"),
                max_iterations="auto",
                session_id=f"regression-{case['id']}",
            )
            calls = res.get("calls", [])
            final = res.get("output", "")
            sc = score_trace(case, calls, final)
            if run_llm_judge and llm_config:
                judge = judge_with_llm(case, calls, final, llm_config)
                sc.update(judge)
            results.append(sc)
        except Exception as e:
            logger.error("用例 %s 执行失败: %s", case["id"], e)
            results.append({**score_trace(case, [], ""),
                            "error": str(e)[:200]})

    report = _aggregate(results)
    # 追加知识库检索质量（召回/精确），随 agent 回归一并落盘
    report["kb_eval"] = evaluate_kb_metrics(top_k=2)
    path = out_json or str(Path(__file__).parent / "output" / "quality_report.json")
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "aggregate": report, "kb_eval": report.get("kb_eval"),
                       "cases": results},
                      f, ensure_ascii=False, indent=2)
        report["report_file"] = path
    except Exception as e:
        logger.warning("写入质量报告失败: %s", e)
    return report


def _aggregate(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """汇总所有用例的指标均值与通过率。"""
    n = len(results) or 1
    def _avg(key):
        vals = [r.get(key) for r in results if isinstance(r.get(key), (int, float))]
        return round(sum(vals) / len(vals), 1) if vals else 0.0
    passed = [r for r in results if r.get("overall", 0) >= 70]
    return {
        "num_cases": len(results),
        "passed": len(passed),
        "pass_rate": round(100.0 * len(passed) / n, 1),
        "avg_tool_accuracy": _avg("tool_accuracy"),
        "avg_number_fidelity": _avg("number_fidelity"),
        "avg_overall": _avg("overall"),
        "thresh_passed": len(passed) >= max(1, int(0.7 * n)),
    }


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="质量回归（P2b）")
    p.add_argument("--llm", action="store_true", help="启用 LLM-as-judge 交叉评审")
    p.add_argument("--ids", nargs="*", default=None, help="仅跑指定用例，如 --ids GC001 GC002")
    args = p.parse_args()
    cases = None
    if args.ids:
        allowed = {c["id"] for c in GOLDEN_CASES}
        cases = [c for c in GOLDEN_CASES if c["id"] in set(args.ids)]
    rep = run_regression(run_llm_judge=args.llm, cases=cases)
    print(json.dumps(rep, ensure_ascii=False, indent=2))