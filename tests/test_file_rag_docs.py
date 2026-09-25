# -*- coding: utf-8 -*-
"""tests/test_file_rag_docs.py — 文档类资料（非表格）接入知识库 RAG 的回归

对应能力：让 Agent 能"读研报 / 财报 / 合同 / 产品规则"这类非结构化资料。
此前 `file_rag` 只处理 csv/xlsx（经 `load_dataframe`），文档类后缀会抛
`ValueError: Unsupported format` 并被静默吞成空字符串 —— Agent 对这类文件"看不见内容"。

本组测试要证明的不只是"能读出文本"，还包括：
  ① 出境关闭时**正文不外发**（只留元信息），与表格的「仅本地统计」同口径；
  ② 解析库缺失（PDF）时**优雅降级并明说没读到**，不崩、也不谎报有内容；
  ③ 模式切换后**旧索引被剔除**，关闭前索引的正文不会残留可检索。
"""
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import egress_policy as ep  # noqa: E402
import file_rag  # noqa: E402

SENTINEL = 'SENTINEL_DOC_BODY_2026'


@pytest.fixture
def store(tmp_path, monkeypatch):
    """隔离出境策略文件 + 清空相关环境变量"""
    p = tmp_path / 'egress_policy.json'
    monkeypatch.setenv('EGRESS_POLICY_FILE', str(p))
    for feat in ep.FEATURES.values():
        monkeypatch.delenv(feat['env'], raising=False)
    ep.reset_cache()
    yield p
    ep.reset_cache()


@pytest.fixture
def out_dir(tmp_path, monkeypatch):
    """把 file_rag 读取文件的位置指到临时目录"""
    import core_config
    monkeypatch.setattr(core_config, 'OUT_DIR', tmp_path)
    monkeypatch.setattr(file_rag, '_INDEXED', {})
    return tmp_path


@pytest.fixture
def eng_recorder(monkeypatch):
    """替掉 rag_unified.rag_engine，记录写入的文档正文"""

    class _Eng:
        def __init__(self):
            self.docs, self.deleted, self._n = {}, [], 0

        def add_document(self, text, name, dtype='generic'):
            self._n += 1
            did = f'd{self._n}'
            self.docs[did] = text
            return did

        def delete_document(self, did):
            self.deleted.append(did)
            self.docs.pop(did, None)

        def search(self, *_a, **_k):
            return []

    eng = _Eng()
    monkeypatch.setitem(sys.modules, 'rag_unified', types.SimpleNamespace(rag_engine=eng))
    return eng


# ------------------------------- 文档后缀覆盖 -------------------------------
def test_doc_exts_cover_report_and_contract_formats():
    for ext in ('.pdf', '.docx', '.txt', '.md', '.json', '.html'):
        assert ext in file_rag._DOC_EXTS, f'{ext} 应纳入文档类抽取'


# ------------------------------- 纯文本类 -------------------------------
def test_markdown_and_txt_body_is_indexed(store, out_dir):
    (out_dir / 'report.md').write_text(f'# 行业周报\n{SENTINEL} 增速 12%\n', encoding='utf-8')
    text = file_rag._load_as_text('report.md')
    assert SENTINEL in text, '文档正文应进入索引文本（出境默认开启）'
    assert '文档类资料' in text and '字符' in text

    (out_dir / 'notes.txt').write_text(f'政策要点：{SENTINEL}\n', encoding='utf-8')
    assert SENTINEL in file_rag._load_as_text('notes.txt')


# ------------------------------- Word -------------------------------
def test_docx_reads_paragraphs_and_tables(store, out_dir):
    docx = pytest.importorskip('docx')
    d = docx.Document()
    d.add_paragraph(f'研报结论：{SENTINEL}')
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text = '科目'
    t.cell(0, 1).text = '金额'
    t.cell(1, 0).text = '营收'
    t.cell(1, 1).text = '100'
    d.save(str(out_dir / 'research.docx'))

    text = file_rag._load_as_text('research.docx')
    assert SENTINEL in text, '段落正文应被抽取'
    assert '营收' in text and '[表1]' in text, '表格应被抽取并带表号'


# ------------------------------- PDF -------------------------------
def test_pdf_without_parser_degrades_instead_of_lying(store, out_dir, monkeypatch):
    """解析库缺失时必须"明说没读到"，不能返回空正文让模型自由发挥"""
    (out_dir / 'annual.pdf').write_bytes(b'%PDF-1.4\nnot-a-real-pdf\n')
    for mod in ('fitz', 'pypdf', 'PyPDF2'):
        monkeypatch.setitem(sys.modules, mod, None)      # import 直接抛 ImportError

    text = file_rag._load_as_text('annual.pdf')
    assert '未成功' in text and '解析库' in text
    assert SENTINEL not in text


def test_pdf_uses_available_parser(store, out_dir, monkeypatch):
    """有解析库时正常抽取（用假 pypdf 模拟，避免测试依赖真实库）"""
    (out_dir / 'annual.pdf').write_bytes(b'%PDF-1.4\n')
    monkeypatch.setitem(sys.modules, 'fitz', None)
    monkeypatch.setitem(sys.modules, 'pypdf', types.SimpleNamespace(
        PdfReader=lambda _p: types.SimpleNamespace(
            pages=[types.SimpleNamespace(extract_text=lambda: f'年度报告 {SENTINEL}')])))

    text = file_rag._load_as_text('annual.pdf')
    assert SENTINEL in text and '页数: 1' in text


# ------------------------------- 出境开关 -------------------------------
def test_egress_off_leaves_metadata_only(store, out_dir):
    (out_dir / 'report.md').write_text(f'正文 {SENTINEL}\n' * 20, encoding='utf-8')
    assert SENTINEL in file_rag._load_as_text('report.md')

    ep.set_flag('file_sample_egress', False)
    ep.reset_cache()
    meta = file_rag._load_as_text('report.md')
    assert SENTINEL not in meta, '关闭后不得含任何正文片段'
    assert '仅本地统计模式' in meta and '文件:' in meta
    assert file_rag._mode() == 'stats'


def test_doc_mode_switch_rebuilds_index_and_drops_body(store, out_dir, eng_recorder):
    (out_dir / 'report.md').write_text(f'正文 {SENTINEL}\n', encoding='utf-8')
    first = file_rag.index_current_file('report.md')
    assert any(SENTINEL in t for t in eng_recorder.docs.values())

    ep.set_flag('file_sample_egress', False)
    ep.reset_cache()
    second = file_rag.index_current_file('report.md')
    assert second != first and first in eng_recorder.deleted, '切换模式必须剔除旧索引'
    assert not any(SENTINEL in t for t in eng_recorder.docs.values()), '关闭后不得残留正文'


# ------------------------------- 非文档后缀不受影响 -------------------------------
def test_binary_file_is_ignored_without_crash(store, out_dir):
    (out_dir / 'pic.png').write_bytes(b'\x89PNG\r\n\x1a\n')
    assert file_rag._load_as_text('pic.png') == ''      # 原表格分支不支持 → 静默跳过
    assert file_rag._load_as_text('missing.md') == ''   # 文件不存在 → 空字符串
