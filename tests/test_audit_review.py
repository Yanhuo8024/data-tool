# -*- coding: utf-8 -*-
"""合规审计凭证 + 人工复核队列。"""

import json

import pytest

import audit_trail as at
import review_queue as rq


def _record(**kw):
    base = dict(
        kind='valuation', subject='小米集团-W (HK1810)',
        sources=[{'metric': '现价', 'name': '东方财富港股行情', 'tier': 3,
                  'period': '2025-12-31', 'retrieved_at': '2026-10-08 23:50',
                  'url': 'https://quote.eastmoney.com/hk/01810.html'}],
        computations=[{'name': '每股内在价值', 'expr': '净利润折现/总股本',
                       'inputs': {'净利润': 416.43e8}, 'output': 68.08, 'unit': '元'}],
        as_of='2025-12-31')
    base.update(kw)
    return at.build_record(**base)


class Test审计凭证:

    def test_必填齐备即可追溯(self, tmp_path):
        log = at.AuditLog(path=str(tmp_path / 'a.jsonl'))
        rec = log.append(_record())
        assert rec['traceable'] is True and rec['warnings'] == []
        assert rec['hash'] and rec['prev_hash'] == ''

    def test_缺来源定位给告警(self, tmp_path):
        log = at.AuditLog(path=str(tmp_path / 'a.jsonl'))
        rec = log.append(_record(sources=[{'metric': '净利', 'name': 'IR', 'tier': 2,
                                           'period': '2025-12-31', 'retrieved_at': 'x'}]))
        assert rec['traceable'] is False
        assert any('缺定位' in w for w in rec['warnings'])

    def test_低层级来源给告警(self, tmp_path):
        log = at.AuditLog(path=str(tmp_path / 'a.jsonl'))
        rec = log.append(_record(sources=[{'metric': '净利', 'name': '某百科', 'tier': 5,
                                           'period': '2025-12-31', 'retrieved_at': 'x',
                                           'url': 'http://x'}]))
        assert any('低层级来源' in w for w in rec['warnings'])

    def test_估值类缺算式给告警(self, tmp_path):
        log = at.AuditLog(path=str(tmp_path / 'a.jsonl'))
        rec = log.append(_record(computations=[]))
        assert any('计算式' in w for w in rec['warnings'])

    def test_哈希链连续(self, tmp_path):
        log = at.AuditLog(path=str(tmp_path / 'a.jsonl'))
        r1 = log.append(_record())
        r2 = log.append(_record(subject='第二家'))
        assert r2['prev_hash'] == r1['hash']
        assert log.verify_chain(log.read_all())['ok'] is True

    def test_篡改被检出(self, tmp_path):
        log = at.AuditLog(path=str(tmp_path / 'a.jsonl'))
        rec = log.append(_record())
        assert log.verify(rec)['hash_ok'] is True
        bad = dict(rec, subject='被改过的公司')
        assert log.verify(bad)['hash_ok'] is False

    def test_断链被检出(self, tmp_path):
        log = at.AuditLog(path=str(tmp_path / 'a.jsonl'))
        log.append(_record())
        log.append(_record(subject='B'))
        rows = log.read_all()
        assert log.verify_chain(rows)['ok'] is True
        # 单看第二条：其 prev_hash 指向第一条，链头悬挂 → 断链
        assert log.verify_chain([rows[1]])['ok'] is False

    def test_落盘重载一致(self, tmp_path):
        p = str(tmp_path / 'a.jsonl')
        at.AuditLog(path=p).append(_record())
        rows = at.AuditLog(path=p).read_all()
        assert len(rows) == 1 and rows[0]['subject'].startswith('小米')

    def test_导出json含链校验(self, tmp_path):
        log = at.AuditLog(path=str(tmp_path / 'a.jsonl'))
        log.append(_record())
        out = log.export_json(path=str(tmp_path / 'export.json'))
        data = json.loads(open(out, encoding='utf-8').read())
        assert data['chain']['ok'] is True and len(data['records']) == 1

    def test_markdown含来源与算式(self, tmp_path):
        log = at.AuditLog(path=str(tmp_path / 'a.jsonl'))
        md = at.render_markdown(log.append(_record()))
        assert '数据来源' in md and '每股内在价值' in md and '内容哈希' in md

    def test_落盘失败不中断业务(self, tmp_path, monkeypatch):
        import os as _os
        monkeypatch.setattr(_os, 'makedirs',
                            lambda *a, **k: (_ for _ in ()).throw(OSError('磁盘已满')))
        log = at.AuditLog(path=str(tmp_path / 'a.jsonl'))
        rec = log.append(_record())
        assert rec['hash']                            # 内存仍保留，不抛异常


class Test复核队列:

    def test_入队并按类别定级(self, tmp_path):
        q = rq.ReviewQueue(path=str(tmp_path / 'q.jsonl'))
        a = q.submit(reason='币种没换算', category='caliber_error')
        assert a['status'] == 'pending' and a['severity'] == 'high'
        b = q.submit(reason='表格没渲染', category='format_issue')
        assert b['severity'] == 'low'

    def test_指定严重度优先于默认(self, tmp_path):
        q = rq.ReviewQueue(path=str(tmp_path / 'q.jsonl'))
        assert q.submit(reason='x', category='format_issue', severity='high')['severity'] == 'high'

    def test_未知类别回落other(self, tmp_path):
        q = rq.ReviewQueue(path=str(tmp_path / 'q.jsonl'))
        assert q.submit(reason='x', category='不存在的类别')['category'] == 'other'

    def test_现金流未补与证据等级低类别(self, tmp_path):
        """P2 要求：用户可标记「现金流未补」「证据等级低」并进入复核队列。"""
        q = rq.ReviewQueue(path=str(tmp_path / 'q.jsonl'))
        a = q.submit(reason='经营现金流没有补上', category='cash_flow_missing')
        b = q.submit(reason='核心结论用了百科数字', category='low_evidence')
        assert a['category_cn'] == '现金流未补' and a['severity'] == 'mid'
        assert b['category_cn'] == '证据等级低' and b['severity'] == 'mid'
        assert len(q.list(status='pending')) == 2

    def test_合法流转到终态(self, tmp_path):
        q = rq.ReviewQueue(path=str(tmp_path / 'q.jsonl'))
        rid = q.submit(reason='x', category='data_error')['id']
        q.transition(rid, 'triaged', actor='a')
        q.transition(rid, 'confirmed', actor='a')
        rec = q.transition(rid, 'fixed', actor='a', resolution='已修')
        assert rec['status'] == 'fixed' and rec['resolved_at']

    def test_非法流转被拒(self, tmp_path):
        q = rq.ReviewQueue(path=str(tmp_path / 'q.jsonl'))
        rid = q.submit(reason='x')['id']
        try:
            q.transition(rid, 'fixed', actor='a')
            raise AssertionError('应当拒绝 pending→fixed')
        except ValueError as e:
            assert '非法状态转移' in str(e)

    def test_未知状态被拒(self, tmp_path):
        q = rq.ReviewQueue(path=str(tmp_path / 'q.jsonl'))
        rid = q.submit(reason='x')['id']
        try:
            q.transition(rid, '不存在的状态')
            raise AssertionError('应当拒绝未知状态')
        except ValueError as e:
            assert '未知状态' in str(e)

    def test_不存在项报错(self, tmp_path):
        q = rq.ReviewQueue(path=str(tmp_path / 'q.jsonl'))
        with pytest.raises(KeyError):
            q.transition('nope', 'triaged')

    def test_指派记录进历史(self, tmp_path):
        q = rq.ReviewQueue(path=str(tmp_path / 'q.jsonl'))
        rid = q.submit(reason='x')['id']
        q.assign(rid, 'bob')
        assert q.get(rid)['assignee'] == 'bob'
        assert any('指派' in h['note'] for h in q.get(rid)['history'])

    def test_SLA超期可检出(self, tmp_path):
        q = rq.ReviewQueue(path=str(tmp_path / 'q.jsonl'))
        rec = q.submit(reason='x', category='data_error')
        assert q.overdue() == []                       # 刚提交未超期
        assert q.overdue(now=9e12)                     # 很久以后必超期

    def test_结项后不再计入超期(self, tmp_path):
        q = rq.ReviewQueue(path=str(tmp_path / 'q.jsonl'))
        rid = q.submit(reason='x', category='data_error')['id']
        q.transition(rid, 'rejected', actor='a')
        assert q.overdue(now=9e12) == []

    def test_统计口径(self, tmp_path):
        q = rq.ReviewQueue(path=str(tmp_path / 'q.jsonl'))
        a = q.submit(reason='x', category='data_error')['id']
        q.submit(reason='y', category='format_issue')
        q.transition(a, 'triaged', actor='x')
        q.transition(a, 'confirmed', actor='x')
        q.transition(a, 'fixed', actor='x')
        st = q.stats()
        assert st['total'] == 2 and st['open'] == 1 and st['fix_rate'] == 0.5
        assert st['by_severity']['high'] == 1

    def test_筛选与排序(self, tmp_path):
        q = rq.ReviewQueue(path=str(tmp_path / 'q.jsonl'))
        q.submit(reason='高', category='data_error')
        q.submit(reason='低', category='format_issue')
        assert len(q.list(status='pending')) == 2
        assert len(q.list(severity='high')) == 1
        assert len(q.list(category='format_issue')) == 1

    def test_持久化重载(self, tmp_path):
        p = str(tmp_path / 'q.jsonl')
        rid = rq.ReviewQueue(path=p).submit(reason='x', category='data_error')['id']
        q2 = rq.ReviewQueue(path=p)
        assert q2.get(rid) is not None and q2.get(rid)['reason'] == 'x'

    def test_badcase钩子被调用(self, tmp_path):
        seen = []
        q = rq.ReviewQueue(path=str(tmp_path / 'q.jsonl'), badcase_hook=seen.append)
        q.submit(reason='x', category='hallucination')
        assert len(seen) == 1 and seen[0]['category'] == 'hallucination'

    def test_钩子异常不影响入队(self, tmp_path):
        def boom(rec, **kw):
            raise RuntimeError('下游炸了')
        q = rq.ReviewQueue(path=str(tmp_path / 'q.jsonl'), badcase_hook=boom)
        assert q.submit(reason='x')['status'] == 'pending'

    def test_渲染含上下文与历史(self, tmp_path):
        q = rq.ReviewQueue(path=str(tmp_path / 'q.jsonl'))
        rid = q.submit(reason='结论不对', category='conclusion_error',
                       context={'symbol': 'HK1810', 'message_id': 'm1'},
                       output_excerpt='每股 313.59 元')['id']
        md = rq.render_item(q.get(rid))
        assert 'HK1810' in md and '313.59' in md and '流转历史' in md


class Test反馈接入:

    def test_负面反馈转复核项(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rq, '_DEFAULT_DIR', str(tmp_path))
        rid = rq.enqueue_from_feedback('dislike',
                                       {'message_id': 'm1', 'source': 'agent_pipeline'},
                                       session_id='s1', reply='每股 313.59 元')
        assert rid
        rec = rq.ReviewQueue(path=str(tmp_path / 'queue.jsonl')).get(rid)
        assert rec['category'] == 'conclusion_error'
        assert rec['context']['session_id'] == 's1'
        assert rec['severity'] == 'mid' and rec['status'] == 'pending'

    def test_正面反馈不入队(self):
        assert rq.enqueue_from_feedback('like', {}) is None
        assert rq.enqueue_from_feedback('', {}) is None

    def test_可指定类别与原因(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rq, '_DEFAULT_DIR', str(tmp_path))
        rid = rq.enqueue_from_feedback('inaccurate',
                                       {'category': 'caliber_error', 'reason': '币种没换算'})
        rec = rq.ReviewQueue(path=str(tmp_path / 'queue.jsonl')).get(rid)
        assert rec['category'] == 'caliber_error' and rec['reason'] == '币种没换算'
        assert rec['severity'] == 'high'

    def test_入队失败不抛异常(self, monkeypatch):
        monkeypatch.setattr(rq, 'ReviewQueue',
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError('磁盘已满')))
        assert rq.enqueue_from_feedback('dislike', {'reason': 'x'}) is None
