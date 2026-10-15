# -*- coding: utf-8 -*-
"""derive_chain：衍生数字计算链（2026-10-10 P0-2）。"""

import derive_chain as dc


class Test基本推导:

    def test_百分比除法链(self):
        """108% ≈ 57 ÷ 52.48（第一轮），57 = 1418.78 × 4%（第二轮链式）。"""
        out = dc.compute(
            verified=['52.48亿', '1418.78亿', '4%'],
            unverified=['108%', '57亿'],
        )
        hits = {d['value_text']: d for d in out}
        assert '57亿' in hits, '57 = 1418.78 × 4% 必须命中'
        assert '4%' in hits['57亿']['formula'] or '1418.78' in hits['57亿']['formula']
        assert '108%' in hits, '108% 必须命中'
        # 链式：108% 的公式里应引用 57
        assert '57' in hits['108%']['formula']
        # 兼容 fact_triplet：value_low/high 可被 _matches_derived 命中
        assert hits['108%']['value_low'] is not None

    def test_普通除法(self):
        out = dc.compute(verified=['300亿', '7亿'], unverified=['42.9倍'])
        assert any(d['value_text'] == '42.9倍' for d in out)

    def test_加减(self):
        out = dc.compute(verified=['120亿', '45亿'], unverified=['75亿'])
        assert any(d['value_text'] == '75亿' for d in out)


class Test防误报:

    def test_年份不参与(self):
        out = dc.compute(verified=['2024', '300亿'], unverified=['1724亿'])
        assert not any(d['value_text'] == '1724亿' for d in out), '2024+300 不允许命中'

    def test_无匹配返回空(self):
        assert dc.compute(['3.14亿'], ['999%']) == []

    def test_空输入(self):
        assert dc.compute([], []) == []

    def test_零除保护(self):
        out = dc.compute(verified=['0', '5亿'], unverified=['1'])
        # 5/0 不崩即可，具体是否命中不重要
        assert isinstance(out, list)
