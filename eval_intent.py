#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eval_intent.py - 意图/路由评测集（⑦）

用途：每次改动 Prompt / 意图体系 / 模型后，回跑一遍评估集，衡量分类质量，
避免"优化=盲调"。含难例：模糊指代、多意图、金融陷阱题、边界歧义。

用法：python eval_intent.py   → 打印准确率与逐个错例
      pytest tests/test_intent_eval.py  → 作为回归纳入测试
"""

import json
import logging

logger = logging.getLogger(__name__)

# 每项：{"q":问题, "intent":期望意图或可接受集合}
EVAL_SET = [
    # ---- 查数 NLQ ----
    {"q": "查一下上个月的销售额", "intent": "nlq"},
    {"q": "最近30天收益率最高的基金是哪个", "intent": "nlq"},
    {"q": "这个基金的年化收益率是多少", "intent": "nlq"},
    {"q": "腾讯最新股价多少", "intent": "nlq"},
    {"q": "看看茅台最新市值", "intent": ["nlq", "analysis"]},
    # ---- 分析 ANALYSIS ----
    {"q": "为什么最大回撤这么大", "intent": "analysis"},
    {"q": "帮我分析一下这个数据", "intent": "analysis"},
    {"q": "解读一下这只基金的业绩表现", "intent": "analysis"},
    {"q": "这个模型训练出来准不准", "intent": "analysis"},
    # ---- 实体感知（公司/股票 + 口语问询，2026-08 实测难例）----
    {"q": "宁德时代怎么样", "intent": "analysis"},
    {"q": "五粮液最近走势怎么样", "intent": "analysis"},
    {"q": "比亚迪和宁德时代哪个好", "intent": "analysis"},
    {"q": "茅台值不值得买", "intent": ["analysis", "agent", "computation"]},
    # ---- 行业/板块研究（2026-08 实测难例）----
    {"q": "分析游戏行业", "intent": "analysis"},
    {"q": "全面分析电器行业", "intent": ["agent", "analysis"]},
    {"q": "电器行业前景怎么样", "intent": ["analysis", "agent"]},
    {"q": "游戏行业竞争格局如何", "intent": ["analysis", "agent"]},
    {"q": "半导体板块有投资机会吗", "intent": ["analysis", "agent"]},
    # ---- 数据质量/计量（2026-08 实测难例）----
    {"q": "这份数据缺失严重吗", "intent": ["analysis", "nlq"]},
    {"q": "数据里有多少异常值", "intent": ["analysis", "nlq"]},
    {"q": "做个DID政策评估", "intent": "analysis"},
    {"q": "跑一下双重差分回归", "intent": "analysis"},
    # ---- 可视化 ----
    {"q": "画个柱状图", "intent": "visualization"},
    {"q": "用折线图展示趋势", "intent": "visualization"},
    {"q": "热力图看一下相关性", "intent": "visualization"},
    {"q": "帮我把销量画成折线图", "intent": "visualization"},
    # ---- 知识问答 ----
    {"q": "什么是久期？", "intent": "knowledge"},
    {"q": "解释一下夏普比率", "intent": "knowledge"},
    {"q": "DID 是什么", "intent": "knowledge"},
    {"q": "PSI 是什么意思", "intent": "knowledge"},
    {"q": "什么是数据分析", "intent": "knowledge"},
    # ---- 操作 ----
    {"q": "怎么上传文件", "intent": "operation"},
    {"q": "在哪里生成图表", "intent": "operation"},
    {"q": "怎么导出报告", "intent": "operation"},
    {"q": "怎么把图表保存下来", "intent": "operation"},
    # ---- 故障 ----
    {"q": "报错了怎么办", "intent": "troubleshoot"},
    {"q": "页面白屏", "intent": "troubleshoot"},
    # ---- 闲聊 ----
    {"q": "你好", "intent": "chat"},
    {"q": "谢谢", "intent": "chat"},
    {"q": "你好呀", "intent": "chat"},
    # ---- 边界歧义（难点，考验少被误判）----
    {"q": "帮我看看收益率", "intent": "nlq", "note": "要数据，不是知识"},          # 边界关键案例
    {"q": "收益率是什么意思", "intent": "knowledge"},                            # vs 上面
    {"q": "算一下这个基金的总收益率", "intent": "computation"},                   # 计算类
    {"q": "算一下这笔投资的年化收益率", "intent": "computation"},
    {"q": "夏普比率怎么算的", "intent": "knowledge"},                            # 公式讲解→知识
    {"q": "分析一下", "intent": "analysis", "note": "无目标分析：路由为 analysis，由上层引导选文件"},
    # ---- 模糊指代（难点）----
    {"q": "它呢", "intent": None, "note": "指代，应走改写/澄清，不硬猜"},
    # ---- 多意图/复杂（难点）----
    {"q": "综合分析这只基金的风险和收益，帮我看看要不要买", "intent": ["analysis", "agent", "computation"]},
]


def _load_user_cases(path: str = 'output/bad_cases/intent_cases.jsonl', limit: int = 50) -> list:
    """动态并入生产回流难例（record_intent_case 落盘）。

    数据闭环的"人工标注"环节：JSONL 中每条难例可人工补上 "expected": "意图名" 字段，
    补标后自动升级为**硬回归用例**参与严格判定；未标注的只要求不误判为 chat。"""
    cases = []
    try:
        with open(path, encoding='utf-8') as f:
            for line in f.readlines()[-limit:]:
                try:
                    item = json.loads(line)
                    q = item.get('q')
                    if not q:
                        continue
                    expected = item.get('expected')
                    if expected:
                        cases.append({'q': q, 'intent': expected,
                                      'note': 'user-case（已人工标注 → 硬回归）'})
                    else:
                        cases.append({'q': q, 'intent': None,
                                      'note': 'user-case（难例回流，不硬判；补 expected 字段即升级硬回归）'})
                except Exception:
                    continue
    except OSError:
        pass
    return cases


def _ok(predicted: str, expected) -> bool:
    if expected is None:
        # 指代等：不要求命中特定意图，只要求不误判成明显错的闲聊
        return predicted not in ("chat",)
    if isinstance(expected, list):
        return predicted in expected
    return predicted == expected


def run_eval(verbose: bool = True):
    """运行评测，返回 (accuracy, fails)。

    评测集 = 内置 EVAL_SET + 生产回流难例。

    准确率口径（P2 修复）：分母为**本次实际评测的全部用例数**。
    此前分母只用 `len(EVAL_SET)`，而分子 `pass_n` 把回流难例也算进去，
    于是出现 112.5%（54/48）这种"恒真"指标（`acc >= 0.95` 永远通过），
    指标失去回归意义。同时打印硬/软用例分项，便于定位退化来源：

      · 硬用例 = 有明确 expected（内置集 + 已人工标注的回流难例）
      · 软用例 = 回流难例未标注（只要求"未误判成 chat"）
    """
    from intent_router import classify_intent, rewrite_query

    builtin_n = len(EVAL_SET)
    cases = EVAL_SET + _load_user_cases()
    hist = [{"entities": {"indicator": ["夏普比率"], "target": ["基金"]}}]

    pass_n = 0
    hard_pass = hard_n = 0
    soft_pass = soft_n = 0
    fails = []
    for idx, item in enumerate(cases):
        q = item["q"]
        # 模拟改写后分类（含指代项用历史）
        qw = rewrite_query(q, hist) if item.get("intent") is None else q
        predicted = classify_intent(qw)["intent"]
        ok = _ok(predicted, item["intent"])
        if item["intent"] is None:
            soft_n += 1
            soft_pass += ok
        else:
            hard_n += 1
            hard_pass += ok
        if ok:
            pass_n += 1
        else:
            src = "内置" if idx < builtin_n else "回流难例"
            fails.append({"q": q, "expect": item["intent"], "pred": predicted,
                          "note": item.get("note", ""), "src": src})
        if verbose:
            tag = "✔" if ok else "✘"
            print(f"{tag} {q[:24]:<26} → {predicted}{' (期望:'+str(item['intent'])+')' if not ok else ''}")

    acc = pass_n / len(cases) if cases else 0.0
    print(f"\n准确率: {pass_n}/{len(cases)} = {acc*100:.1f}%"
          f"  ｜硬用例 {hard_pass}/{hard_n}"
          f"  ｜软用例(仅要求非闲聊) {soft_pass}/{soft_n}")
    if fails:
        print("失败项:")
        for f in fails:
            print(f"  ✘ [{f['src']}] {f['q']}  期望={f['expect']} 实际={f['pred']} {f['note']}")
    return acc, fails


if __name__ == "__main__":
    run_eval()