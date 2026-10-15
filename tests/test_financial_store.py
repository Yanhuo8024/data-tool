# -*- coding: utf-8 -*-
"""财务数据自动化回归（2026-10-08 用户第 3 项）。

- 财报 PDF 解析：自动提取分部收入、毛利率、经营利润（**标注页码与待人工确认**）；
- 财务数据库：每条记录带 version 与 as-of 日期，支持时点回溯；
- 比率计算：一律 SQL/Python，不经 LLM（且算式可审计）。
"""
import importlib
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """每个用例独立库文件，避免相互污染。"""
    monkeypatch.setenv('FIN_STORE_PATH', str(tmp_path / 'fin.duckdb'))
    import financial_store as fs
    importlib.reload(fs)
    fs.init_db()
    return fs


SAMPLE = [
    {'metric': 'revenue', 'value': 4572.87, 'unit': '亿元'},
    {'metric': 'net_profit', 'value': 416.43},
    {'metric': 'gross_profit', 'value': 1017.9},
    {'metric': 'equity', 'value': 2663.23},
    {'metric': 'total_assets', 'value': 5077.0},
    {'metric': 'total_liab', 'value': 2413.8},
]


class Test版本化与asof:

    def test_写入带版本与截止日(self, store):
        n = store.put(SAMPLE, version='fin-2025A', as_of='2025-12-31',
                      source='公司IR年报', source_tier=2, period='2025-12-31')
        assert n == 6
        row = store.latest('net_profit')
        assert row['version'] == 'fin-2025A'
        assert row['as_of'] == '2025-12-31'
        assert row['source'] == '公司IR年报' and row['source_tier'] == 2

    def test_未给版本自动生成(self, store):
        store.put([{'metric': 'revenue', 'value': 1}], as_of='2025-12-31')
        assert store.latest('revenue')['version'].startswith('fin-')

    def test_时点回溯(self, store):
        store.put([{'metric': 'revenue', 'value': 4572.87}], version='A', as_of='2025-12-31')
        store.put([{'metric': 'revenue', 'value': 4000.0}], version='B', as_of='2024-12-31')
        assert store.latest('revenue', as_of='2024-12-31')['value'] == 4000.0
        assert store.latest('revenue')['value'] == 4572.87

    def test_历史版本可追溯(self, store):
        store.put([{'metric': 'revenue', 'value': 4572.87}], version='A', as_of='2025-12-31')
        store.put([{'metric': 'revenue', 'value': 4600.0}], version='A2', as_of='2026-03-31')
        h = store.history('revenue')
        assert [x['value'] for x in h] == [4600.0, 4572.87]
        assert {x['version'] for x in h} == {'A', 'A2'}

    def test_按版本筛选(self, store):
        store.put([{'metric': 'revenue', 'value': 1}], version='A', as_of='2025-12-31')
        store.put([{'metric': 'revenue', 'value': 2}], version='B', as_of='2025-12-31')
        assert store.query('revenue', version='B')[0]['value'] == 2

    def test_缺指标名或值被跳过(self, store):
        assert store.put([{'metric': '', 'value': 1}, {'metric': 'x', 'value': None}]) == 0

    def test_指标清单(self, store):
        store.put(SAMPLE, version='A', as_of='2025-12-31')
        assert 'net_profit' in store.metrics() and len(store.metrics()) == 6


class Test比率由算力而非LLM:

    def test_比率可算且带算式(self, store):
        store.put(SAMPLE, version='A', as_of='2025-12-31')
        r = store.derive_ratios()['ratios']
        assert r['net_margin']['value'] == pytest.approx(9.1065, abs=0.01)
        assert r['gross_margin']['value'] == pytest.approx(22.2595, abs=0.01)
        assert r['roe']['value'] == pytest.approx(15.6363, abs=0.01)
        assert r['debt_ratio']['value'] == pytest.approx(47.5438, abs=0.01)

    def test_每项都带可审计算式与输入(self, store):
        store.put(SAMPLE, version='A', as_of='2025-12-31')
        for k, v in store.derive_ratios()['ratios'].items():
            assert v['expr'] and v['inputs'], k

    def test_缺输入进missing不静默(self, store):
        store.put([{'metric': 'net_profit', 'value': 10}], version='A', as_of='2025-12-31')
        r = store.derive_ratios()
        assert not r['ratios']
        assert any('缺' in m for m in r['missing'])

    def test_分母为零不崩(self, store):
        store.put([{'metric': 'net_profit', 'value': 10},
                   {'metric': 'revenue', 'value': 0}], version='A', as_of='2025-12-31')
        r = store.derive_ratios()
        assert 'net_margin' not in r['ratios']
        assert any('分母为 0' in m for m in r['missing'])

    def test_按asof派生可回溯(self, store):
        store.put([{'metric': 'net_profit', 'value': 100}, {'metric': 'revenue', 'value': 1000}],
                  version='A', as_of='2024-12-31')
        store.put([{'metric': 'net_profit', 'value': 200}, {'metric': 'revenue', 'value': 1000}],
                  version='B', as_of='2025-12-31')
        assert store.derive_ratios(as_of='2024-12-31')['ratios']['net_margin']['value'] == 10.0
        assert store.derive_ratios()['ratios']['net_margin']['value'] == 20.0


class Test财报PDF分部提取:
    """财报版式千差万别，提取只做定位初筛，必须标注页码与待人工确认。"""

    @pytest.fixture()
    def pdf(self, tmp_path):
        fitz = pytest.importorskip('fitz')
        p = tmp_path / 'annual.pdf'
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((50, 60), '分部资料 / Segment Information', fontname='china-s')
        y = 90
        for line in ('智能手机 2000 亿元 45.0% 12.5%',
                     'IoT与生活消费产品 1000 亿元 22.0% 15.0%',
                     '互联网服务 350 亿元 75.0% 30.0%',
                     '智能电动汽车及创新业务 1000 亿元 20.4% 3.0%'):
            page.insert_text((50, y), line, fontname='china-s')
            y += 18
        doc.new_page().insert_text((50, 60), '无关键字的普通页', fontname='china-s')
        doc.save(str(p))
        doc.close()
        return str(p)

    def test_能定位分部页(self, pdf, store):
        r = store.extract_pdf_segments(pdf)
        assert r['ok'] is True
        assert 1 in r['hit_pages']

    def test_抽取行带页码(self, pdf, store):
        r = store.extract_pdf_segments(pdf)
        assert r['rows'] and all(row['page'] == 1 for row in r['rows'])
        assert any('智能手机' in row['name'] for row in r['rows'])

    def test_标记待人工确认(self, pdf, store):
        r = store.extract_pdf_segments(pdf)
        assert r['needs_review'] is True
        assert '人工核对' in r['note']

    def test_列出期望字段(self, pdf, store):
        assert '经营利润' in store.extract_pdf_segments(pdf)['field_hint']

    def test_文件不存在如实报错(self, store):
        r = store.extract_pdf_segments('不存在的文件.pdf')
        assert r['ok'] is False and '不存在' in r['reason']

    def test_无分部页时如实报未定位到(self, tmp_path, store):
        fitz = pytest.importorskip('fitz')
        p = tmp_path / 'plain.pdf'
        doc = fitz.open()
        doc.new_page().insert_text((50, 60), '这是一份没有分部资料的文档', fontname='china-s')
        doc.save(str(p))
        doc.close()
        r = store.extract_pdf_segments(str(p))
        assert r['ok'] is False and '未在 PDF 中定位到' in r['reason']

    def test_确认后可转成入库记录(self, pdf, store):
        rows = store.extract_pdf_segments(pdf)['rows']
        recs = store.to_records(rows, segment='智能手机', period='2025-12-31')
        assert recs and recs[0]['metric'].startswith('智能手机.')
        assert all('page' in r for r in recs)

    def test_入库后可按分部查询(self, pdf, store):
        rows = store.extract_pdf_segments(pdf)['rows']
        recs = store.to_records(rows, segment='智能手机', period='2025-12-31')
        store.put(recs, version='fin-2025A', as_of='2025-12-31', source='公司IR年报')
        got = store.query('智能手机.revenue')
        assert got and got[0]['value'] == 2000.0
        assert got[0]['page'] == '1'      # 页码作为溯源留档
