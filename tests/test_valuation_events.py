# -*- coding: utf-8 -*-
"""事件驱动估值更新：检测 / 重估 / 去重 / 闸门 / 如实上报。"""

import valuation_events as ve


def _engine(**kw):
    """重估桩：行情跌幅 >8% 时落入闸门，其余给常规结论。"""
    def revalue(symbol, market, ev, **kwargs):
        p = ev.payload or {}
        drift = p.get('drift_pct') or 0
        if ev.kind == 'quote' and drift < -8:
            return {'ok': True, 'per_share': 50.0, 'price': p.get('price'),
                    'verdict': 'assumption_extreme', 'verdict_cn': '假设极端，需人工复核',
                    'flags': ['single_year_growth_capped'], 'as_of': '2025-12-31'}
        return {'ok': True, 'per_share': 68.08, 'price': p.get('price'),
                'verdict': 'overvalued', 'verdict_cn': '高估', 'flags': [],
                'as_of': '2025-12-31'}
    return ve.ValuationEventEngine(revalue, **kw)


class Test事件检测:

    def test_单日异动触发(self):
        ev = ve.detect_quote_event(None, {'symbol': '01810', 'price': 23.94, 'change_pct': -7.3})
        assert ev is not None and ev.kind == 'quote'
        assert ev.payload['change_pct'] == -7.3

    def test_平静行情不触发(self):
        assert ve.detect_quote_event(None, {'symbol': 'X', 'price': 10, 'change_pct': 0.5}) is None

    def test_相对上次快照漂移触发(self):
        ev = ve.detect_quote_event({'price': 100.0}, {'symbol': 'X', 'price': 92.0, 'change_pct': 0.1})
        assert ev is not None and ev.payload['drift_pct'] == -8.0

    def test_无价格不触发(self):
        assert ve.detect_quote_event(None, {'symbol': 'X', 'price': 0}) is None

    def test_公告命中关键词(self):
        evs = ve.detect_announcement_events('01810', 'hk', [
            {'title': '2025 年全年业绩公告', 'url': 'http://x'},
            {'title': '关于召开股东周年大会的通函'},
        ])
        assert len(evs) == 1 and evs[0].payload['hit']

    def test_经营数据命中交付(self):
        evs = ve.detect_delivery_events('01810', 'hk', [
            {'title': '2026 年 1 月汽车交付量', 'value': 22897},
            {'title': '公司简介'},
        ])
        assert len(evs) == 1 and evs[0].payload['value'] == 22897

    def test_指纹稳定且同类同内容相同(self):
        a = ve.Event(kind='quote', symbol='X', payload={'price': 1, 'b': 2})
        b = ve.Event(kind='quote', symbol='X', payload={'b': 2, 'price': 1})
        assert a.fingerprint() == b.fingerprint()


class Test引擎:

    def test_重估并产出摘要(self):
        eng = _engine()
        ev = ve.Event(kind='quote', symbol='01810', market='hk',
                      payload={'price': 23.94, 'change_pct': -7.3})
        out = eng.ingest([ev])
        assert len(out) == 1
        o = out[0]
        assert o['ok'] and o['new']['per_share'] == 68.08
        assert '每股内在价值' in o['summary']
        assert o['needs_review'] is False

    def test_落入闸门则转人工(self):
        eng = _engine()
        eng.ingest([ve.Event(kind='quote', symbol='A', payload={'price': 24.0, 'drift_pct': -9})])
        out = eng.ingest([ve.Event(kind='quote', symbol='B', payload={'price': 20.0, 'drift_pct': -10})])
        assert out[0]['needs_review'] is True
        assert '转人工复核' in out[0]['summary']

    def test_结论方向翻转标记复核(self):
        def revalue(symbol, market, ev, **kw):
            flip = ev.payload.get('flip')
            return {'ok': True, 'per_share': 10.0, 'price': 10,
                    'verdict': 'undervalued' if flip else 'overvalued',
                    'verdict_cn': '低估' if flip else '高估', 'flags': []}
        eng = ve.ValuationEventEngine(revalue)
        eng.ingest([ve.Event(kind='quote', symbol='S', payload={'price': 10})])
        out = eng.ingest([ve.Event(kind='announcement', symbol='S',
                                   payload={'title': '业绩公告', 'flip': True})])
        assert out[0]['verdict_changed'] is True and out[0]['needs_review'] is True

    def test_重复事件去重(self):
        eng = _engine()
        ev = ve.Event(kind='quote', symbol='X', payload={'price': 1, 'change_pct': -9})
        assert len(eng.ingest([ev])) == 1
        assert eng.ingest([ev]) == []
        assert eng.status()['deduped'] == 1

    def test_重估异常如实记录不编造(self):
        def boom(*a, **kw):
            raise RuntimeError('取数超时')
        eng = ve.ValuationEventEngine(boom)
        out = eng.ingest([ve.Event(kind='quote', symbol='X', payload={'price': 1})])
        assert out[0]['ok'] is False and '取数超时' in out[0]['error']
        assert eng.status()['failed'] == 1

    def test_重估返回空也算失败(self):
        eng = ve.ValuationEventEngine(lambda *a, **kw: None)
        out = eng.ingest([ve.Event(kind='quote', symbol='X', payload={'price': 1})])
        assert out[0]['ok'] is False

    def test_订阅收到结果(self):
        eng = _engine()
        got = []
        eng.subscribe('quote', got.append)
        eng.ingest([ve.Event(kind='quote', symbol='X', payload={'price': 1, 'change_pct': -9})])
        assert len(got) == 1 and got[0]['symbol'] == 'X'

    def test_订阅者异常不影响主流程(self):
        eng = _engine()
        eng.subscribe('*', lambda o: (_ for _ in ()).throw(ValueError('订阅炸了')))
        out = eng.ingest([ve.Event(kind='quote', symbol='X', payload={'price': 1, 'change_pct': -9})])
        assert len(out) == 1

    def test_扫描产出事件并更新快照(self):
        quotes = {'X': {'price': 100.0, 'change_pct': 0.2}, 'Y': {'price': 50.0, 'change_pct': 0.1}}
        calls = []

        def fetch(symbol, market, **kw):
            calls.append(symbol)
            return dict(quotes.get(symbol, {}), symbol=symbol, market=market)

        eng = _engine()
        syms = [{'symbol': 'X', 'market': 'a'}, {'symbol': 'Y', 'market': 'a'}]
        assert eng.scan(syms, fetch) == []          # 首次无对比基准
        quotes['X'] = {'price': 92.0, 'change_pct': -8.0}
        out = eng.scan(syms, fetch)
        assert len(out) == 1 and out[0]['symbol'] == 'X'
        assert calls.count('X') == 2

    def test_状态与历史(self):
        eng = _engine()
        eng.ingest([ve.Event(kind='quote', symbol='X', payload={'price': 1, 'change_pct': -9})])
        st = eng.status()
        assert st['revalued'] == 1 and st['tracked'] == 1
        assert len(eng.recent(5)) == 1

    def test_历史长度受限(self):
        eng = _engine(max_history=2)
        for i in range(5):
            eng.ingest([ve.Event(kind='quote', symbol=f'S{i}',
                                 payload={'price': i + 1, 'change_pct': -9})])
        assert len(eng.recent(100)) == 2
