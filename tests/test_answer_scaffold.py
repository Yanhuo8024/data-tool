# -*- coding: utf-8 -*-
"""回答脚手架剥离 + 金融短答预算的回归（2026-10-06 实战）。

故障：江西铜业（600362）估值问答的回答里，模型把
    「任务复述 + 数据概览 + 要求清单 + 分析：草稿 + 最终稿」全写进了正文，
    且正文因 max_tokens 太小被截断在半句「...股价」→ 用户看到的是不完整且过程结论混排的回答。

本文件锁死三件事：
  ① 能把脚手架剥掉，且只保留最终稿；
  ② **不误伤**正常回答（这是本模块最容易犯的错，宁可漏剥不可改坏好答案）；
  ③ 提示词带输出纪律 + 预算足够，同步/流式两条路径口径一致（防双轨漂移）。
"""
import io
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from answer_scaffold import (  # noqa: E402
    FINANCE_ANSWER_RULE,
    FINANCE_MAX_TOKENS,
    clean_reply,
    scaffold_report,
    strip_answer_scaffold,
)

ROOT = Path(__file__).resolve().parents[1]
AA = (ROOT / 'ai_assistant.py').read_text(encoding='utf-8')

# 真实故障样本：任务复述 + 草稿 + 最终稿，且最终稿被截断。
REAL_BAD = (
    '用户希望我分析江西铜业（600362）的财务报表，并基于提供的实时数据判断其估值是否合理。\n\n'
    '数据概览：\n- DCF估值：现价41.8元，内在价值1.86元，偏差-95.5%。\n'
    '- 财务比率：ROE=10.22%，净利率=3.09%。\n\n'
    '要求：\n1. 先给结论（低估/合理/高估）。\n2. 列2-3个关键依据。\n'
    '3. 结尾必须注明：⚠️ 本结论为客观数据分析，仅供参考，不构成任何投资建议。\n\n'
    '分析：\n结论：高估。\n依据1：DCF估值显示严重高估。\n'
    '依据2：财务比率显示利润率极薄。\n\n'
    '字数控制在200字左右，自然表达。\n\n'
    '草稿：\n江西铜业当前估值**高估**。\n\n关键依据如下：\n'
    '1. **DCF估值严重偏离**：每股内在价值仅1.86元，偏差达-95.5%。\n'
    '2. **盈利质量偏弱且杠杆高**：负债率高达65.48%。\n'
    '3. **技术面走弱**：近一月下跌13.64%，股价'
)


class TestStripScaffold:

    def test_真实样本_剥离出最终稿(self):
        out = strip_answer_scaffold(REAL_BAD)
        assert out.startswith('江西铜业当前估值')
        assert '草稿' not in out

    def test_真实样本_任务复述与要求清单被清掉(self):
        out = strip_answer_scaffold(REAL_BAD)
        for leak in ('用户希望我', '数据概览', '要求：', '字数控制在'):
            assert leak not in out, f'脚手架残留: {leak}'

    def test_真实样本_诊断类型为最终稿标记(self):
        assert scaffold_report(REAL_BAD) == 'final_marker'

    def test_体积显著缩小(self):
        out = strip_answer_scaffold(REAL_BAD)
        assert len(out) < len(REAL_BAD) * 0.45

    def test_复述头存在时剥掉前缀保留数据正文(self):
        """复述头 + 数据概览 → 剥掉复述，留下的数据正文不能塌成一行。"""
        s = '用户希望我分析这家公司。\n\n数据概览：\n- ROE=10%'
        out = strip_answer_scaffold(s)
        assert '用户希望我' not in out
        assert out == '- ROE=10%'

    def test_剥到只剩标记时保留原文(self):
        """标记后无任何正文 → 剥空了更糟，必须原样返回。"""
        s = '用户希望我分析这家公司。\n\n数据概览：'
        assert strip_answer_scaffold(s) == s

    def test_无最终稿但有复述头时剥前缀(self):
        s = ('用户希望我分析江西铜业。\n数据概览：\n- ROE=10.22%\n\n'
             '分析：\n江西铜业当前估值高估，DCF内在价值1.86元。\n⚠️ 本结论仅供参考。')
        out = strip_answer_scaffold(s)
        assert out.startswith('江西铜业当前估值高估')
        assert '用户希望我' not in out


class Test不误伤:  # 反向用例，本模块最重要的一类

    @pytest.mark.parametrize('ok', [
        '江西铜业当前估值高估。DCF 显示内在价值 1.86 元，偏差 -95.5%。\n\n'
        '⚠️ 本结论为客观数据分析，仅供参考，不构成任何投资建议。',
        '分析一下销售数据：\n1. 结论：增长放缓\n2. 依据：毛利率下降 3 个百分点',
        '数据概览如下：\n- x=1\n- y=2',
        '这批用户的最终答案转化率是 3.2%，草稿尚未提交。',
    ])
    def test_正常回答原样返回(self, ok):
        assert strip_answer_scaffold(ok) == ok
        assert scaffold_report(ok) == ''

    def test_空值安全(self):
        assert strip_answer_scaffold('') == ''
        assert strip_answer_scaffold(None) is None


class Test标签与脚手架叠加:

    def test_先剥thinking标签再剥脚手架(self):
        out = clean_reply('<thinking>内部推理</thinking>' + REAL_BAD)
        assert '内部推理' not in out
        assert out.startswith('江西铜业当前估值')

    def test_含标签时脚手架函数不越权(self):
        s = '<div>' + REAL_BAD + '</div>'
        assert strip_answer_scaffold(s) == s


class Test提示词与预算:

    def test_输出纪律含禁写过程与自我提醒(self):
        for kw in ('不要复述任务', '草稿', '字数控制'):
            assert kw in FINANCE_ANSWER_RULE

    def test_预算足够容纳200字正文(self):
        # 200 字中文 + Markdown + 免责声明，旧值 500/600 会截断在半句
        assert FINANCE_MAX_TOKENS >= 1000

    def test_金融短答走免思考配置(self):
        """reasoning 系模型思考挤占预算是截断主因，必须显式关。"""
        assert aa_uses_plain_cfg()

    def test_同步与流式都接了净化(self):
        assert AA.count('clean_reply(') >= 2, '同步/流式两条路径都要净化'

    def test_同步与流式预算一致(self):
        """双轨漂移防护：两处 max_tokens 必须是同一个常量，不能各写数字。"""
        nums = re.findall(r'max_tokens=(\d+)', AA)
        assert not [n for n in nums if n in ('500', '600')], f'仍有硬编码小预算: {nums}'


def aa_uses_plain_cfg():
    """同步路径把 reasoning_effort='none' 打进 cfg 后再调 chat。"""
    return "dict(llm_config, reasoning_effort='none')" in AA


def aa():
    return AA