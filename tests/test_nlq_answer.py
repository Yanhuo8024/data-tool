"""自然语言查数（nlq_answer）回归测试。

覆盖：真实表结构生成、预览不执行、直执路径、只读护栏、路径白名单、
以及最关键的一条——**任何失败都不得返回伪造数据**（原实现会在库缺失时
吐出硬编码的「广东/浙江」示例行）。
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db_connector            # noqa: E402
import nlq_answer as N         # noqa: E402

SENTINEL = 'SENTINEL_NLQ_COL_9K3'
GOOD_SQL = 'SELECT region, SUM(revenue) AS "revenue总和" FROM metrics GROUP BY region'


@pytest.fixture
def demo_db(tmp_path, monkeypatch):
    """隔离测试库：建在 tmp_path/output/，并把该目录临时设为 SQLite 白名单根。"""
    out = tmp_path / 'output'
    out.mkdir()
    db = out / 'probe.db'
    con = sqlite3.connect(str(db))
    con.execute('CREATE TABLE metrics (id INTEGER, region TEXT, revenue REAL, note TEXT)')
    con.executemany('INSERT INTO metrics VALUES (?,?,?,?)',
                    [(1, 'North', 120.5, SENTINEL),
                     (2, 'South', 80.25, SENTINEL),
                     (3, 'North', 99.0, SENTINEL)])
    con.commit()
    con.close()
    monkeypatch.setattr(db_connector, 'OUT_DIR', str(out))
    monkeypatch.setattr(N, 'BASE_DIR', tmp_path)
    return str(db)


@pytest.fixture
def llm_recorder(monkeypatch):
    """LLM 替身：记录调用并返回固定 SQL。签名带 **kw，防窄签名假通过。"""
    calls = []

    def _gen(cfg, question, hint='', **kw):
        calls.append({'question': question, 'hint': hint})
        return GOOD_SQL

    monkeypatch.setattr(N, 'generate_sql', _gen)
    return calls


def test_clean_sql_strips_markdown_and_prefix():
    assert N.clean_sql('```sql\nSELECT 1\n```') == 'SELECT 1'
    assert N.clean_sql('说明文字\nSELECT 2 FROM t;') == 'SELECT 2 FROM t'


def test_preview_returns_sql_without_executing(demo_db, llm_recorder):
    d = N.answer('各区域营收合计', db_path=demo_db, preview=True)
    assert d['ok'] is True and d['preview'] is True
    assert 'FROM metrics' in d['sql']
    assert d['rows'] == [] and d['data'] == []
    assert llm_recorder, '预览应调用一次生成'


def test_execute_returns_real_rows(demo_db, llm_recorder):
    d = N.answer('各区域营收合计', db_path=demo_db)
    assert d['ok'] is True
    assert len(d['rows']) == 2
    assert set(d['columns']) == {'region', 'revenue总和'}
    assert 'LIMIT' in d['sql'].upper(), '应自动追加 LIMIT'
    assert d['data'][0]['region'] == d['rows'][0][0]
    assert ['North', 219.5] in [list(r) for r in d['rows']], '聚合值须来自真实测试库'


def test_direct_sql_skips_generation(demo_db, llm_recorder):
    d = N.answer('', sql='SELECT COUNT(*) FROM metrics', db_path=demo_db)
    assert d['ok'] is True
    assert d['rows'][0][0] == 3
    assert llm_recorder == [], '直执路径不得调用生成（避免 SQL 漂移）'


@pytest.mark.parametrize('stmt', [
    'DROP TABLE metrics',
    'UPDATE metrics SET revenue=0',
    'DELETE FROM metrics',
    'INSERT INTO metrics VALUES (9,1,1,1)',
])
def test_write_statements_are_blocked(demo_db, llm_recorder, stmt):
    d = N.answer('', sql=stmt, db_path=demo_db)
    assert d['ok'] is False
    assert d['errors'], '写入语句必须被拦截并给出原因'


def test_missing_db_returns_error_not_fake_data(tmp_path, monkeypatch, llm_recorder):
    """核心回归：库不存在时必须报错，绝不返回伪造行（原实现返回广东/浙江示例）。"""
    monkeypatch.setattr(db_connector, 'OUT_DIR', str(tmp_path))
    monkeypatch.setattr(N, 'BASE_DIR', tmp_path)
    d = N.answer('随便问问', db_path=str(tmp_path / 'nope.db'))
    assert d['ok'] is False
    assert d['rows'] == [] and d['data'] == []
    assert any('不存在' in e for e in d['errors'])
    assert llm_recorder == [], '库都不存在就不该去生成 SQL'


@pytest.mark.parametrize('bad', ['/etc/passwd', 'C:/Windows/win.ini', '../../etc/hosts'])
def test_path_whitelist_rejects_outside_paths(tmp_path, monkeypatch, bad):
    monkeypatch.setattr(db_connector, 'OUT_DIR', str(tmp_path))
    d = N.answer('x', db_path=bad)
    assert d['ok'] is False and d['errors']


def test_app_db_is_denied(tmp_path, monkeypatch):
    """站点主库存用户凭据，永远不可经 SQL 接口直连。"""
    (tmp_path / 'app.db').write_bytes(b'')
    monkeypatch.setattr(db_connector, 'OUT_DIR', str(tmp_path))
    assert db_connector.safe_sqlite_path(str(tmp_path / 'app.db')) is None


def test_llm_disabled_reports_error_without_guessing(demo_db, monkeypatch):
    monkeypatch.setenv('NLQ_ANSWER_LLM', '0')
    d = N.answer('各区域营收合计', db_path=demo_db)
    assert d['ok'] is False and d['llm'] is False
    assert any('已关闭' in e for e in d['errors'])


def test_unanswerable_question_is_not_faked(demo_db, monkeypatch):
    monkeypatch.setattr(N, 'generate_sql',
                        lambda cfg, q, hint='', **kw: '-- 无法回答：与表结构无关')
    d = N.answer('今天天气如何', db_path=demo_db)
    assert d['ok'] is False and d['rows'] == []


def test_failed_first_attempt_retries_with_hint(demo_db, monkeypatch):
    """首次执行失败 → 带错误提示重试一次，且第二次用更正后的 SQL。"""
    seq = []

    def _gen(cfg, question, hint='', **kw):
        seq.append(hint)
        return 'SELECT nonexistent_col FROM metrics' if not hint else 'SELECT COUNT(*) AS n FROM metrics'

    monkeypatch.setattr(N, 'generate_sql', _gen)
    d = N.answer('有多少条记录', db_path=demo_db)
    assert len(seq) == 2, '应重试恰好一次'
    assert seq[1], '第二次必须携带错误提示'
    assert d['ok'] is True and d['rows'][0][0] == 3


def test_second_failure_is_reported_not_swallowed(demo_db, monkeypatch):
    monkeypatch.setattr(N, 'generate_sql',
                        lambda cfg, q, hint='', **kw: 'SELECT nonexistent_col FROM metrics')
    d = N.answer('有多少条记录', db_path=demo_db)
    assert d['ok'] is False and d['errors']


def test_literal_col_detection():
    assert N.detect_literal_cols(['名称'], [['名称'], ['名称']]) == ['名称']
    assert N.detect_literal_cols(['名称'], [['A'], ['B']]) == []
    assert N.detect_literal_cols([], []) == []


def test_discover_sources_lists_sqlite(demo_db, tmp_path, monkeypatch):
    monkeypatch.setattr(N, 'BASE_DIR', tmp_path)
    sources = N.discover_sources()
    assert 'sqlite:probe.db' in [s['id'] for s in sources]


def test_split_source_handles_both_kinds(tmp_path, monkeypatch):
    monkeypatch.setattr(N, 'BASE_DIR', tmp_path)
    p, cid = N.split_source('sqlite:probe.db')
    assert p and p.endswith('probe.db') and cid is None
    p2, cid2 = N.split_source('conn:abc123')
    assert p2 is None and cid2 == 'abc123'
    assert N.split_source('') == (None, None)


def test_empty_question_is_rejected(demo_db):
    d = N.answer('', db_path=demo_db)
    assert d['ok'] is False
    assert any('不能为空' in e for e in d['errors'])
