#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_fraud_rules.py — 贷前反欺诈规则引擎回归

覆盖：三类规则的命中/不命中、滑动窗口边界、时间解析容错、裁定分级、
阈值可覆盖、确定性（同输入同输出）、DataFrame 输入、渲染与规则清单。
"""
import datetime as dt

import pytest

import fraud_rules as F


def _rows_none():
    """无异常的正常申请流水（3 人、3 设备、各 1 次申请）。"""
    base = dt.datetime(2026, 3, 1, 10, 0, 0)
    return [
        {'applicant_id': 'A1', 'device_id': 'D1', 'apply_time': base.strftime('%Y-%m-%d %H:%M:%S'),
         'phone': '13800000001', 'employer': '甲公司', 'contact_phone': '13900000001'},
        {'applicant_id': 'A2', 'device_id': 'D2', 'apply_time': (base + dt.timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S'),
         'phone': '13800000002', 'employer': '乙公司', 'contact_phone': '13900000002'},
        {'applicant_id': 'A3', 'device_id': 'D3', 'apply_time': (base + dt.timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S'),
         'phone': '13800000003', 'employer': '丙公司', 'contact_phone': '13900000003'},
    ]


class TestInputRobustness:

    def test_空输入不报错(self):
        for v in (None, [], (), '', 0):
            r = F.review(v)
            assert r['ok'] is True
            assert r['n_rows'] == 0
            assert r['verdict'] == 'pass'
            assert r['hits'] == []

    def test_非字典元素被忽略(self):
        r = F.review([{'applicant_id': 'A1'}, 'not-a-dict', 123, None])
        assert r['n_rows'] == 1

    def test_支持_dataframe_输入(self):
        pd = pytest.importorskip('pandas')
        df = pd.DataFrame(_rows_none())
        r = F.review(df)
        assert r['ok'] is True
        assert r['n_rows'] == 3

    def test_缺字段的行被跳过而非报错(self):
        rows = [{'applicant_id': ''}, {'device_id': 'D9'}, {}]
        r = F.review(rows)
        assert r['ok'] is True
        assert r['verdict'] == 'pass'


class TestDeviceCluster:

    def test_同一设备多申请人命中高危(self):
        rows = [{'applicant_id': f'A{i}', 'device_id': 'DX'} for i in range(5)]
        h = F.check_device_cluster(rows, F.DEFAULT_THRESHOLDS)
        assert h is not None
        assert h['rule'] == 'device_cluster'
        assert h['level'] == 'HIGH'
        assert h['evidence'][0]['device'] == 'DX'
        assert len(h['evidence'][0]['applicants']) == 5

    def test_同一人多次申请同一设备不算聚集(self):
        """设备聚集看的是**不同申请人**数，同一人重复提交不应触发。"""
        rows = [{'applicant_id': 'A1', 'device_id': 'DX'} for _ in range(10)]
        assert F.check_device_cluster(rows, F.DEFAULT_THRESHOLDS) is None

    def test_阈值边界等于不触发(self):
        """默认阈值 3：恰好 3 人共用一台设备不触发，第 4 个才触发。"""
        th = F.DEFAULT_THRESHOLDS
        rows3 = [{'applicant_id': f'A{i}', 'device_id': 'DX'} for i in range(3)]
        rows4 = [{'applicant_id': f'A{i}', 'device_id': 'DX'} for i in range(4)]
        assert F.check_device_cluster(rows3, th) is None
        assert F.check_device_cluster(rows4, th) is not None

    def test_正常流水不命中(self):
        assert F.check_device_cluster(_rows_none(), F.DEFAULT_THRESHOLDS) is None


class TestVelocityBurst:

    def test_窗口内高频命中(self):
        base = dt.datetime(2026, 3, 1, 9, 0, 0)
        rows = [{'applicant_id': 'A1',
                 'apply_time': (base + dt.timedelta(hours=h)).strftime('%Y-%m-%d %H:%M:%S')}
                for h in (0, 1, 2, 3, 4)]
        h = F.check_velocity_burst(rows, F.DEFAULT_THRESHOLDS)
        assert h is not None
        assert h['level'] == 'HIGH'
        assert h['evidence'][0]['applies_in_window'] == 5

    def test_窗口外低频不命中(self):
        """默认 24h 窗口、上限 3 次：每天 1 次、连续 5 天的申请不算高频。"""
        base = dt.datetime(2026, 3, 1, 9, 0, 0)
        rows = [{'applicant_id': 'A1',
                 'apply_time': (base + dt.timedelta(days=d)).strftime('%Y-%m-%d %H:%M:%S')}
                for d in range(5)]
        assert F.check_velocity_burst(rows, F.DEFAULT_THRESHOLDS) is None

    def test_滑动窗口恰好越界不计数(self):
        """窗口 24h：第 1 次与第 5 次相隔恰好 24h，仍在窗口内（>> 比较）。"""
        base = dt.datetime(2026, 3, 1, 0, 0, 0)
        rows = [{'applicant_id': 'A1',
                 'apply_time': (base + dt.timedelta(hours=x)).strftime('%Y-%m-%d %H:%M:%S')}
                for x in (0, 6, 12, 18, 24)]
        h = F.check_velocity_burst(rows, F.DEFAULT_THRESHOLDS)
        assert h is not None
        assert h['evidence'][0]['applies_in_window'] == 5

    def test_无法解析的时间被排除且诚实标注(self):
        base = dt.datetime(2026, 3, 1, 9, 0, 0)
        rows = [{'applicant_id': 'A1', 'apply_time': (base + dt.timedelta(hours=i)).strftime('%Y-%m-%d %H:%M:%S')}
                for i in range(4)]
        rows += [{'applicant_id': 'A2', 'apply_time': '不是时间'}] * 3
        h = F.check_velocity_burst(rows, F.DEFAULT_THRESHOLDS)
        assert h is not None
        assert '无法解析' in h['note']
        assert h['evidence'][0]['applicant'] == 'A1'

    def test_全部时间无法解析则不命中(self):
        rows = [{'applicant_id': 'A1', 'apply_time': 'x'}] * 5
        assert F.check_velocity_burst(rows, F.DEFAULT_THRESHOLDS) is None

    @pytest.mark.parametrize('value', [
        1772000000.0,                                  # 数值时间戳
        dt.datetime(2026, 3, 1, 10, 0, 0),             # datetime 对象
        '2026-03-01 10:00:00',
        '2026-03-01 10:00',
        '2026-03-01',
        '2026/03/01 10:00:00',
        '2026/03/01',
        '2026-03-01T10:00:00',                         # ISO 格式
    ])
    def test_时间格式兼容(self, value):
        assert F._parse_ts(value) is not None

    @pytest.mark.parametrize('value', [None, '', '  ', 'nonsense', '2026年3月1日'])
    def test_非法时间返回_none(self, value):
        assert F._parse_ts(value) is None


class TestIdentityOverlap:

    def test_联系人被三人共现命中(self):
        rows = [{'applicant_id': f'A{i}', 'contact_phone': '13900000000'} for i in range(3)]
        h = F.check_identity_overlap(rows, F.DEFAULT_THRESHOLDS)
        assert h is not None
        assert h['level'] == 'MID'
        assert h['evidence'][0]['field'] == 'contact_phone'
        assert len(h['evidence'][0]['applicants']) == 3

    def test_单位共用命中(self):
        rows = [{'applicant_id': f'A{i}', 'employer': '某包装公司'} for i in range(4)]
        h = F.check_identity_overlap(rows, F.DEFAULT_THRESHOLDS)
        assert h is not None
        assert h['evidence'][0]['field'] == 'employer'

    def test_联系人为列表时逐个识别(self):
        rows = [
            {'applicant_id': 'A1', 'contact_phone': ['111', '222']},
            {'applicant_id': 'A2', 'contact_phone': ['222', '333']},
            {'applicant_id': 'A3', 'contact_phone': ['222']},
        ]
        h = F.check_identity_overlap(rows, F.DEFAULT_THRESHOLDS)
        assert h is not None
        vals = [e['value'] for e in h['evidence']]
        assert '222' in vals
        assert '111' not in vals

    def test_同一人多条申请不构成复用(self):
        rows = [{'applicant_id': 'A1', 'phone': '13800000000'} for _ in range(5)]
        assert F.check_identity_overlap(rows, F.DEFAULT_THRESHOLDS) is None

    def test_正常流水不命中(self):
        assert F.check_identity_overlap(_rows_none(), F.DEFAULT_THRESHOLDS) is None


class TestVerdict:

    def test_仅_mid_转人工抽查(self):
        rows = [{'applicant_id': f'A{i}', 'contact_phone': '13900000000'} for i in range(3)]
        r = F.review(rows)
        assert r['verdict'] == 'manual_check'
        assert r['n_high'] == 0
        assert r['n_mid'] == 1
        assert r['handoff'] is True

    def test_有_high_则转人工复核(self):
        rows = [{'applicant_id': f'A{i}', 'device_id': 'DX'} for i in range(5)]
        r = F.review(rows)
        assert r['verdict'] == 'reject_review'
        assert r['n_high'] == 1
        assert '不得自动通过' in r['action']

    def test_无命中直接放行(self):
        r = F.review(_rows_none())
        assert r['verdict'] == 'pass'
        assert r['handoff'] is False
        assert r['hits'] == []

    def test_多规则同时命中都记录(self):
        base = dt.datetime(2026, 3, 1, 9, 0, 0)
        # A1~A4 共用设备 DX 与同一联系人 → 设备聚集 + 信息复用
        rows = [{'applicant_id': f'A{i + 1}', 'device_id': 'DX', 'contact_phone': '13900000000',
                 'apply_time': (base + dt.timedelta(hours=i)).strftime('%Y-%m-%d %H:%M:%S')}
                for i in range(4)]
        # A1 在 24h 窗口内再申请 4 次 → 同一申请人共 5 次 → 短时高频
        rows += [{'applicant_id': 'A1', 'device_id': 'DX', 'contact_phone': '13900000000',
                  'apply_time': (base + dt.timedelta(minutes=10 * k)).strftime('%Y-%m-%d %H:%M:%S')}
                 for k in range(1, 5)]
        r = F.review(rows)
        assert r['n_high'] == 2                       # 设备聚集 + 短时高频
        assert r['n_mid'] == 1                        # 信息复用
        assert {h['rule'] for h in r['hits']} == {'device_cluster', 'velocity_burst', 'identity_overlap'}


class TestThresholds:

    def test_阈值可覆盖(self):
        rows = [{'applicant_id': f'A{i}', 'device_id': 'DX'} for i in range(2)]
        assert F.check_device_cluster(rows, F.DEFAULT_THRESHOLDS) is None
        r = F.review(rows, {'device_max_applicants': 1})
        assert r['n_high'] == 1

    def test_无效阈值键被忽略(self):
        r = F.review(_rows_none(), {'不存在的阈值': 999, 'device_max_applicants': 2})
        assert r['thresholds']['device_max_applicants'] == 2
        assert '不存在的阈值' not in r['thresholds']

    def test_阈值原样回传便于审计(self):
        r = F.review(_rows_none())
        assert r['thresholds'] == F.DEFAULT_THRESHOLDS


class TestDeterminism:

    def test_同输入同输出(self):
        """反欺诈裁定必须可复现——同批数据两次扫描结果必须完全一致。"""
        rows = [{'applicant_id': f'A{i}', 'device_id': 'DX',
                 'apply_time': '2026-03-01 10:00:00'} for i in range(5)]
        a, b = F.review(rows), F.review(rows)
        assert a == b

    def test_不修改入参(self):
        rows = [{'applicant_id': 'A1', 'device_id': 'DX'}]
        snapshot = [dict(r) for r in rows]
        F.review(rows)
        assert rows == snapshot


class TestRenderAndCatalog:

    def test_渲染命中文本(self):
        rows = [{'applicant_id': f'A{i}', 'device_id': 'DX'} for i in range(5)]
        txt = F.render(F.review(rows))
        assert '贷前反欺诈扫描' in txt
        assert '设备聚集' in txt

    def test_渲染放行文本带提醒(self):
        txt = F.render(F.review(_rows_none()))
        assert '未命中' in txt
        assert '不代表无欺诈风险' in txt

    def test_渲染非法输入不报错(self):
        assert '未取得有效结果' in F.render(None)
        assert '未取得有效结果' in F.render({})

    def test_规则清单完整(self):
        cat = F.rule_catalog()
        assert len(cat) == 3
        assert {c['rule'] for c in cat} == {'device_cluster', 'velocity_burst', 'identity_overlap'}
        assert all(c['level'] in ('HIGH', 'MID') for c in cat)
