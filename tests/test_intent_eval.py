#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""评测集回归（⑦）：保证意图/路由改动不会回退分类质量"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_intent_eval_accuracy():
    from eval_intent import run_eval
    acc, fails = run_eval(verbose=False)
    # 关键：准确率不得低于 95%，否则视为回归
    assert acc >= 0.95, f"意图评测准确率 {acc*100:.1f}% 低于 95%，失败: {fails}"